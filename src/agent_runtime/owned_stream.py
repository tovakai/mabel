"""Demand-driven async streams with one task owning their ContextVars.

ASGI consumers may iterate and close in different tasks. Generator-local
context managers must still enter and exit in the same execution context.
There is no prefetch: each consumer request advances exactly one yield.
"""
import asyncio
from functools import wraps


class OwnedStream:
    def __init__(self, iterator):
        self._iterator = iterator
        self._requests = asyncio.Queue(maxsize=1)
        self._task = None
        self._reading = False
        self._closed = False

    def __aiter__(self):
        return self

    async def _run(self):
        reply = None
        terminal = None
        try:
            while True:
                reply = await self._requests.get()
                try:
                    item = await anext(self._iterator)
                except BaseException as exc:
                    terminal = exc
                    return
                if not reply.done():
                    reply.set_result(item)
                reply = None
        finally:
            self._closed = True
            try:
                await self._iterator.aclose()
            except BaseException as exc:
                terminal = exc
                if reply is None or reply.done():
                    raise
            finally:
                # Publish terminal errors only after producer cleanup. A
                # consumer ending its turn must not interrupt token cleanup.
                if reply is not None and not reply.done():
                    reply.set_exception(terminal or asyncio.CancelledError())

    async def __anext__(self):
        if self._closed:
            raise StopAsyncIteration
        if self._reading:
            raise RuntimeError("Concurrent reads of an agent stream are unsupported")
        self._reading = True
        try:
            if self._task is None:
                self._task = asyncio.create_task(self._run())
            reply = asyncio.get_running_loop().create_future()
            self._requests.put_nowait(reply)
            try:
                return await reply
            except asyncio.CancelledError:
                await self.aclose()
                raise
        finally:
            self._reading = False

    async def aclose(self):
        self._closed = True
        if self._task is None:
            await self._iterator.aclose()
        else:
            if not self._task.done():
                self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


def owned_stream(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        return OwnedStream(func(*args, **kwargs))
    return wrapped
