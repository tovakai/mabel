"""Conservative cache-prefix and Qwen intermediate web-round budgets."""
from src.agent_runtime.round_cache import (
    StableWebSchemas, digest_schema_surface, digest_system_prefix,
    fast_web_round_budget,
)


def _schema(name):
    return {"type":"function","function":{"name":name,"description":"Trusted tool",
            "parameters":{"type":"object","properties":{}}}}


def test_schema_prefix_stable_after_relevance_narrowing_with_unchanged_scope():
    cache=StableWebSchemas()
    first=[_schema("web_search"),_schema("web_fetch")]
    chosen,status=cache.choose(
        first,route_key=("local","qwen3.8-flash-next","compact",True),
        round_num=1,pure_web_turn=True,safe_to_freeze=True,
        allowed_names={"web_search","web_fetch"},
    )
    assert status=="baseline" and chosen==first
    narrowed=[_schema("web_fetch")]
    chosen,status=cache.choose(
        narrowed,route_key=("local","qwen3.8-flash-next","compact",True),
        round_num=2,pure_web_turn=True,safe_to_freeze=True,
        allowed_names={"web_search","web_fetch"},
    )
    assert status=="stable_web_prefix"
    assert chosen==first
    assert digest_schema_surface(chosen)==digest_schema_surface(first)


def test_permission_revocation_must_not_reenable_older_tool():
    cache=StableWebSchemas()
    key=("local","qwen3.8-flash-next","compact",True)
    cache.choose([_schema("web_search"),_schema("web_fetch")],
                 route_key=key,round_num=1,pure_web_turn=True,
                 safe_to_freeze=True,allowed_names={"web_search","web_fetch"})
    result,status=cache.choose([_schema("web_fetch")],
                 route_key=key,round_num=2,pure_web_turn=True,
                 safe_to_freeze=True,allowed_names={"web_fetch"})
    assert status=="authorization_changed"
    assert [x["function"]["name"] for x in result]==["web_fetch"]


def test_force_final_and_recovery_can_change_surface_without_cache_resurrection():
    cache=StableWebSchemas()
    key=("local","qwen3.8-flash-next","compact",True)
    cache.choose([_schema("web_search"),_schema("web_fetch")],
                 route_key=key,round_num=1,pure_web_turn=True,
                 safe_to_freeze=True,allowed_names={"web_search","web_fetch"})
    result,status=cache.choose([],route_key=key,round_num=2,
                 pure_web_turn=True,safe_to_freeze=False,
                 allowed_names={"web_search","web_fetch"})
    assert status=="dynamic_required" and result==[]


def test_no_freeze_after_model_or_endpoint_fallback_or_new_tool():
    cache=StableWebSchemas()
    key=("local","qwen3.8-flash-next","compact",True)
    cache.choose([_schema("web_fetch")],
                 route_key=key,round_num=1,pure_web_turn=True,
                 safe_to_freeze=True,allowed_names={"web_fetch"})
    changed,status=cache.choose(
        [_schema("web_fetch")],route_key=("other","qwen3.8-flash-next","compact",True),
        round_num=2,pure_web_turn=True,safe_to_freeze=True,
        allowed_names={"web_fetch"},
    )
    assert status=="route_changed"
    added,status=cache.choose(
        [_schema("web_search"),_schema("web_fetch")],route_key=key,
        round_num=3,pure_web_turn=True,safe_to_freeze=True,
        allowed_names={"web_search","web_fetch"},
    )
    assert status=="capability_added"
    assert len(added)==2


def test_schema_and_system_prefix_fingerprints_are_deterministic_and_redacted():
    schemas=[_schema("web_fetch")]
    digest=digest_schema_surface(schemas)
    assert len(digest)==16
    assert digest==digest_schema_surface(schemas)
    msgs=[{"role":"system","content":"You may search public sites."},
          {"role":"user","content":"Find two citations"}]
    assert digest_system_prefix(msgs)==digest_system_prefix(msgs+[
        {"role":"assistant","content":"Searching..."},
    ])
    assert digest_system_prefix(msgs)!=digest_system_prefix([
        {"role":"system","content":"A changed system instruction."},*msgs[1:],
    ])


def test_qwen_web_continuation_caps_generation_and_disables_auto_thinking():
    cap,thinking=fast_web_round_budget(
        "qwen3.8-flash-next-gyro-gyro-s",4096,"auto",
        round_num=2,pure_web_turn=True,has_tools=True,
    )
    assert cap==1536 and thinking=="off"
    cap,thinking=fast_web_round_budget(
        "qwen3.8-flash-next-gyro-gyro-s",4096,"off",
        round_num=7,pure_web_turn=True,has_tools=True,
    )
    assert cap==1536 and thinking=="off"


def test_nonweb_first_round_explicit_thinking_and_synthesis_are_unchanged():
    for kwargs in [
        dict(round_num=1,pure_web_turn=True,has_tools=True),
        dict(round_num=2,pure_web_turn=False,has_tools=True),
        dict(round_num=2,pure_web_turn=True,has_tools=False),
    ]:
        assert fast_web_round_budget(
            "qwen3.8-flash-next",4096,"auto",**kwargs,
        )==(4096,"auto")
    assert fast_web_round_budget(
        "qwen3.8-flash-next",4096,"on",
        round_num=3,pure_web_turn=True,has_tools=True,
    )==(4096,"on")
    assert fast_web_round_budget(
        "qwen3.6-14b",4096,"auto",
        round_num=3,pure_web_turn=True,has_tools=True,
    )==(4096,"auto")


def test_empty_schema_baseline_does_not_invent_a_tool():
    cache=StableWebSchemas()
    result,status=cache.choose(
        [],route_key=("local","model","full",True),
        round_num=1,pure_web_turn=True,safe_to_freeze=True,
        allowed_names=set(),
    )
    assert status=="baseline" and result==[]


def test_invalid_budget_is_left_to_existing_agent_budget_recovery():
    assert fast_web_round_budget(
        "qwen3.8-flash-next","not-a-number","auto",
        round_num=2,pure_web_turn=True,has_tools=True,
    )==("not-a-number","auto")
