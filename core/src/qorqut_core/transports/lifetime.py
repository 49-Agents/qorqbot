"""Cancellation-safe ownership of blocking lifetime and descriptor operations."""

import asyncio

import anyio


async def settle_sync(function):
    """Finish even after Task.cancel(), then propagate cancellation."""
    future = asyncio.get_running_loop().run_in_executor(None, function)
    cancelled = False
    with anyio.CancelScope(shield=True):
        while True:
            try:
                result = await asyncio.shield(future)
                break
            except asyncio.CancelledError:
                cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result
