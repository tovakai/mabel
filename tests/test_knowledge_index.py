"""Deterministic Phase 1, disposable vaults, no model or GPU dependencies."""
import asyncio
import json
import os
from pathlib import Path
from threading import Event, Thread
import time

import pytest
from src.knowledge_engine.index import VaultIndex, relative, filesystem_id
from src.knowledge_engine.markdown import parse
from src.knowledge_engine.__main__ import watch


@pytest.fixture
def vault(tmp_path,monkeypatch):
    root=tmp_path/'vault';root.mkdir()
    monkeypatch.setenv("MABEL_VAULT_ROOT",str(root))
    def write(name,text):
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
    write('Projects/Lid.md','---\naliases: [Lock, Locket]\nowned: true\ntags: [svenska, test]\nparent: "[[Projects/Box]]"\n---\n# Mått\nBredd: 62 mm; tolerans ±0.2 mm.\n\n## Test\nSenare test fungerade. [[Box#Results|lådan]]\n![[photo.png]]\n')
    write('Projects/Box.md','# Box\nResults for the box.\n')
    write('Elsewhere/Other.md','---\nowned: false\n---\n# Other\nLink [[Locket]] and [[Missing]].\n')
    write('A/Repeated.md','One')
    write('B/Repeated.md','Two')
    write('Ambiguous.md','[[Repeated]]\n')
    write('.personal/Visible.md','Hidden-name legitimate content.\n')
    write('.stversions/Projects/Lid.md','Historical ownership.\n')
    write('.obsidian/plugins/plugin.md','internal')
    write('node_modules/test.md','generated')
    return root,tmp_path/'cache'


@pytest.fixture
def index(vault):
    root,cache=vault
    result=VaultIndex(root,owner='alice',cache_dir=cache)
    result.reconcile()
    return result


def test_initial_incremental_zero_source_mutations(index,vault):
    root,_=vault
    before={p:p.read_bytes() for p in root.rglob('*.md')}
    state=index.status()
    assert state['notes']==7 and state['health']=='ok'
    assert index.reconcile()['changed']==0
    assert {p:p.read_bytes() for p in root.rglob('*.md')}==before
    assert index.path.name=='vault_index.db' and not index.path.is_relative_to(root)
    other=VaultIndex(root,owner='alice',cache_dir=vault[1])
    assert other.status()['generation']==state['generation']


def test_typed_filters_and_unicode(index):
    hits=index.search('mått tolerans')['matches']
    assert hits[0]['rel_path']=='Projects/Lid.md'
    assert index.search('svenska')['matches']
    assert len(index.search(mode='metadata_only',property_filters={'owned':True})['matches'])==1
    assert len(index.search(mode='metadata_only',property_filters={'owned':False})['matches'])==1
    assert len(index.search(mode='metadata_only',property_filters={'owned':{'exists':False}})['matches'])==5
    assert len(index.search(mode='metadata_only',property_filters={'tags':{'in':['svenska']}})['matches'])==1
    assert index.search(mode='metadata_only',path_prefix='Projects')['matches']
    assert not index.search(mode='metadata_only',path_prefix='Project')['matches']


def test_fts_injection_is_literal_and_filters_are_parameterized(index):
    assert index.search('"; DROP TABLE notes; --')['matches']==[]
    assert index.search(mode='metadata_only',property_filters={"x') OR 1=1 --":True})['matches']==[]
    assert index.status()['notes']==7
    with pytest.raises(ValueError):
        index.search(property_filters={'x':{'sql':'1=1'}})
    with pytest.raises(ValueError):
        index.search(top_k=1000)


@pytest.mark.parametrize('value',['../x','/tmp/x','A/../../x','C:/x','A\\x','x\0y'])
def test_path_selectors_refuse_escapes(value):
    with pytest.raises(ValueError):
        relative(value)


def test_explicit_graph_alias_backlinks_ambiguity_and_line_evidence(index):
    outgoing=index.traverse('Projects/Lid.md',direction='out')['edges']
    parent=next(e for e in outgoing if e['edge_type']=='property')
    assert parent['target_path']=='Projects/Box.md' and parent['property_key']=='parent' and parent['line']==5
    assert next(e for e in outgoing if e['edge_type']=='embed')['resolution']=='unresolved'
    backlinks=index.traverse('Projects/Lid.md',direction='in')['edges']
    assert backlinks[0]['source_path']=='Elsewhere/Other.md'
    ambiguous=index.traverse('Ambiguous.md')['edges']
    assert ambiguous[0]['resolution']=='ambiguous' and ambiguous[0]['target_id'] is None
    assert index.traverse('Projects/Lid.md',edge_type='property')['edges']
    assert index.traverse('Projects/Lid.md',search_filter='parent')['edges']


def test_current_read_range_and_stale_citations(index,vault):
    result=index.read_range('Projects/Lid.md',7,9)
    assert result['text']=='# Mått\nBredd: 62 mm; tolerans ±0.2 mm.\n' and not result['stale']
    path=vault[0]/'Projects/Lid.md'
    path.write_text(path.read_text().replace('62 mm','63 mm'))
    assert index.read_range('Projects/Lid.md',8,8)['stale']
    index.reconcile()
    assert not index.read_range('Projects/Lid.md',8,8)['stale']
    with pytest.raises(ValueError):
        index.read_range('Projects/Lid.md',1,300)


def test_rename_modify_delete_reconcile(index,vault):
    root,_=vault
    original=index.search(mode='metadata_only',path_prefix='Projects/Lid.md')['matches'][0]['note_id']
    (root/'Projects/Lid.md').rename(root/'Projects/Renamed.md')
    index.reconcile()
    assert index.search(mode='metadata_only',path_prefix='Projects/Renamed.md')['matches'][0]['note_id']==original
    assert index.traverse(original)['edges']
    (root/'Projects/Renamed.md').unlink()
    index.reconcile()
    assert index.status()['notes']==6
    assert not index.search('mått')['matches']


def test_unique_hash_rename_and_duplicate_hash_ambiguity(index,vault):
    root,_=vault
    old=index.search(mode='metadata_only',path_prefix='A/Repeated.md')['matches'][0]['note_id']
    (root/'A/Repeated.md').unlink()
    (root/'Replacement.md').write_text('One')
    index.reconcile()
    assert index.search(mode='metadata_only',path_prefix='Replacement.md')['matches'][0]['note_id']==old


def test_symlinks_do_not_leak_and_cache_cannot_live_in_vault(index,vault,tmp_path):
    root,cache=vault
    outside=tmp_path/'private.md';outside.write_text('secretword')
    (root/'escape.md').symlink_to(outside)
    (root/'escape-dir').symlink_to(tmp_path,target_is_directory=True)
    index.reconcile()
    assert not index.search('secretword')['matches']
    with pytest.raises(ValueError):
        VaultIndex(root,cache_dir=root/'cache')
    synced=tmp_path/'synced';synced.mkdir();(synced/'.stfolder').mkdir()
    with pytest.raises(ValueError):
        VaultIndex(root,cache_dir=synced/'cache')
    with pytest.raises(ValueError):
        index.read('../private.md')


def test_atomic_rollback_after_interrupted_update(index,vault,monkeypatch):
    before=index.status()['generation']
    (vault[0]/'New.md').write_text('newword')
    monkeypatch.setattr(index,'resolve_edges',lambda db:(_ for _ in ()).throw(RuntimeError('interrupt')))
    with pytest.raises(RuntimeError):
        index.reconcile()
    assert index.status()['generation']==before
    assert not index.search('newword')['matches']


def test_hub_suppression_and_bounded_context(index,vault):
    root,_=vault
    for n in range(310):
        (root/f'hub-{n}.md').write_text('[[Projects/Box]]')
    (root/'Projects/Lid.md').write_text('Lid unique_query [[Projects/Box]] [[.personal/Visible]]')
    index.reconcile()
    links=index.traverse('Projects/Lid.md',direction='out')['edges']
    assert links[0]['target_path']=='.personal/Visible.md'
    page=index.traverse('Projects/Box.md',direction='in',limit=5)
    assert len(page['edges'])==5 and page['omitted']>=305
    result=index.search('unique_query')
    assert len(result['graph_context'])<=5


def test_markdown_fences_malformed_frontmatter_and_line_ranges():
    source='---\nowned: true\nowned: false\n---\n# Header\n' + chr(96)*3+'\n# not heading [[Fake]]\n'+chr(96)*3+'\n## Real\n' + ('Line sentence. '*12+'\n\n')*30
    parsed=parse(source)
    assert parsed['errors'] and not parsed['properties']
    assert not parsed['edges']
    lines=source.splitlines()
    for chunk in parsed['chunks']:
        assert chunk['text']=='\n'.join(lines[chunk['start_line']-1:chunk['end_line']])
        assert len(chunk['text'])<=3200
    recursive=parse('---\nx: &x [*x]\n---\nBody')
    assert recursive['errors']


def test_portable_polling_watcher_reconciles_missed_updates(index,vault):
    stop=Event()
    thread=Thread(target=watch,args=(index,),kwargs={'interval':0.02,'reconcile_seconds':0.06,'stop':stop})
    thread.start()
    try:
        (vault[0]/'Observed.md').write_text('watchword')
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and not index.search('watchword')['matches']:
            time.sleep(.02)
        assert index.search('watchword')['matches']
    finally:
        stop.set();thread.join(3)
    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_production_dispatch_advertisement_authority_and_scope(vault,monkeypatch):
    from src import agent_tools,tool_execution
    from src.knowledge_engine.tools import schemas
    from src.agent_runtime.authority import create_request_authority,RequestAuthority
    from src.tool_types import ToolBlock,TOOL_TAGS
    from src.tool_capabilities import ToolRunSecurityContext
    from src.turn_contract import resolve_turn_contract,bind_turn_contract
    from src.tool_policy import ToolPolicy
    root,cache=vault
    monkeypatch.setenv('XDG_CACHE_HOME',str(cache))
    monkeypatch.setattr(tool_execution,'owner_is_admin_or_single_user',lambda _:True)
    auth=create_request_authority('Inspect this vault',owner='alice',session_id='s',workspace=str(root),capabilities={'workspace_read'},interactive_review=True)
    contract=resolve_turn_contract(capabilities={'workspace_read'},schemas=schemas(),policy=ToolPolicy())
    assert 'vault_knowledge_search' in contract.offered and 'vault_knowledge_search' in TOOL_TAGS
    async def call(authority,path=''):
        with bind_turn_contract(contract):
            return await tool_execution.execute_tool_block(ToolBlock('vault_knowledge_search',json.dumps({'query':'tolerans','path':path})),
                owner='alice',session_id='s',workspace=str(root),request_authority=authority,security_context=ToolRunSecurityContext())
    _,result=await call(auth)
    assert result['exit_code']==0,result
    assert json.loads(result['output'])['matches'][0]['rel_path']=='Projects/Lid.md'
    _,denied=await call(RequestAuthority.empty(owner='alice',session_id='s',workspace=str(root)))
    assert denied['blocked']
    _,escape=await call(auth,'../')
    assert escape['exit_code']!=0


def test_graph_cached_evidence_refuses_replaced_symlink(index,vault,tmp_path):
    root,_=vault
    source=root/'Projects/Lid.md'
    outside=tmp_path/'outside.md';outside.write_text('Private outside contents')
    source.unlink();source.symlink_to(outside)
    with pytest.raises(ValueError):index.traverse('Projects/Lid.md')
    result=index.traverse('Projects/Box.md',direction='in')
    assert result['edges']==[] and result['unavailable_edges']>=1
    assert not index.search('tolerans')['matches']


def test_additive_v1_migration_preserves_notes_and_source_hashes(index,vault):
    with index.connect() as db:
        before=[tuple(r) for r in db.execute('SELECT id,path,content_hash FROM notes ORDER BY path')]
        db.execute('DROP TABLE chunk_vectors');db.execute('DROP TABLE embedding_spaces')
        db.execute('PRAGMA user_version=1')
    restored=VaultIndex(vault[0],owner='alice',cache_dir=vault[1])
    with restored.connect() as db:
        assert [tuple(r) for r in db.execute('SELECT id,path,content_hash FROM notes ORDER BY path')]==before
    assert restored.status()['schema_version']==3



@pytest.mark.parametrize('device,inode', [
    (2**63 + 17, 2**64 + 23),
    (2**104 + 91, 2**129 + 31),
    (-2**90 - 7, 2**96 + 5),
])
def test_oversized_filesystem_ids_survive_sqlite_and_rename(index,vault,monkeypatch,device,inode):
    from types import SimpleNamespace
    root,_ = vault
    original_read = index.read
    tracked = {'Projects/Lid.md', 'Projects/Renamed.md'}
    def read_with_wide_ids(path):
        text, resource = original_read(path)
        if path in tracked:
            resource = SimpleNamespace(identity=SimpleNamespace(device=device, inode=inode))
        return text, resource
    monkeypatch.setattr(index, 'read', read_with_wide_ids)
    original_id = index.search(mode='metadata_only',path_prefix='Projects/Lid.md')['matches'][0]['note_id']
    # Same content, different filesystem ID must update the identity in place.
    assert index.reconcile()['changed'] == 0
    with index.connect() as db:
        row = db.execute('SELECT device,inode,typeof(device),typeof(inode) FROM notes WHERE path=?',
            ('Projects/Lid.md',)).fetchone()
        assert tuple(row) == (filesystem_id(device), filesystem_id(inode), 'text', 'text')
    (root/'Projects/Lid.md').rename(root/'Projects/Renamed.md')
    index.reconcile()
    renamed = index.search(mode='metadata_only',path_prefix='Projects/Renamed.md')['matches'][0]
    assert renamed['note_id'] == original_id
    assert index.status()['health'] == 'ok'


def test_v2_integer_filesystem_ids_migrate_without_rebuilding_notes_or_vectors(index,vault,monkeypatch):
    root,cache = vault
    # Ambiguous same-content notes require filesystem identity to preserve IDs
    # on rename; a content-hash-only fallback cannot disambiguate them.
    (root/'A/Copy.md').write_text('One',encoding='utf-8')
    index.reconcile()
    original = index.search(mode='metadata_only',path_prefix='A/Repeated.md')['matches'][0]['note_id']
    # Legacy v2 only supported signed 64-bit identifiers. Use synthetic values
    # so this setup also runs on Windows, where the actual IDs may overflow.
    legacy_device, legacy_inode = 12345, 67890
    with index.connect() as db:
        preserved = [tuple(r) for r in db.execute('SELECT id,path,content_hash FROM notes ORDER BY path')]
        chunk = db.execute('SELECT id,content_hash FROM chunks WHERE note_id=? LIMIT 1',
            (original,)).fetchone()
        db.execute('INSERT INTO embedding_spaces VALUES(?,?,?,?,?)',
            ('test-space','test-model','revision-1',1,'l2'))
        db.execute('INSERT INTO chunk_vectors VALUES(?,?,?,?)',
            (chunk['id'],'test-space',chunk['content_hash'],b'\x00\x00\x80?'))
        # Recreate the v2 numeric representation.  Fresh v3 databases use TEXT
        # affinity, so these numeric values are stored as numeric text here.
        db.execute('UPDATE notes SET device=?,inode=? WHERE path=?',
            (legacy_device,legacy_inode,'A/Repeated.md'))
        db.execute('PRAGMA user_version=2')
    restored = VaultIndex(root,owner='alice',cache_dir=cache)
    assert restored.status()['schema_version'] == 3
    with restored.connect() as db:
        assert [tuple(r) for r in db.execute('SELECT id,path,content_hash FROM notes ORDER BY path')] == preserved
        row = db.execute('SELECT device,inode,typeof(device),typeof(inode) FROM notes WHERE path=?',
            ('A/Repeated.md',)).fetchone()
        assert tuple(row) == (filesystem_id(legacy_device),filesystem_id(legacy_inode),'text','text')
        assert db.execute('SELECT embedding FROM chunk_vectors WHERE chunk_id=?',
            (chunk['id'],)).fetchone()[0] == b'\x00\x00\x80?'
    from types import SimpleNamespace
    original_read = restored.read
    def read_with_legacy_identity(path):
        text,resource = original_read(path)
        if path == 'A/Renamed.md':
            resource = SimpleNamespace(identity=SimpleNamespace(device=legacy_device,inode=legacy_inode))
        return text,resource
    monkeypatch.setattr(restored,'read',read_with_legacy_identity)
    (root/'A/Repeated.md').rename(root/'A/Renamed.md')
    restored.reconcile()
    assert restored.search(mode='metadata_only',path_prefix='A/Renamed.md')['matches'][0]['note_id'] == original


def test_schema3_filesystem_identifiers_are_text_on_new_index(vault):
    root,cache = vault
    fresh = VaultIndex(root,owner='new-index',cache_dir=cache)
    fresh.reconcile()
    with fresh.connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert [tuple(row) for row in db.execute(
            'SELECT DISTINCT typeof(device),typeof(inode) FROM notes')] == [('text','text')]

def test_frontmatter_only_chunk_preserves_actual_source_lines():
    source='---\nowned: true\n---'
    parsed=parse(source)
    assert parsed['chunks'][0]['text']==source


def test_large_collection_metadata_pages_exclude_history_and_preserve_sources(tmp_path):
    root=tmp_path/'large';root.mkdir()
    before={}
    for number in range(223):
        property_line=['owned: true','owned: false','other: value','owned: unexpected'][number%4]
        file=root/f'Film-{number:03d}.md'
        file.write_text('---\n'+property_line+'\ntags: [svenska, English]\n---\n# Mått\nBredd 62 mm.\n', encoding='utf-8')
        before[file.name]=file.read_bytes()
    history=root/'.stversions';history.mkdir();(history/'Film-000.md').write_text('owned history')
    index=VaultIndex(root,cache_dir=tmp_path/'index-cache');state=index.reconcile()
    assert state['notes']==223 and state['health']=='ok'
    collected=[];offset=0
    while True:
        page=index.search(mode='metadata_only',top_k=50,offset=offset)
        collected.extend(page['matches'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    assert len({row['note_id'] for row in collected})==223
    values=[row['frontmatter'].get('owned') for row in collected]
    assert sum(v is True for v in values)==56
    assert sum(v is False for v in values)==56
    assert sum(v is None for v in values)==56
    assert values.count('unexpected')==55
    assert before=={file.name:file.read_bytes() for file in root.glob('*.md')}


def test_cache_rejects_actual_git_working_tree(tmp_path):
    root=tmp_path/'vault';root.mkdir()
    repo=tmp_path/'repository';(repo/'.git').mkdir(parents=True)
    (repo/'.git/HEAD').write_text('ref: refs/heads/test')
    with pytest.raises(ValueError,match='Git working trees'):
        VaultIndex(root,cache_dir=repo/'cache')


def test_cli_scan_search_graph_read_and_benchmark_without_models(vault):
    import subprocess,sys
    root,cache=vault
    before={str(p):p.read_bytes() for p in root.rglob('*.md')}
    command=[sys.executable,'-m','src.knowledge_engine','--vault',str(root),'--cache-dir',str(cache),'--owner','synthetic-cli']
    results=[]
    for arguments in [['scan'],['status'],['search','tolerans'],['graph','Projects/Lid.md'],
            ['read','Projects/Lid.md','--start','8','--end','8'],['benchmark'],['embedding-coverage']]:
        output=subprocess.run(command+arguments,capture_output=True,text=True,timeout=15,check=True)
        results.append(json.loads(output.stdout))
    assert results[0]['notes']==7 and results[1]['health']=='ok'
    assert results[2]['matches'][0]['rel_path']=='Projects/Lid.md'
    assert results[3]['edges'] and '62 mm' in results[4]['text']
    assert results[5]['gpu_benchmark']=='not_run' and results[6]==[]
    assert before=={str(p):p.read_bytes() for p in root.rglob('*.md')}
