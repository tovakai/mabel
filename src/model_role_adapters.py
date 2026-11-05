"""Typed HTTP inference adapters sharing the role router and endpoint registry."""
from dataclasses import replace
import httpx
from . import model_roles as roles
from .endpoint_resolver import build_model_api_url


async def request(role,payload,*,suffix,owner='',primary_route=None):
    entries=roles.candidates(role,owner)
    prepared={}
    if primary_route:
        first=replace(entries[0],model=primary_route[1])
        entries[0]=first
        prepared[id(first)]=roles.Route(first,primary_route[0],primary_route[2] or {})
    async def operation(route):
        async with httpx.AsyncClient(timeout=roles.get_policy(role,owner)['timeout'],follow_redirects=False) as client:
            response=await client.post(build_model_api_url(route.url,suffix),headers=route.headers,
                json={**payload,'model':route.assignment.model})
            response.raise_for_status()
            return response
    # Resolve fallback only through the existing owner-scoped endpoint registry.
    for a in entries:
        if id(a) not in prepared:
            try:prepared[id(a)]=roles.resolve(a,owner,roles.ROLE_CAPABILITIES[role])
            except (roles.RoleUnavailable,ValueError):continue
    selected=[a for a in entries if id(a) in prepared]
    response,diagnostic,used=await roles.run_candidates(role,selected,operation,owner=owner,resolved_routes=prepared)
    diagnostic['fallback_active']=used.assignment is not entries[0]
    roles._LAST[(owner,role)]=diagnostic
    return response,used


def audio(role,*,owner='',primary_route=None,text='',audio_bytes=b'',voice='alloy',speed=1.,language='',local_primary=None):
    suffix='/audio/speech' if role=='tts' else '/audio/transcriptions'
    def operation(route):
        if local_primary is not None and route.url.startswith('local://'):
            value=local_primary()
            if not value:
                raise roles.CapabilityUnavailable('Local audio model unavailable')
            return value
        kwargs={'headers':route.headers,'timeout':roles.get_policy(role,owner)['timeout'],'follow_redirects':False}
        if role=='tts':
            kwargs['json']={'model':route.assignment.model,'input':text,'voice':voice,'speed':speed,'response_format':'mp3',
                            **({'lang_code':language} if language else {})}
        else:
            kwargs['data']={'model':route.assignment.model,**({'language':language} if language else {})}
            kwargs['files']={'file':('audio.webm',audio_bytes,'audio/webm')}
        response=httpx.post(build_model_api_url(route.url,suffix),**kwargs)
        response.raise_for_status()
        return response.content if role=='tts' else response.json().get('text','')
    return roles.run_sync(role,operation,owner=owner,required=roles.ROLE_CAPABILITIES[role],primary_route=primary_route)
