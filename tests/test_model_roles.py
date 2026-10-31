import asyncio
from dataclasses import asdict
import httpx
import pytest
from src import model_roles as roles
from tests.test_knowledge_semantic import routing,assignment,configure


@pytest.mark.asyncio
async def test_optional_fallback_circuit_breaker_and_primary_recovery(routing,monkeypatch):
    p=assignment();f=assignment('secondary')
    configure(routing,p,f)
    clock=[1.]
    monkeypatch.setattr(roles.time,'monotonic',lambda:clock[0])
    calls=[];down=[True]
    async def op(route):
        calls.append(route.assignment.endpoint_id)
        if down[0] and route.assignment.endpoint_id=='primary':raise httpx.ConnectError('offline')
        return route.assignment.model
    _,diag,_=await roles.run_candidates('embedding',roles.candidates('embedding'),op)
    assert diag['fallback_active']
    await roles.run_candidates('embedding',roles.candidates('embedding'),op)
    assert calls.count('primary')==1
    down[0]=False;clock[0]+=2
    _,diag,_=await roles.run_candidates('embedding',roles.candidates('embedding'),op)
    assert not diag['fallback_active']


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[400,401,403,422])
async def test_authorization_and_arguments_do_not_fail_over(routing,status):
    configure(routing,assignment(),assignment('secondary'))
    calls=[]
    async def op(route):
        calls.append(route)
        raise httpx.HTTPStatusError('denied',request=httpx.Request('POST',route.url),response=httpx.Response(status))
    with pytest.raises(httpx.HTTPStatusError):
        await roles.run_candidates('embedding',roles.candidates('embedding'),op)
    assert len(calls)==1


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_cancellation_propagates(routing):
    configure(routing,assignment(),assignment('secondary'))
    async def op(route):
        if route.assignment.endpoint_id=='primary':raise TimeoutError('deadline')
        return 'fallback'
    value,diag,_=await roles.run_candidates('embedding',roles.candidates('embedding'),op)
    assert value=='fallback' and diag['fallback_active']
    async def cancelled(route):raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await roles.run_candidates('embedding',roles.candidates('embedding'),cancelled)


def test_legacy_migration_does_not_write_or_replace_assignments(routing):
    routing.update({'default_model':'existing Gyro-S','utility_model':'existing utility',
        'research_model':'existing researcher','vision_model':'existing vision',
        'utility_model_fallbacks':[{'endpoint_id':'old','model':'old utility'}]})
    before=dict(routing)
    for role in roles.ROLE_CAPABILITIES:
        roles.primary_assignment(role)
        roles.candidates(role)
    assert routing==before
    assert roles.primary_assignment('default').model=='existing Gyro-S'
    assert roles.candidates('utility')[1].model=='old utility'
    routing[roles.POLICY_KEY]={'utility':{'fallback':None}}
    assert len(roles.candidates('utility'))==1


@pytest.mark.parametrize('role',list(roles.ROLE_CAPABILITIES))
def test_every_role_has_independent_optional_fallback(routing,role):
    p=roles.Assignment('primary','model')
    f=roles.Assignment('secondary','fallback')
    configure(routing,p,f,role=role)
    assert [a.model for a in roles.candidates(role)]==['model','fallback']
    routing[roles.POLICY_KEY][role]['fallback']=None
    assert len(roles.candidates(role))==1


def test_ownerless_routing_never_borrows_named_user_preferences(monkeypatch):
    from routes import prefs_routes
    from src import settings
    monkeypatch.setattr(prefs_routes,'_load',lambda:{'_users':{'alice':{
        roles.POLICY_KEY:{'utility':{'fallback':{'endpoint_id':'private','model':'private'}}},
        'utility_model':'private-model'}}})
    monkeypatch.setattr(settings,'get_user_setting',lambda key,owner='',default=None:default)
    assert roles.policies('')=={}
    assert roles.setting('utility_model','','global')=='global'
    assert roles.policies('alice')['utility']['fallback']['model']=='private'


def test_auth_disabled_policy_save_preserves_named_consent(monkeypatch):
    from routes import prefs_routes
    from copy import deepcopy
    raw={'_users':{'alice':{roles.POLICY_KEY:{'research':{'fallback':{'model':'private'}}}}}}
    monkeypatch.setattr(prefs_routes,'_load',lambda:deepcopy(raw))
    def save(data):raw.clear();raw.update(deepcopy(data))
    monkeypatch.setattr(prefs_routes,'_save',save)
    prefs=prefs_routes._load_for_user(None)
    assert roles.POLICY_KEY not in prefs
    prefs[roles.POLICY_KEY]={'research':{'fallback':None}}
    prefs_routes._save_for_user(None,prefs)
    assert raw['_users']['alice'][roles.POLICY_KEY]['research']['fallback']['model']=='private'
    assert raw[roles.POLICY_KEY]['research']['fallback'] is None


def test_role_assignment_uses_real_endpoint_owner_and_capability_checks(tmp_path,monkeypatch):
    from tests.helpers.database import disposable_database
    from src import endpoint_resolver
    import core.database as database
    with disposable_database(tmp_path) as factory:
        monkeypatch.setattr(database,'SessionLocal',factory)
        monkeypatch.setattr(endpoint_resolver,'SessionLocal',factory)
        monkeypatch.setattr(endpoint_resolver,'resolve_url',lambda url:url)
        with factory() as db:
            db.add(database.ModelEndpoint(id='alice-chat',name='Private test',owner='alice',
                base_url='https://models.test/v1',is_enabled=True,supports_tools=False,
                cached_models='["research"]',pinned_models='["research"]'))
            db.commit()
        assignment=roles.Assignment('alice-chat','research','openai_tools',('chat','tools'))
        with pytest.raises(roles.CapabilityUnavailable):roles.resolve(assignment,'alice',required='tools')
        with pytest.raises(roles.RoleUnavailable):roles.resolve(assignment,'bob',required='chat')
        with factory() as db:
            db.query(database.ModelEndpoint).filter_by(id='alice-chat').update({'supports_tools':True})
            db.commit()
        assert roles.resolve(assignment,'alice',required='tools').assignment.model=='research'
        with factory() as db:
            db.query(database.ModelEndpoint).filter_by(id='alice-chat').update({'is_enabled':False})
            db.commit()
        with pytest.raises(roles.RoleUnavailable):roles.resolve(assignment,'alice',required='tools')


@pytest.mark.asyncio
async def test_unavailable_utility_role_uses_explicit_foreground_route(monkeypatch):
    """An unavailable synthesis role must not strand an otherwise live chat turn."""
    primary = roles.Assignment('missing-endpoint', 'stale-model')
    monkeypatch.setattr(roles, 'candidates', lambda role, owner='': [primary])
    monkeypatch.setattr(roles, 'resolve', lambda assignment, owner='', required=None:
                        (_ for _ in ()).throw(roles.RoleUnavailable('missing endpoint')))
    monkeypatch.setattr(roles, 'get_policy', lambda role, owner='':
                        {'timeout': 5, 'retries': 0, 'cooldown': 30, 'failure_threshold': 1})
    recorded = []

    async def fake_llm(url, model, messages, **kwargs):
        recorded.append((url, model, kwargs.get('headers')))
        return 'Recovered answer'

    from src import llm_core
    monkeypatch.setattr(llm_core, 'llm_call_async', fake_llm)
    result = await roles.call('utility', [{'role': 'user', 'content': 'Hello'}],
                              primary_route=('http://localhost:8080/v1', 'gyro-s', {'Authorization': 'Bearer local'}))
    assert result == 'Recovered answer'
    assert recorded == [('http://localhost:8080/v1', 'gyro-s', {'Authorization': 'Bearer local'})]


@pytest.mark.asyncio
async def test_valid_utility_role_is_not_overridden_by_foreground_route(monkeypatch):
    chosen = roles.Assignment('utility-ep', 'utility-model')
    monkeypatch.setattr(roles, 'candidates', lambda role, owner='': [chosen])
    monkeypatch.setattr(roles, 'resolve', lambda assignment, owner='', required=None:
                        roles.Route(assignment, 'http://localhost:8090/v1', {}))
    monkeypatch.setattr(roles, 'get_policy', lambda role, owner='':
                        {'timeout': 5, 'retries': 0, 'cooldown': 30, 'failure_threshold': 1})
    recorded = []

    async def fake_llm(url, model, messages, **kwargs):
        recorded.append((url, model))
        return 'Utility answer'

    from src import llm_core
    monkeypatch.setattr(llm_core, 'llm_call_async', fake_llm)
    result = await roles.call('utility', [], primary_route=('http://localhost:8080/v1', 'gyro-s', {}))
    assert result == 'Utility answer'
    assert recorded == [('http://localhost:8090/v1', 'utility-model')]
