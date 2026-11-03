"""Extend Settings' existing assignments; no independent provider registry."""
from dataclasses import asdict
from fastapi import APIRouter, HTTPException, Request
from src.auth_helpers import get_current_user
from src import model_roles as roles


def setup_model_role_routes():
    router=APIRouter(prefix='/api/model-roles',tags=['settings'])

    @router.get('')
    async def snapshot(request:Request):
        user=get_current_user(request)
        result=[]
        for role,capability in roles.ROLE_CAPABILITIES.items():
            policy=roles.get_policy(role,user or '')
            try:
                primary=asdict(roles.primary_assignment(role,user or '',policy))
                entries=roles.candidates(role,user or '')
                fallback=asdict(entries[1]) if len(entries)>1 else None
                error=None
            except (ValueError,TypeError):
                primary=None;fallback=None;error='Invalid saved model metadata'
            result.append({'role':role,'label':roles.ROLE_LABELS[role],'required_capability':capability,
                'primary':primary,'policy':policy,'diagnostics':roles.diagnostics(user or '')[role],
                'configuration_error':error,
                'effective_fallback':fallback})
        return {'version':1,'roles':result,'health':roles.health_status(user or '')}

    @router.put('/{role}')
    async def update(role:str,request:Request,body:dict):
        from src.agent_runtime.authority import require_user_approval_request
        from src.auth_helpers import is_delegated_credential
        require_user_approval_request(request)
        if is_delegated_credential(request):
            raise HTTPException(403,'Model role settings require an interactive human session')
        if role not in roles.ROLE_CAPABILITIES or set(body)-{'primary','fallback','timeout','retries','cooldown','failure_threshold','primary_metadata','limits'}:
            raise HTTPException(400,'Unknown model role or configuration field')
        user=get_current_user(request)
        try:
            primary=roles.validate_assignment(body['primary'],role,user or '') if 'primary' in body and body['primary'] is not None else None
            fallback=roles.validate_assignment(body['fallback'],role,user or '',allow_empty=True) if 'fallback' in body else None
            if 'limits' in body:
                bounds={'max_calls':(8,64),'max_concurrency':(1,6),'max_hops':(0,3),
                    'max_chunks':(1,128),'max_bytes':(4096,100000),'max_seconds':(5,300)}
                value=body['limits']
                if role!='research' or not isinstance(value,dict) or set(value)-set(bounds):
                    raise ValueError('Research limits must be a structured research-role configuration')
                for key,item in value.items():
                    if type(item) is not int or not bounds[key][0]<=item<=bounds[key][1]:
                        raise ValueError('Invalid research limit')
            for key,low,high in [('timeout',1,300),('retries',0,2),('cooldown',1,600),('failure_threshold',1,5)]:
                if key in body and (type(body[key]) is not int or not low<=body[key]<=high):
                    raise ValueError('Invalid '+key)
        except (ValueError,TypeError,roles.RoleUnavailable) as error:
            raise HTTPException(400,str(error)) from error
        from routes.prefs_routes import _load_for_user,_save_for_user
        prefs=_load_for_user(user)
        policies=dict(prefs.get(roles.POLICY_KEY) or {})
        policy=dict(policies.get(role) or {})
        policy.update({k:body[k] for k in ('timeout','retries','cooldown','failure_threshold') if k in body})
        if 'limits' in body:
            policy['limits']=body['limits']
        if 'fallback' in body:
            policy['fallback']=asdict(fallback) if fallback else None
        if 'primary_metadata' in body:
            # Adapter metadata can be edited without overwriting the legacy
            # primary picker, including changes made while Settings was open.
            current=roles.primary_assignment(role,user or '')
            if not isinstance(body['primary_metadata'],dict):
                raise HTTPException(400,'Primary adapter metadata must be an object')
            metadata=dict(body['primary_metadata'])
            metadata.pop('endpoint_id',None)
            metadata.pop('model',None)
            try:
                roles.validate_assignment({'endpoint_id':current.endpoint_id,'model':current.model,**metadata},role,user or '')
            except (ValueError,TypeError,roles.RoleUnavailable) as error:
                raise HTTPException(400,str(error)) from error
            policy['primary_metadata']=metadata
        if 'primary' in body:
            if primary:
                prefs[role+'_endpoint_id']=primary.endpoint_id
                prefs[role+'_model']=primary.model
                policy['primary_metadata']={k:v for k,v in asdict(primary).items() if k not in {'endpoint_id','model'}}
                policy['primary_disabled']=False
            else:
                policy['primary_disabled']=True
        policies[role]=policy
        prefs[roles.POLICY_KEY]=policies
        if role=='default' and 'fallback' in body:
            # Reuse explicit foreground consent and its existing commitment
            # boundary; never restore retired default_model_fallbacks.
            prefs['foreground_fallback_enabled']=fallback is not None
            prefs['foreground_model_fallbacks']=[{'endpoint_id':fallback.endpoint_id,'model':fallback.model}] if fallback else []
        _save_for_user(user,prefs)
        return {'ok':True,'role':role,'policy':policy}

    return router
