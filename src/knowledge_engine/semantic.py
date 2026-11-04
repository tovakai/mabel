"""Dense SQLite vectors, compatible spaces, RRF and optional scoring adapters."""
from __future__ import annotations
import asyncio
from dataclasses import asdict
import json
import math
import struct
import sqlite3
from urllib.parse import urlsplit,urlunsplit

import httpx
from src import model_roles
from .markdown import digest,json_value


from src.endpoint_resolver import build_model_api_url as api_url


class LocalVectorUnavailable(model_roles.RoleUnavailable):
    pass


class UnindexedSpace(model_roles.CapabilityUnavailable):
    affects_health = False


def space(assignment):
    if not assignment.space_id or not assignment.revision or type(assignment.dimensions) is not int or not 1<=assignment.dimensions<=8192 or assignment.normalization!='l2':
        raise model_roles.CapabilityUnavailable('Explicit compatible embedding space/revision/dimensions required')
    return digest(json_value([assignment.model,assignment.space_id,assignment.revision,assignment.dimensions,assignment.normalization]))


def vectors(raw,count,dimension):
    if not isinstance(raw,list) or len(raw)!=count:
        raise model_roles.CapabilityUnavailable('Embedding response has wrong batch size')
    result=[]
    for vector in raw:
        if not isinstance(vector,list) or len(vector)!=dimension or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector):
            raise model_roles.CapabilityUnavailable('Embedding dimensions/values are incompatible')
        norm=math.sqrt(sum(v*v for v in vector))
        if norm==0 or not math.isfinite(norm):
            raise model_roles.CapabilityUnavailable('Embedding response contains zero vectors')
        result.append([float(v/norm) for v in vector])
    return result


async def embed(route,texts):
    a=route.assignment
    space(a)
    if a.adapter=='fastembed_cached':
        from src.embeddings import FastEmbedClient
        client=await asyncio.to_thread(FastEmbedClient,a.model,local_files_only=True)
        raw=await asyncio.to_thread(client.encode,texts)
        return vectors(raw.tolist(),len(texts),a.dimensions)
    if a.adapter not in {'openai_embeddings','ollama_embed'}:
        raise model_roles.CapabilityUnavailable('Use a compatible embedding adapter')
    url=api_url(route.url,'/api/embed' if a.adapter=='ollama_embed' else '/embeddings')
    async with httpx.AsyncClient(timeout=30,follow_redirects=False) as client:
        response=await client.post(url,headers=route.headers,json={'model':a.model,'input':texts})
        response.raise_for_status()
        try:
            body=response.json()
        except ValueError:
            raise model_roles.CapabilityUnavailable('Embedding endpoint did not return JSON')
    try:
        raw=body['embeddings'] if a.adapter=='ollama_embed' else [r['embedding'] for r in sorted(body['data'],key=lambda r:r['index'])]
    except (KeyError,TypeError,ValueError):
        raise model_roles.CapabilityUnavailable('Malformed embedding response')
    return vectors(raw,len(texts),a.dimensions)


def ensure_schema(index):
    # VaultIndex owns additive schema migration, independent of sqlite-vec.
    with index.connect() as db:
        if db.execute('PRAGMA user_version').fetchone()[0] < 2:
            raise ValueError('Upgrade the disposable index schema first')


def coverage(index):
    ensure_schema(index)
    with index.connect() as db:
        return [dict(r) for r in db.execute("""SELECT s.*,count(c.id) AS embedded,
            (SELECT count(*) FROM chunks) AS total_chunks FROM embedding_spaces s
            LEFT JOIN chunk_vectors v ON v.space_id=s.id
            LEFT JOIN chunks c ON c.id=v.chunk_id AND c.content_hash=v.chunk_hash GROUP BY s.id""")]


async def index_embeddings(index,*,owner='',limit=1000,batch_size=8):
    if type(limit) is not int or not 1<=limit<=10000 or type(batch_size) is not int or not 1<=batch_size<=64:
        raise ValueError('Invalid embedding maintenance bounds')
    ensure_schema(index)
    generated=0
    diagnostics=[]
    while generated<limit:
        async def operation(route):
            sid=space(route.assignment)
            with index.connect() as db:
                rows=db.execute("""SELECT c.id,c.text,c.heading,c.content_hash,n.title FROM chunks c JOIN notes n ON n.id=c.note_id
                    LEFT JOIN chunk_vectors v ON v.chunk_id=c.id AND v.space_id=? AND v.chunk_hash=c.content_hash
                    WHERE v.chunk_id IS NULL ORDER BY n.path,c.start_line LIMIT ?""",(sid,min(batch_size,limit-generated))).fetchall()
            if not rows:
                return 0
            texts=[r['title']+'\n'+' / '.join(json.loads(r['heading']))+'\n'+r['text'] for r in rows]
            encoded=await embed(route,texts)
            with index.connect() as db:
                db.execute('INSERT OR IGNORE INTO embedding_spaces VALUES(?,?,?,?,?)',
                    (sid,route.assignment.space_id,route.assignment.revision,route.assignment.dimensions,route.assignment.normalization))
                for row,vector in zip(rows,encoded):
                    # A concurrent reindex may have deleted/replaced this chunk.
                    current=db.execute('SELECT content_hash FROM chunks WHERE id=?',(row['id'],)).fetchone()
                    if current and current[0]==row['content_hash']:
                        db.execute('INSERT OR REPLACE INTO chunk_vectors VALUES(?,?,?,?)',
                            (row['id'],sid,row['content_hash'],struct.pack('<'+'f'*len(vector),*vector)))
            return len(rows)
        count,diag,_=await model_roles.run_candidates('embedding',model_roles.candidates('embedding',owner),operation,
            owner=owner,required='embedding')
        diagnostics.append(diag)
        generated+=count
        if not count:
            break
    return {'generated':generated,'coverage':coverage(index),'routes':diagnostics[-2:],
        'bounded':generated>=limit}


async def dense_search(index,query,*,owner='',property_filters=None,path_prefix='',top_k=15):
    ensure_schema(index)
    async def operation(route):
        sid=space(route.assignment)
        where,params=index._filters(property_filters or {},path_prefix)
        with index.connect() as db:
            count=db.execute(f"""SELECT count(*) FROM chunk_vectors v JOIN chunks c ON c.id=v.chunk_id
                JOIN notes n ON n.id=c.note_id WHERE v.space_id=? AND {where}""",(sid,*params)).fetchone()[0]
        if not count:
            raise UnindexedSpace('No compatible vector index; lexical retrieval remains available')
        if count>50000:
            raise model_roles.CapabilityUnavailable('Dense scan budget exceeded; narrow metadata/path filters')
        with index.connect() as db:
            try:
                import sqlite_vec
                db.enable_load_extension(True)
                try:
                    sqlite_vec.load(db)
                finally:
                    db.enable_load_extension(False)
            except (ImportError,AttributeError,RuntimeError,sqlite3.Error,OSError):
                raise LocalVectorUnavailable('sqlite-vec extension unavailable; lexical retrieval remains available')
            query_vector=(await embed(route,[query]))[0]
            raw=struct.pack('<'+'f'*len(query_vector),*query_vector)
            rows=db.execute(f"""SELECT c.*,n.path,n.title,n.content_hash AS note_hash,
                vec_distance_cosine(v.embedding,?) AS distance
                FROM chunk_vectors v JOIN chunks c ON c.id=v.chunk_id JOIN notes n ON n.id=c.note_id
                WHERE v.space_id=? AND v.chunk_hash=c.content_hash AND {where}
                ORDER BY distance,n.path,c.start_line LIMIT ?""",(raw,sid,*params,top_k)).fetchall()
        return [{'chunk_id':r['id'],'note_id':r['note_id'],'rel_path':r['path'],'title':r['title'],
            'start_line':r['start_line'],'end_line':r['end_line'],'excerpt':r['text'][:2000],
            'heading':json.loads(r['heading']),'content_hash':r['note_hash'],'distance':r['distance'],
            'space_id':sid,'excerpt_truncated':len(r['text'])>2000} for r in rows
            if index.source_accessible(r['path'])]
    result,diag,_=await model_roles.run_candidates('embedding',model_roles.candidates('embedding',owner),operation,
        owner=owner,required='embedding')
    return result,diag


def fuse(*rankings,k=60):
    scores,records={},{}
    for ranking in rankings:
        seen=set()
        for rank,row in enumerate(ranking,1):
            key=row['chunk_id']
            if key in seen:
                continue
            seen.add(key)
            records.setdefault(key,row)
            scores[key]=scores.get(key,0)+1/(k+rank)
    return [{**records[key],'fusion_score':score} for key,score in sorted(scores.items(),key=lambda pair:(-pair[1],pair[0]))]


async def rerank(query,candidates,*,owner=''):
    async def operation(route):
        a=route.assignment
        if a.adapter not in {'rerank','tei_rerank'}:
            raise model_roles.CapabilityUnavailable('Use an explicit rerank scoring adapter')
        documents=[c['excerpt'] for c in candidates]
        payload={'query':query,'texts':documents} if a.adapter=='tei_rerank' else {'model':a.model,'query':query,'documents':documents,'top_n':len(documents)}
        url=api_url(route.url,'/rerank')
        if a.adapter=='tei_rerank':
            parsed=urlsplit(url)
            url=urlunsplit((parsed.scheme,parsed.netloc,parsed.path.replace('/v1/rerank','/rerank'),'',''))
        async with httpx.AsyncClient(timeout=30,follow_redirects=False) as client:
            response=await client.post(url,headers=route.headers,json=payload)
            response.raise_for_status()
            try:
                body=response.json()
            except ValueError:
                raise model_roles.CapabilityUnavailable('Reranker did not return JSON')
        rows=body if a.adapter=='tei_rerank' else body.get('results')
        if not isinstance(rows,list) or len(rows)!=len(candidates):
            raise model_roles.CapabilityUnavailable('Reranker must return a complete candidate permutation')
        try:
            order=sorted(rows,key=lambda row:-float(row.get('relevance_score',row.get('score'))))
            ids=[row['index'] for row in order]
            if any(type(i) is not int for i in ids) or set(ids)!=set(range(len(candidates))) or any(not math.isfinite(float(r.get('relevance_score',r.get('score')))) for r in rows):
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise model_roles.CapabilityUnavailable('Malformed reranker scores/indices')
        # Only rank order is normalized; never mix raw scores between models.
        return [candidates[i] for i in ids]
    result,diag,_=await model_roles.run_candidates('reranking',model_roles.candidates('reranking',owner),operation,
        owner=owner,required='reranking')
    return result,diag


async def search(index,query='',*,mode='hybrid',property_filters=None,path_prefix='',top_k=8,offset=0,use_reranking=False,owner='',fields=None,sort_by=None):
    if mode=='metadata_only':
        # The index validates compact listing bounds (titles-only pages may reach 200).
        return await asyncio.to_thread(index.search,query,mode=mode,property_filters=property_filters,path_prefix=path_prefix,
                                       top_k=top_k,offset=offset,fields=fields,sort_by=sort_by)
    if fields is not None or sort_by is not None:
        raise ValueError('fields and sort_by apply to metadata_only searches')
    if mode not in {'semantic','hybrid','lexical','metadata_only'} or type(top_k) is not int or not 1<=top_k<=50 or type(offset) is not int or not 0<=offset<=10000:
        raise ValueError('Invalid retrieval mode/page bounds')
    if mode=='lexical':
        return await asyncio.to_thread(index.search,query,mode=mode,property_filters=property_filters,path_prefix=path_prefix,top_k=top_k,offset=offset)
    if offset:
        raise ValueError('Ranked semantic search is bounded to top_k; refine filters rather than using unstable offsets')
    lexical=await asyncio.to_thread(index.search,query,top_k=max(15,top_k),property_filters=property_filters,path_prefix=path_prefix)
    status={}
    try:
        dense,diag=await dense_search(index,query,owner=owner,property_filters=property_filters,path_prefix=path_prefix,top_k=max(15,top_k))
        status['embedding']=diag
        ranked=fuse(lexical['matches'],dense) if mode=='hybrid' else dense
    except (model_roles.RoleUnavailable,httpx.HTTPError,ValueError,sqlite3.Error) as error:
        ranked=lexical['matches']
        status['embedding']={'status':'degraded','reason':type(error).__name__,'mode':'lexical'}
    if use_reranking and ranked:
        count=min(15,len(ranked))
        try:
            first,diag=await rerank(query,ranked[:count],owner=owner)
            ranked=first+ranked[count:]
            status['reranking']=diag
        except (model_roles.RoleUnavailable,httpx.HTTPError,ValueError,sqlite3.Error) as error:
            status['reranking']={'status':'degraded','reason':type(error).__name__,'used':'fused_order'}
    return {**lexical,'matches':ranked[:top_k],'mode':mode,'services':status,'next_offset':None,
        'embedding_coverage':coverage(index)}
