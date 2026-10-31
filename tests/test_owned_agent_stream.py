"""Consumer task changes must not transfer ContextVar token ownership."""
import asyncio
from contextvars import ContextVar

import pytest

from src.agent_runtime.authority import (
    RequestAuthority, active_request_authority, with_request_authority,
)
from src.agent_runtime.owned_stream import owned_stream
from src.turn_contract import with_turn_contract


@pytest.mark.asyncio
async def test_authority_stream_can_be_closed_by_another_consumer_task():
    marker = ContextVar('stream-owner-test', default=None)
    tasks = []

    @with_request_authority
    @with_turn_contract
    async def stream(*, request_authority, turn_contract=None):
        token = marker.set('bound')
        tasks.append(asyncio.current_task())
        try:
            assert active_request_authority() == request_authority
            yield 'approval'
            raise AssertionError('Closing may not execute the next operation')
        finally:
            assert active_request_authority() == request_authority
            marker.reset(token)
            tasks.append(asyncio.current_task())

    iterator = stream(request_authority=RequestAuthority.empty(owner='alice', session_id='s'))
    assert await asyncio.create_task(anext(iterator)) == 'approval'
    assert active_request_authority() is None
    await asyncio.create_task(iterator.aclose())
    assert tasks[0] is tasks[1]
    assert marker.get() is None


@pytest.mark.asyncio
async def test_stream_has_no_prefetch_and_cancellation_closes_in_owner_task():
    started, closed = asyncio.Event(), asyncio.Event()
    actions = []

    @owned_stream
    async def stream():
        try:
            yield 'first'
            actions.append('requested')
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    iterator = stream()
    assert await anext(iterator) == 'first'
    await asyncio.sleep(0)
    assert actions == []
    consumer = asyncio.create_task(anext(iterator))
    await started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert closed.is_set()
    assert actions == ['requested']
