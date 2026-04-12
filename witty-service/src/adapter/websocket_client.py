from __future__ import annotations

import json
from typing import Any, Iterator

import websockets

from src.adapter.exceptions import (
    AdaptorConnectionError,
    AdaptorConnectionTimeout,
    AdaptorSendFailed,
    AdaptorReceiveError,
)
from src.adapter.websocket_protocol import InboundEvent, OutboundMessage

class WebSocketClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._connected: bool = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _build_url(self, session_id: str) -> str:
        return f"{self._base_url}/agent/sessions/{session_id}/ws"

    async def connect(self, session_id: str) -> None:
        url = self._build_url(session_id)
        try:
            self._ws = await websockets.connect(url)
            self._connected = True
        except Exception as exc:
            raise AdaptorConnectionError(
                message="WebSocket connection failed",
                details={"url": url, "error": str(exc)},
            ) from exc

    async def disconnect(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None
            self._connected = False

    async def send(self, message: OutboundMessage) -> None:
        if not self._ws or not self._connected:
            raise AdaptorSendFailed(
                message="Cannot send - not connected",
                details={},
            )
        try:
            await self._ws.send(json.dumps(message))
        except Exception as exc:
            raise AdaptorSendFailed(
                message="Failed to send WebSocket message",
                details={"error": str(exc)},
            ) from exc

    async def recv(self) -> Iterator[InboundEvent]:
        if not self._ws or not self._connected:
            raise AdaptorReceiveError(
                message="Cannot recv - not connected",
                details={},
            )
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                    yield InboundEvent(
                        type=data["type"],
                        session_id=data["session_id"],
                        sandbox_type=data.get("sandbox_type", data.get("runtime_type")),
                        event_id=data["event_id"],
                        ts_ms=data["ts_ms"],
                        payload=data.get("payload", {}),
                    )
                except (json.JSONDecodeError, KeyError) as exc:
                    raise AdaptorReceiveError(
                        message="Failed to parse WebSocket message",
                        details={"error": str(exc), "raw": raw[:200]},
                    ) from exc
        except websockets.ConnectionClosed:
            self._connected = False
            return
        except Exception as exc:
            raise AdaptorReceiveError(
                message="WebSocket receive failed",
                details={"error": str(exc)},
            ) from exc

    async def close(self) -> None:
        await self.disconnect()
