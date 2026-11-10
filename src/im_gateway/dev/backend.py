"""Developer echo backend for debugging gateway ↔ channel connectivity.

Two ways to use this kit:

* **in-process** — ``imgw run --dev-backend`` starts this app on a loopback port
  and routes **every tenant** to it (no ``backends`` row is written);
* **standalone** — ``uv run python -m im_gateway.dev.backend --port 9900`` and
  register it as a normal backend when you want to debug another gateway.

It implements the minimum model-A contract (HANDOVER §12): ``POST /v1/turns``
consumes the ``kind=start`` frame and streams two SSE frames back — one
``message`` that echoes channel / subject / turn / idempotency / text and one
``completed`` — so a channel developer sees exactly what the gateway received
as the chat reply.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from im_gateway.core.contracts import FrameKind, TextContent, TurnFrame

__all__ = ["create_dev_backend_app", "main", "summary_text"]

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def summary_text(start: TurnFrame) -> str:
    """One-shot human-readable echo of everything the gateway sent."""
    channel = start.channel
    subject = start.subject
    lines = [
        "✅ dev-backend received a turn",
        f"channel_id      {channel.channel_id if channel else '-'}",
        f"channel_kind    {channel.kind if channel else '-'}",
        f"tenant_id       {(channel.tenant_id if channel else None) or '-'}",
        f"subject_id      {subject.subject_id if subject else '-'}",
        f"subject_name    {(subject.display_name if subject else '') or '-'}",
        f"chat_type       {(subject.chat_type if subject else '') or '-'}",
        f"session_key     {start.session_key or '-'}",
        f"turn_id         {start.turn_id}",
        f"idempotency_key {start.idempotency_key or '-'}",
        f"message         {start.text or '(no text)'}",
    ]
    return "\n".join(lines)


def _frames(start: TurnFrame) -> Iterator[TurnFrame]:
    yield TurnFrame(
        turn_id=start.turn_id,
        seq=1,
        kind=FrameKind.MESSAGE,
        content=[TextContent(text=summary_text(start))],
    )
    yield TurnFrame(turn_id=start.turn_id, seq=2, kind=FrameKind.COMPLETED)


def _sse(frames: Iterator[TurnFrame]) -> Iterator[str]:
    for frame in frames:
        payload = json.dumps(frame.model_dump(mode="json"), ensure_ascii=False)
        yield f"data: {payload}\n\n"


def create_dev_backend_app(*, token: str = "") -> FastAPI:
    """Build the debug backend ASGI app.

    An empty *token* accepts any caller (in-process dev mode); pass the same
    bearer the gateway uses when you register this app as a normal backend.
    """
    app = FastAPI(title="im-gateway dev backend", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "mode": "dev-backend"}

    @app.post("/v1/turns")
    async def turns(request: Request) -> Any:
        if token and request.headers.get("authorization", "") != f"Bearer {token}":
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        payload: Any = await request.json()
        try:
            start = TurnFrame.model_validate(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid TurnFrame: {exc}") from exc
        if start.kind != FrameKind.START:
            raise HTTPException(status_code=400, detail="dev backend expects kind=start")
        return StreamingResponse(
            _sse(_frames(start)),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    return app


def main(argv: list[str] | None = None) -> None:
    """Standalone entry point: ``python -m im_gateway.dev.backend``."""
    parser = argparse.ArgumentParser(description="im-gateway developer echo backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9900)
    parser.add_argument("--token", default="")
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        create_dev_backend_app(token=str(args.token)),
        host=str(args.host),
        port=int(args.port),
    )


if __name__ == "__main__":
    main()
