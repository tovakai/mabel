"""Role annotations for existing inference calls, never workflow retries."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
import asyncio
import json
import time

from . import model_roles as roles

_ACTIVE=ContextVar('model_role_context',default=None)
_RUNNING=ContextVar('model_role_dispatch',default=False)


@contextmanager
def role_scope(role,owner='',required=None):
    token=_ACTIVE.set((role,owner or '',required))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def consumer(role):
    """Tag inference inside a regular async consumer; do not rerun its actions."""
    def decorate(func):
        sig=signature(func)
        @wraps(func)
        async def wrapped(*args,**kwargs):
            bound=sig.bind(*args,**kwargs)
            owner=bound.arguments.get('owner') or ''
            with role_scope(role,owner):
                return await func(*args,**kwargs)
        return wrapped
    return decorate


def stream_routes(raw,context,*,retain_cooled=False):
    """Prepare at most primary/fallback from existing owner-bound endpoints."""
    role,owner,required=context
    policy=roles.get_policy(role,owner)
    entries=roles.candidates(role,owner)
    prepared=[]
    failures=[]
    for i,assignment in enumerate(entries):
        try:
            # Preserve the active session/consumer route as the primary; role
            # defaults must never switch an already-selected conversation.
            if i==0 and raw and role!='research':
                from dataclasses import replace
                route=roles.Route(replace(assignment,endpoint_id='',model=raw[0][1]),raw[0][0],raw[0][2] or {})
            else:
                route=roles.resolve(assignment,owner,required)
            key=(owner,*roles.health_key(route))
            if roles._HEALTH.get(key,{}).get('until',0)>time.monotonic():
                failures.append('circuit_open')
                if not retain_cooled:
                    continue
            prepared.append((route,i))
        except (roles.RoleUnavailable,ValueError):
            failures.append('unavailable_or_incompatible')
    return prepared,policy,failures


def record_failure(context,route,policy):
    key=(context[1],*roles.health_key(route))
    count=roles._HEALTH.get(key,{}).get('failures',0)+1
    roles._HEALTH[key]={'failures':count,'until':time.monotonic()+policy['cooldown'] if count>=policy['failure_threshold'] else 0}
    if len(roles._HEALTH)>1024:
        roles._HEALTH.pop(next(iter(roles._HEALTH)))


def record_success(context,route,index,failures):
    roles._HEALTH.pop((context[1],*roles.health_key(route)),None)
    roles._LAST[(context[1],context[0])]={'role':context[0],'model':route.assignment.model,
        'endpoint_id':route.assignment.endpoint_id,'fallback_active':index>0,'failover_reasons':list(failures)}


def inference(func):
    sig=signature(func)
    @wraps(func)
    async def wrapped(*args,**kwargs):
        context=_ACTIVE.get()
        if context is None or _RUNNING.get() or not roles.configured(context[0],context[1]):
            return await func(*args,**kwargs)
        bound=sig.bind(*args,**kwargs)
        values=bound.arguments
        routes,policy,failures=stream_routes([(values['url'],values['model'],values.get('headers') or {})],context)
        entries=[route.assignment for route,_ in routes]
        mapping={id(route.assignment):route for route,_ in routes}
        async def operation(route):
            target=mapping[id(route.assignment)]
            call=dict(values)
            call.update(url=target.url,model=target.assignment.model,headers=target.headers,
                timeout=policy['timeout'],max_retries=1)
            token=_RUNNING.set(True)
            try:
                return await func(**call)
            finally:
                _RUNNING.reset(token)
        # Reuse the generic router, including its classified failure policy.
        # A request-local operation consumes trusted prepared routes. No global
        # monkeypatch/resolver mutation: run_candidates accepts this mapping.
        result,diagnostic,used = await roles.run_candidates(context[0],entries,operation,owner=context[1],
            required=context[2],policy=policy,resolved_routes=mapping)
        original_index = next(i for route,i in routes if route is used)
        record_success(context,used,original_index,failures + diagnostic['failover_reasons'])
        return result
    return wrapped


def streaming(func):
    @wraps(func)
    async def wrapped(candidates,messages,**kwargs):
        explicit=kwargs.pop('model_role',None)
        role_owner=kwargs.pop('role_owner','')
        context=(explicit,role_owner,None) if explicit else _ACTIVE.get()
        if context is None or not roles.configured(context[0],context[1]):
            async for chunk in func(candidates,messages,**kwargs):
                yield chunk
            return
        prepared,policy,failures=stream_routes(list(candidates or []),context,retain_cooled=True)
        if not prepared:
            yield 'event: error\ndata: '+json.dumps({'status':503,'error':'No available role endpoint','fallback_eligible':False})+'\n\n'
            return
        routes=[(r.url,r.assignment.model,r.headers) for r,_ in prepared]
        active=0
        options={**kwargs,'fallback_statuses':roles.RECOVERABLE,'fallback_on_empty':False,'timeout':policy['timeout'],
            'role_retry_precontent':policy['retries']>0}
        original_factory=kwargs.get('candidate_request_factory')
        async def factory(index,url,model,headers):
            route=prepared[index][0]
            key=(context[1],*roles.health_key(route))
            if roles._HEALTH.get(key,{}).get('until',0)>time.monotonic():
                from fastapi import HTTPException
                raise HTTPException(503,'Role endpoint cooling down')
            result=original_factory(index,url,model,headers) if original_factory else {}
            if hasattr(result,'__await__'):
                result=await result
            return result or {}
        options['candidate_request_factory']=factory
        # The existing loop owns commitment: it cannot switch after tokens or
        # native calls are emitted. Never restart an agent workflow here.
        async for chunk in func(routes,messages,**options):
            data={}
            for line in chunk.splitlines():
                if line.startswith('data: ') and line!='data: [DONE]':
                    try:data=json.loads(line[6:])
                    except ValueError:pass
            if data.get('type')=='fallback':
                for failure in data.get('failures',[]):
                    i=failure.get('candidate_index')
                    if type(i) is int and i<len(prepared):
                        if roles._HEALTH.get((context[1],*roles.health_key(prepared[i][0])),{}).get('until',0)<=time.monotonic():
                            record_failure(context,prepared[i][0],policy)
                        failures.append(str(failure.get('status')))
                active=data.get('candidate_index',active)
            if chunk.startswith('event: error'):
                if data.get('status') in roles.RECOVERABLE:
                    record_failure(context,prepared[min(active,len(prepared)-1)][0],policy)
            elif data.get('delta') or data.get('type')=='tool_calls':
                route,index=prepared[min(active,len(prepared)-1)]
                record_success(context,route,index,failures)
            yield chunk
    return wrapped


def current_owner(role):
    value=_ACTIVE.get()
    return value[1] if value is not None and value[0]==role else ''


def synchronous(func):
    sig=signature(func)
    @wraps(func)
    def wrapped(*args,**kwargs):
        context=_ACTIVE.get()
        if context is None or _RUNNING.get() or not roles.configured(context[0],context[1]):
            return func(*args,**kwargs)
        values=sig.bind(*args,**kwargs).arguments
        def operation(route):
            call=dict(values)
            call.update(url=route.url,model=route.assignment.model,headers=route.headers,
                timeout=roles.get_policy(context[0],context[1])['timeout'],max_retries=1)
            return func(**call)
        return roles.run_sync(context[0],operation,owner=context[1],required=context[2],
            primary_route=(values['url'],values['model'],values.get('headers') or {}))
    return wrapped


def sync_consumer(role):
    def decorate(func):
        sig=signature(func)
        @wraps(func)
        def wrapped(*args,**kwargs):
            owner=sig.bind(*args,**kwargs).arguments.get('owner') or ''
            with role_scope(role,owner):
                return func(*args,**kwargs)
        return wrapped
    return decorate
