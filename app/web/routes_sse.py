from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.web.deps import RuntimeDep

router = APIRouter()


@router.get("/events")
async def events(request: Request, runtime: RuntimeDep):
    async def generator():
        async with runtime.bus.subscribe() as queue:
            while not await request.is_disconnected():
                yield {"data": await queue.get()}

    # sse-starlette sends a ": ping" comment every 15s on its own, so idle streams stay open.
    return EventSourceResponse(generator())
