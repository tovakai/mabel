"""Owner-scoped automatic memory-list authorization regressions."""
from src.agent_runtime.authority import ExactOperation, create_request_authority


def test_named_owner_may_list_memories_without_exact_approval():
    authority = create_request_authority(
        "What day is it?", owner="zum", session_id="conversation-one"
    )
    assert authority.permits(ExactOperation.normalize("manage_memory", '{"action":"list"}'))
    assert not authority.permits(ExactOperation.normalize("manage_memory", '{"action":"delete","id":"1"}'))
    assert not authority.permits(ExactOperation.normalize("manage_memory", '{"action":"add","content":"x"}'))


def test_ownerless_context_does_not_receive_automatic_memory_access():
    authority = create_request_authority(
        "What day is it?", owner="", session_id="background"
    )
    assert not authority.permits(ExactOperation.normalize("manage_memory", '{"action":"list"}'))
