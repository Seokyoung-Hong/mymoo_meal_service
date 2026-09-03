"""In-process Server-Sent Events pub/sub.

토픽별 asyncio.Queue 집합. 결제/충전/식대 발급 시점에 ``publish`` 하고,
``sse_response`` 가 그 토픽의 구독 스트림을 돌려준다.

ponytail: 프로세스 내 큐라 uvicorn 워커 1개 전제. 워커를 늘리면 Redis pub/sub으로 교체.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse

KEEPALIVE_SECONDS = 25  # Cloudflare는 100초 무응답이면 끊는다.

_subscribers: defaultdict[str, set[asyncio.Queue[str]]] = defaultdict(set)


def format_event(event: str, data: Any) -> str:
    """SSE 프레임 한 개(``event:`` + ``data:`` 줄, 빈 줄로 종료)를 만든다."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def publish(topic: str, event: str, data: Any) -> None:
    """토픽 구독자 전원에게 이벤트를 보낸다. 구독자가 없으면 아무 일도 안 한다."""
    queues = _subscribers.get(topic)
    if not queues:
        return
    frame = format_event(event, data)
    for queue in queues:
        queue.put_nowait(frame)


async def subscribe(topic: str) -> AsyncIterator[str]:
    """토픽을 구독해 SSE 프레임을 흘린다. 유휴 시 keep-alive 주석을 보낸다."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    _subscribers[topic].add(queue)
    try:
        yield ": connected\n\n"
        while True:
            try:
                yield await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": keep-alive\n\n"
    finally:
        _subscribers[topic].discard(queue)
        if not _subscribers[topic]:
            del _subscribers[topic]


def sse_response(topic: str) -> StreamingResponse:
    """토픽 구독 스트림을 ``text/event-stream`` 응답으로 감싼다."""
    return StreamingResponse(
        subscribe(topic),
        media_type="text/event-stream",
        # X-Accel-Buffering: nginx가 이 응답만 버퍼링하지 않게 한다 (설정 변경 불필요).
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
