import json
from dataclasses import replace
import httpx
import pytest

from src import model_roles as roles
from src.knowledge_engine import semantic
from src.knowledge_engine.index import VaultIndex
from tests.test_knowledge_index import vault,index


@pytest.fixture
def routing(monkeypatch):
    roles._HEALTH.clear();roles._LAST.clear()
    settings={}
    monkeypatch.setattr(roles,'setting',lambda key,owner='',default=None:settings.get(key,default))
    monkeypatch.setattr(roles,'resolve',lambda a,owner='',required=None:roles.Route(a,'https://'+(a.endpoint_id or 'missing')+'.test/v1/chat/completions',{}))
    return settings


def assignment(endpoint='primary',model='embed',dim=3,space_id='same'):
    return roles.Assignment(endpoint,model,'openai_embeddings',('embedding',),space_id,'revision-1',dim)


def configure(routing,primary,fallback=None,role='embedding'):
    from dataclasses import asdict
    routing.update({role+'_endpoint_id':primary.endpoint_id,role+'_model':primary.model})
    routing.setdefault(roles.POLICY_KEY,{})[role]={'timeout':1,'retries':0,'cooldown':1,
        'primary_metadata':{k:v for k,v in asdict(primary).items() if k not in {'endpoint_id','model'}},
        'fallback':asdict(fallback) if fallback else None}


@pytest.mark.asyncio
async def test_real_sqlite_vec_incremental_persistence_and_deletion(index,vault,routing,monkeypatch):
    configure(routing,assignment())
    calls=[]
    async def fake(route,texts):
        calls.extend(texts)
        return [[1.,0.,0.] for _ in texts]
    monkeypatch.setattr(semantic,'embed',fake)
    first=await semantic.index_embeddings(index)
    assert first['generated']==index.status()['chunks']
    assert (await semantic.index_embeddings(index))['generated']==0
    same=VaultIndex(vault[0],owner='alice',cache_dir=vault[1])
    results=await semantic.search(same,'tolerans',mode='hybrid')
    assert results['matches'] and results['services']['embedding'].get('model')=='embed', (
        f"Embedding search degradation: {results.get('services')!r}; "
        f"vector coverage: {results.get('embedding_coverage')!r}"
    )
    assert all('start_line' in item for item in results['matches'])
    with index.connect() as db:
        before={r['chunk_id'] for r in db.execute('SELECT * FROM chunk_vectors')}
    (vault[0]/'Projects/Lid.md').write_text((vault[0]/'Projects/Lid.md').read_text()+'\nNew sentence.\n')
    index.reconcile()
    with index.connect() as db:
        retained={r['chunk_id'] for r in db.execute('SELECT * FROM chunk_vectors')}
    assert retained <= before
    assert (await semantic.index_embeddings(index))['generated'] < first['generated']
    (vault[0]/'Projects/Box.md').unlink();index.reconcile()
    with index.connect() as db:
        assert not db.execute('SELECT 1 FROM chunk_vectors v LEFT JOIN chunks c ON c.id=v.chunk_id WHERE c.id IS NULL').fetchone()


@pytest.mark.asyncio
async def test_incompatible_fallback_does_not_mix_or_rebuild_query_index(index,routing,monkeypatch):
    primary=assignment()
    fallback=assignment('secondary','other-embed',3,'other-space')
    configure(routing,primary,fallback)
    fail=[False];called=[]
    async def fake(route,texts):
        called.append(route.assignment.model)
        if fail[0] and route.assignment.endpoint_id=='primary':
            raise httpx.ConnectError('offline')
        return [[1.,0.,0.] for _ in texts]
    monkeypatch.setattr(semantic,'embed',fake)
    await semantic.index_embeddings(index)
    fail[0]=True
    count=semantic.coverage(index)[0]['embedded']
    result=await semantic.search(index,'tolerans',mode='hybrid')
    assert result['matches'] and result['services']['embedding']['status']=='degraded'
    assert len(semantic.coverage(index))==1 and semantic.coverage(index)[0]['embedded']==count
    assert semantic.space(primary)!=semantic.space(fallback)
    # Explicit maintenance may build a separate fallback namespace.
    with index.connect() as db:db.execute('DELETE FROM chunk_vectors')
    await semantic.index_embeddings(index)
    assert len(semantic.coverage(index))==2


@pytest.mark.asyncio
async def test_compatible_endpoint_fallback_reuses_vectors(index,routing,monkeypatch):
    primary=assignment()
    configure(routing,primary,replace(primary,endpoint_id='secondary'))
    failed=[False]
    async def fake(route,texts):
        if failed[0] and route.assignment.endpoint_id=='primary':
            raise httpx.ConnectError('offline')
        return [[1.,0.,0.] for _ in texts]
    monkeypatch.setattr(semantic,'embed',fake)
    await semantic.index_embeddings(index)
    failed[0]=True
    result=await semantic.search(index,'tolerans',mode='semantic',property_filters={'owned':True})
    assert result['matches'] and result['services']['embedding'].get('fallback_active'), (
        f"Embedding failover degradation: {result.get('services')!r}; "
        f"vector coverage: {result.get('embedding_coverage')!r}"
    )
    assert {r['rel_path'] for r in result['matches']}=={'Projects/Lid.md'}


def test_fusion_preserves_ids_and_deduplicates():
    a={'chunk_id':'a','note_id':'n','start_line':1}
    b={'chunk_id':'b','note_id':'m','start_line':4}
    result=semantic.fuse([a,b,a],[b])
    assert result[0]['chunk_id']=='b'
    assert len(result)==2 and result[0]['note_id']=='m'


@pytest.mark.asyncio
async def test_http_embedding_protocol_and_wrong_dimension(monkeypatch):
    route=roles.Route(assignment(),'https://embed.test/v1/chat/completions',{})
    async def respond(request):
        assert request.url.path=='/v1/embeddings'
        return httpx.Response(200,json={'data':[{'index':0,'embedding':[3.,4.,0.]}]})
    client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(semantic.httpx,'AsyncClient',lambda **kwargs:client)
    result=await semantic.embed(route,['text'])
    assert result[0]==[.6,.8,0.]
    with pytest.raises(roles.CapabilityUnavailable):
        semantic.vectors([[1,2]],1,3)


@pytest.mark.asyncio
async def test_reranker_scoring_api_fallback_and_fused_degradation(index,routing,monkeypatch):
    primary=roles.Assignment('primary','rank','rerank',('reranking',))
    fallback=replace(primary,endpoint_id='secondary',model='different',adapter='tei_rerank')
    configure(routing,primary,fallback,role='reranking')
    requests=[]
    async def respond(request):
        requests.append(request)
        if request.url.host=='primary.test':return httpx.Response(503)
        assert request.url.path=='/rerank'
        return httpx.Response(200,json=[{'index':0,'score':.1},{'index':1,'score':.9}])
    real=httpx.AsyncClient
    monkeypatch.setattr(semantic.httpx,'AsyncClient',lambda **kwargs:real(transport=httpx.MockTransport(respond)))
    docs=[{'chunk_id':'a','excerpt':'first'},{'chunk_id':'b','excerpt':'second'}]
    ordered,diag=await semantic.rerank('query',docs)
    assert ordered==docs[::-1] and diag['fallback_active'] and len(requests)==2
    # With no embedding assignment, ordinary lexical evidence remains usable.
    degraded=await semantic.search(index,'tolerans',mode='hybrid',use_reranking=True)
    assert degraded['matches'] and degraded['services']['embedding']['status']=='degraded'


@pytest.mark.asyncio
async def test_dense_cached_results_cannot_bypass_current_symlink_scope(index,routing,monkeypatch,tmp_path):
    primary=assignment();configure(routing,primary)
    async def fake(route,texts):return [[1.,0.,0.] for _ in texts]
    monkeypatch.setattr(semantic,'embed',fake)
    await semantic.index_embeddings(index)
    note=index.root/'Projects/Lid.md'
    outside=tmp_path/'private.md';outside.write_text('outside')
    note.unlink();note.symlink_to(outside)
    result=await semantic.search(index,'lid',mode='semantic')
    assert all(row['rel_path']!='Projects/Lid.md' for row in result['matches'])
