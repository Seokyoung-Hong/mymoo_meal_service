"""Request ID middleware utilities."""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request


REQUEST_ID_HEADER = "X-Request-ID"


def _coerce_request_id(value: str | None) -> str:
    request_id = (value or "").strip()
    if request_id:
        return request_id
    return str(uuid4())


def add_request_id_middleware(app: FastAPI) -> None:
    """Register middleware that stores and returns a request ID."""

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = _coerce_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
