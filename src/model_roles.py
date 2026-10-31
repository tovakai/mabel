"""Shared role routing over existing settings, endpoint registry and inference.

No credentials, servers or primary assignments are duplicated here. Policy
entries extend the existing per-user settings; legacy primary keys remain owners.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
import json
import time
from typing import Any
import httpx

ROLE_CAPABILITIES = {'default':'chat','utility':'chat','task':'chat','research':'chat',
    'teacher':'chat','vision':'vision','image':'image','tts':'speech','stt':'transcription',
    'embedding':'embedding','reranking':'reranking'}
ROLE_LABELS = {'default':'Primary assistant','utility':'Utility (memory and summaries)',
    'task':'Scheduled tasks','research':'Research / Dottie','teacher':'Teacher',
    'vision':'Vision','image':'Image generation','tts':'Text to speech','stt':'Transcription',
    'embedding':'Knowledge embeddings','reranking':'Knowledge reranking'}
POLICY_KEY = 'model_role_policies'
RECOVERABLE = frozenset({404,408,409,425,429,500,502,503,504,529})
_HEALTH = {}
_LAST = {}


@dataclass(frozen=True)
class Assignment:
    endpoint_id: str
    model: str
    adapter: str = 'existing'
    capabilities: tuple[str,...] = ()
    space_id: str = ''
    revision: str = ''
    dimensions: int = 0
    normalization: str = 'l2'
    max_context: int = 16384


@dataclass
class Route:
    assignment: Assignment
    url: str
    headers: dict = field(repr=False,default_factory=dict)


class RoleUnavailable(RuntimeError):
    pass


class CapabilityUnavailable(RoleUnavailable):
    """Configured adapter cannot produce the role's required output."""


def setting(key,owner='',default=None):
    from src.settings import get_user_setting
    from routes.prefs_routes import _load_for_user, _load
    # Background/auth-disabled routing must never borrow a named user's
    # endpoint choices or fallback consent from the legacy first-user view.
    prefs=_load_for_user(owner) if owner else {k:v for k,v in _load().items() if k!='_users'}
    return prefs[key] if key in prefs else get_user_setting(key,owner,default)


def policies(owner=''):
    value=setting(POLICY_KEY,owner,{})
    return value if isinstance(value,dict) else {}


def get_policy(role,owner=''):
    if role not in ROLE_CAPABILITIES:
        raise ValueError('Unknown model role')
    value=policies(owner).get(role,{})
    value=value if isinstance(value,dict) else {}
    result=dict(value)
    for key,default,low,high in [('timeout',30,1,300),('retries',0,0,2),('cooldown',30,1,600),('failure_threshold',1,1,5)]:
        item=value.get(key,default)
        result[key]=item if type(item) is int and low<=item<=high else default
    return result


def primary_assignment(role,owner='',policy=None):
    from src.settings import get_user_setting
    policy=policy if policy is not None else get_policy(role,owner)
    ep=setting(role+'_endpoint_id',owner,'') or ''
    model=setting(role+'_model',owner,'') or ''
    if not ep and role in {'tts','stt'}:
        provider=setting(role+'_provider',owner,'')
        if isinstance(provider,str) and provider.startswith('endpoint:'):
            ep=provider.split(':',1)[1]
    metadata=policy.get('primary_metadata',{})
    if policy.get('primary_disabled'):
        ep=model=''
    if not isinstance(metadata,dict):
        raise ValueError('Invalid role adapter metadata')
    return Assignment(str(ep),str(model),**metadata)


def resolve(assignment,owner='',required=None):
    if assignment.adapter=='fastembed_cached':
        if required not in (None,'embedding') or 'embedding' not in assignment.capabilities:
            raise CapabilityUnavailable('Cached FastEmbed is an embedding adapter')
        return Route(assignment,'local://fastembed')
    from src.endpoint_resolver import resolve_endpoint_by_id
    if assignment.endpoint_id:
        raw=resolve_endpoint_by_id(assignment.endpoint_id,assignment.model,owner=owner,require_exact_model=True)
    elif assignment.model:
        from src.ai_interaction import _resolve_model
        try:
            raw=_resolve_model(assignment.model,owner=owner)
        except (ValueError,RuntimeError):
            raw=None
    else:
        raw=None
    if not raw or not raw[0] or not raw[1]:
        raise RoleUnavailable('Configured endpoint/model is unavailable')
    if required=='tools' and assignment.endpoint_id:
        from core.database import SessionLocal,ModelEndpoint
        db=SessionLocal()
        try:
            row=db.query(ModelEndpoint).filter(ModelEndpoint.id==assignment.endpoint_id).first()
            if row is not None and row.supports_tools is False:
                raise CapabilityUnavailable('Endpoint explicitly disables native tool calling')
        finally:
            db.close()
    if required in {'embedding','reranking','tools','vision','image','speech','transcription'} and required not in assignment.capabilities:
        # Existing endpoint tool metadata is authoritative where available.
        if required=='tools' and assignment.endpoint_id:
            from core.database import SessionLocal,ModelEndpoint
            db=SessionLocal()
            try:
                row=db.query(ModelEndpoint).filter(ModelEndpoint.id==assignment.endpoint_id).first()
                if not row or row.supports_tools is not True:
                    raise CapabilityUnavailable('Research requires declared native tool-call support')
            finally:
                db.close()
        else:
            raise CapabilityUnavailable('The selected role requires explicit adapter/capability metadata')
    return Route(assignment,raw[0],raw[2] or {})


def validate_assignment(value,role,owner='',*,allow_empty=False,required=None):
    if value is None and allow_empty:
        return None
    if not isinstance(value,dict) or set(value)-set(Assignment.__dataclass_fields__):
        raise ValueError('Use a configured endpoint/model and declared adapter metadata')
    assignment=Assignment(**{**value,'capabilities':tuple(value.get('capabilities',()))})
    if not isinstance(assignment.endpoint_id,str) or len(assignment.endpoint_id)>512:
        raise ValueError('Invalid configured endpoint identifier')
    if any(not isinstance(v,str) or len(v)>512 for v in (assignment.space_id,assignment.revision)):
        raise ValueError('Embedding space identity/revision must be bounded strings')
    if not isinstance(assignment.model,str) or not assignment.model or len(assignment.model)>512:
        raise ValueError('Model is required')
    if assignment.adapter not in {'existing','openai_tools','openai_embeddings','ollama_embed','rerank','tei_rerank','fastembed_cached','openai_image','openai_tts','openai_stt'}:
        raise ValueError('Unsupported model adapter')
    capability=required or ROLE_CAPABILITIES[role]
    if any(not isinstance(c,str) or c not in set(ROLE_CAPABILITIES.values())|{'tools'} for c in assignment.capabilities) or len(assignment.capabilities)>12:
        raise ValueError('Invalid declared model capabilities')
    if capability=='chat' and assignment.adapter not in {'existing','openai_tools'}:
        raise ValueError('A chat role requires a chat/tool-calling adapter')
    if capability not in assignment.capabilities:
        raise ValueError('Declare the model capability required by this role')
    if role=='research' and 'tools' not in assignment.capabilities:
        raise ValueError('The research worker requires declared native tool-calling capability')
    if capability=='embedding':
        if assignment.adapter not in {'openai_embeddings','ollama_embed','fastembed_cached'} or not assignment.space_id or not assignment.revision or type(assignment.dimensions) is not int or not 1<=assignment.dimensions<=8192:
            raise ValueError('Embedding adapter, stable space identity, revision and dimensions are required')
    if capability=='reranking' and assignment.adapter not in {'rerank','tei_rerank'}:
        raise ValueError('Reranking requires a scoring API adapter')
    if assignment.normalization!='l2' or type(assignment.max_context) is not int or not 1024<=assignment.max_context<=262144:
        raise ValueError('Invalid normalization/context metadata')
    # Validate ownership and exact model availability without an inference probe.
    resolve(assignment,owner,required=capability)
    if 'tools' in assignment.capabilities:
        resolve(assignment,owner,required='tools')
    return assignment


def configured(role,owner=''):
    return role in policies(owner)


def candidates(role,owner='',*,primary=None,required=None):
    policy=get_policy(role,owner)
    initial=primary or primary_assignment(role,owner,policy)
    entries=[initial]
    fallback=policy.get('fallback')
    if fallback:
        entries.append(Assignment(**{**fallback,'capabilities':tuple(fallback.get('capabilities',()))}))
    elif 'fallback' not in policy:
        from src.settings import get_user_setting
        legacy=setting(role+'_model_fallbacks',owner,[]) or []
        if legacy:
            entry=legacy[0]
            entries.append(Assignment(entry.get('endpoint_id',''),entry.get('model','')))
    return entries


def eligible(error):
    if getattr(error,'fallback_eligible',None) is False:
        return False
    if isinstance(error,CapabilityUnavailable):
        return True
    status=getattr(error,'status_code',None)
    if isinstance(error,httpx.HTTPStatusError):
        status=error.response.status_code
    return (type(status) is int and status in RECOVERABLE) or isinstance(error,(httpx.TransportError,TimeoutError))


def health_key(route):
    # Isolation by owner lives in run_candidates; no credentials in diagnostics.
    return route.url,route.assignment.model


async def run_candidates(role,entries,operation,*,owner='',required=None,policy=None,resolved_routes=None):
    policy=policy or get_policy(role,owner)
    failures=[]
    for index,assignment in enumerate(entries[:2]):
        try:
            route=resolved_routes[id(assignment)] if resolved_routes is not None else resolve(assignment,owner,required)
        except (RoleUnavailable,ValueError) as error:
            failures.append(type(error).__name__)
            continue
        key=(owner,*health_key(route))
        state=_HEALTH.get(key,{'failures':0,'until':0})
        if state['until']>time.monotonic():
            failures.append('circuit_open')
            continue
        for attempt in range(int(policy['retries'])+1):
            try:
                result=await asyncio.wait_for(operation(route),timeout=float(policy['timeout']))
                _HEALTH.pop(key,None)
                diagnostic={'role':role,'endpoint_id':assignment.endpoint_id,'model':assignment.model,
                    'fallback_active':index>0,'failover_reasons':failures,'attempts':attempt+1}
                _LAST[(owner,role)]=diagnostic
                return result,diagnostic,route
            except Exception as error:
                if not eligible(error):
                    raise
                failures.append(type(error).__name__ + (':'+str(getattr(error,'status_code','')) if getattr(error,'status_code',None) else ''))
                if getattr(error,'affects_health',True) is False:
                    break
                state={'failures':state['failures']+1,'until':0}
                if state['failures']>=int(policy['failure_threshold']):
                    state['until']=time.monotonic()+float(policy['cooldown'])
                _HEALTH[key]=state
                if len(_HEALTH)>2048:
                    _HEALTH.pop(next(iter(_HEALTH)))
                if attempt < int(policy['retries']):
                    await asyncio.sleep(min(.1*(attempt+1),.5))
        # Only inference is retried; callers own their completed action ledger.
    diagnostic={'role':role,'status':'unavailable','fallback_active':False,'failover_reasons':failures}
    _LAST[(owner,role)]=diagnostic
    raise RoleUnavailable('No usable '+role+' model; '+', '.join(failures))


async def call(role,messages,*,owner='',primary_route=None,**kwargs):
    entries=candidates(role,owner)
    resolved=None
    if primary_route and not entries[0].model:
        from dataclasses import replace
        first=replace(entries[0],model=primary_route[1])
        entries[0]=first
        resolved={id(first):Route(first,primary_route[0],primary_route[2] or {})}
        for a in entries[1:]:
            try:resolved[id(a)]=resolve(a,owner)
            except (RoleUnavailable,ValueError):continue
        entries=[a for a in entries if id(a) in resolved]
    async def operation(route):
        from src.llm_core import llm_call_async
        from src.model_role_runtime import _RUNNING
        token=_RUNNING.set(True)
        try:
            return await llm_call_async(route.url,route.assignment.model,messages,headers=route.headers,
                **{**kwargs,'timeout':get_policy(role,owner)['timeout'],'max_retries':1})
        finally:
            _RUNNING.reset(token)
    result,_,_=await run_candidates(role,entries,operation,owner=owner,resolved_routes=resolved)
    return result


def diagnostics(owner=''):
    return {role:_LAST.get((owner,role),{'role':role,'status':'not_used'}) for role in ROLE_CAPABILITIES}


def health_status(owner=''):
    """Owner-scoped health without URLs, headers, credentials or prompts."""
    clock=time.monotonic()
    return [{'model':key[2],'failures':state['failures'],
        'retry_after_seconds':max(0,round(state['until']-clock,1))}
        for key,state in list(_HEALTH.items()) if key[0]==owner][-20:]


def run_sync(role,operation,*,owner='',required=None,primary_route=None):
    """Synchronous inference adapters share the same bounded routing policy."""
    import concurrent.futures
    entries=candidates(role,owner)
    prepared={}
    if primary_route:
        from dataclasses import replace
        first=replace(entries[0],model=primary_route[1])
        entries[0]=first
        prepared[id(first)]=Route(first,primary_route[0],primary_route[2] or {})
    for assignment in entries:
        if id(assignment) not in prepared:
            try:prepared[id(assignment)]=resolve(assignment,owner,required)
            except (RoleUnavailable,ValueError):continue
    selected=[a for a in entries if id(a) in prepared]
    async def invoke():
        async def wrapped(route):
            return await asyncio.to_thread(operation,route)
        return await run_candidates(role,selected,wrapped,owner=owner,required=required,resolved_routes=prepared)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        result,diagnostic,used=asyncio.run(invoke())
        diagnostic['fallback_active']=used.assignment is not entries[0]
        _LAST[(owner,role)]=diagnostic
        return result
    # Existing sync service APIs may be invoked by async routes. Keep their
    # contract without running a second event loop on the caller's thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        result,diagnostic,used=executor.submit(lambda:asyncio.run(invoke())).result()
        diagnostic['fallback_active']=used.assignment is not entries[0]
        _LAST[(owner,role)]=diagnostic
        return result
