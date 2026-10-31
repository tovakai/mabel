from copy import deepcopy
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from src import model_roles as roles
from routes.model_role_routes import setup_model_role_routes


@pytest.fixture
def client(monkeypatch):
    from routes import prefs_routes,model_role_routes
    prefs={}
    monkeypatch.setattr(model_role_routes,'get_current_user',lambda request:'alice')
    monkeypatch.setattr(prefs_routes,'_load_for_user',lambda user:deepcopy(prefs))
    def save(user,value):
        assert user=='alice';prefs.clear();prefs.update(deepcopy(value))
    monkeypatch.setattr(prefs_routes,'_save_for_user',save)
    monkeypatch.setattr(roles,'setting',lambda key,owner='',default=None:prefs.get(key,default))
    monkeypatch.setattr(roles,'resolve',lambda a,owner='',required=None:roles.Route(a,'https://service.test/v1',{}))
    app=FastAPI();app.include_router(setup_model_role_routes())
    with TestClient(app) as c:yield c,prefs


def config(model='M'):
    return {'endpoint_id':'ep','model':model,'adapter':'existing','capabilities':['chat']}


def test_settings_additive_migration_primary_preservation_and_restart(client):
    c,prefs=client
    prefs.update(default_endpoint_id='existing',default_model='Gyro-S',utility_model='memory-helper')
    before=deepcopy(prefs)
    result=c.get('/api/model-roles')
    assert result.status_code==200 and prefs==before
    roles_by_name={r['role']:r for r in result.json()['roles']}
    assert roles_by_name['default']['primary']['model']=='Gyro-S'
    update=c.put('/api/model-roles/default',json={'fallback':config('fallback'),'timeout':10})
    assert update.status_code==200,update.text
    assert prefs['default_model']=='Gyro-S' and prefs['utility_model']=='memory-helper'
    assert prefs['foreground_fallback_enabled']
    assert c.get('/api/model-roles').json()['roles'][0]['policy']['fallback']['model']=='fallback'
    assert c.put('/api/model-roles/default',json={'fallback':None}).status_code==200
    assert not prefs['foreground_fallback_enabled'] and prefs['default_model']=='Gyro-S'


def test_existing_primary_metadata_does_not_overwrite_picker_changed_while_open(client):
    c,prefs=client
    prefs.update(research_endpoint_id='new-primary',research_model='new-model')
    response=c.put('/api/model-roles/research',json={'primary_metadata':{
        **config('stale-old-model'),'capabilities':['chat','tools'],'adapter':'openai_tools'},'fallback':None})
    assert response.status_code==200,response.text
    assert prefs['research_model']=='new-model' and prefs['research_endpoint_id']=='new-primary'


def test_incompatible_adapters_rejected_and_no_credentials_persisted(client):
    c,prefs=client
    bad=c.put('/api/model-roles/embedding',json={'primary':config()})
    assert bad.status_code==400 and not prefs
    good=c.put('/api/model-roles/embedding',json={'primary':{
        **config('embed'),'adapter':'openai_embeddings','capabilities':['embedding'],
        'space_id':'BGE-M3','revision':'operator-verified-revision','dimensions':1024}})
    assert good.status_code==200,good.text
    assert prefs['embedding_model']=='embed'
    assert 'headers' not in json.dumps(prefs) and 'api_key' not in json.dumps(prefs)
    bad=c.put('/api/model-roles/research',json={'fallback':{**config(),'adapter':'openai_embeddings'}})
    assert bad.status_code==400


def test_invalid_limits_and_model_generated_settings_cannot_enter_api(client):
    from core.middleware import INTERNAL_TOOL_HEADER,INTERNAL_TOOL_TOKEN
    c,prefs=client
    response=c.put('/api/model-roles/research',headers={INTERNAL_TOOL_HEADER:INTERNAL_TOOL_TOKEN},json={'fallback':None})
    assert response.status_code==403
    assert c.put('/api/model-roles/research',json={'limits':{'max_calls':100000}}).status_code==400
    assert c.put('/api/model-roles/utility',json={'retries':99}).status_code==400
    assert not prefs


def test_malformed_saved_role_does_not_break_other_settings(client):
    c,prefs=client
    prefs[roles.POLICY_KEY]={'embedding':{'fallback':{'invented_field':True}}}
    response=c.get('/api/model-roles')
    assert response.status_code==200
    entry=next(r for r in response.json()['roles'] if r['role']=='embedding')
    assert entry['configuration_error']
