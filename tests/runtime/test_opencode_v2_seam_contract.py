from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from witty_agent_server.infra.clients.opencode_v2_client import OpenCodeV2Client
from witty_agent_server.runtime_adapter import OpenCodeRuntimeAdapter
from witty_service.runtime.contracts import (
    HistoryQuery,
    ReplayQuery,
    RuntimeConnection,
    RuntimeEventType,
    RuntimeFilePart,
    RuntimeMessageKind,
    RuntimeModelRef,
    RuntimeTextPart,
    RuntimeWorkspace,
    TurnExecutionRequest,
)
from witty_service.runtime.errors import (
    HistoryCursorSessionMismatchError,
    RuntimeAdmissionConflictError,
    RuntimeContractValidationError,
)

_BASE_URL = "http://runtime.test"
_DIRECTORY = "/srv/ws_a"
_READY_SESSION = {
    "id": "ses_1",
    "agent": "probe-a",
    "model": {"providerID": "mock", "id": "mock-model"},
}
_ADMISSION = {
    "id": "msg_1",
    "sessionID": "ses_1",
    "admittedSeq": 5,
    "delivery": "steer",
}


def _json(status: int, body: object) -> httpx.Response:
    return httpx.Response(
        status, json=body, headers={"content-type": "application/json"}
    )


async def _connected(
    handler: Callable[[httpx.Request], httpx.Response],
) -> OpenCodeRuntimeAdapter:
    adapter = OpenCodeRuntimeAdapter(
        client_factory=lambda _url: OpenCodeV2Client(
            _BASE_URL, transport=httpx.MockTransport(handler)
        ),
        startup_timeout=0,
    )
    await adapter.connect(
        RuntimeConnection(
            workspace=RuntimeWorkspace(
                workspace_id="ws_a", runtime_directory=_DIRECTORY
            ),
            base_url=_BASE_URL,
        )
    )
    return adapter


def _handler(
    *,
    session: dict[str, Any] | None = None,
    prompt: httpx.Response | None = None,
    history: dict[str, Any] | None = None,
    messages: dict[str, Any] | None = None,
    events: str = "",
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            return _json(200, {"healthy": True})
        if path == "/api/location":
            return _json(200, {"directory": _DIRECTORY})
        if path == "/api/agent":
            return _json(200, {"data": [{"id": "build"}, {"id": "probe-a"}]})
        if path == "/api/model":
            return _json(200, {"data": [{"providerID": "mock", "id": "mock-model"}]})
        if path == "/api/skill":
            return _json(200, {"data": []})
        if path == "/api/session/ses_1" and request.method == "GET":
            return _json(200, {"data": session or {"id": "ses_1"}})
        if path == "/api/session/ses_1/prompt":
            return prompt or _json(200, {"data": _ADMISSION})
        if path == "/api/session/ses_1/history":
            return _json(200, history or {"data": [], "hasMore": False})
        if path == "/api/session/ses_1/message":
            return _json(
                200,
                messages
                or {"data": [], "cursor": {"next": "next_1", "previous": None}},
            )
        if path == "/api/event":
            return httpx.Response(
                200,
                content=events.encode(),
                headers={"content-type": "text/event-stream"},
            )
        if path in {"/api/session/ses_1/agent", "/api/session/ses_1/model"}:
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {path}")

    return handler


def _turn(
    *, parts: tuple[Any, ...] | None = None, resume: bool | None = None
) -> TurnExecutionRequest:
    return TurnExecutionRequest(
        workspace_id="ws_a",
        session_id="session_local",
        turn_id="turn_1",
        runtime_session_id="ses_1",
        message_id="msg_1",
        agent_internal_name="probe-a",
        model=RuntimeModelRef(provider_id="mock", model_id="mock-model"),
        parts=parts or (RuntimeTextPart(text="hello"),),
        resume=resume,
    )


def _event(
    event_id: str,
    event_type: str,
    *,
    seq: int | None = None,
    directory: str = _DIRECTORY,
    **data: Any,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": event_type,
        "data": {"sessionID": "ses_1", **data},
        "durable": {"seq": seq} if seq is not None else None,
        "location": {"directory": directory},
    }


@pytest.mark.asyncio
async def test_prepare_switches_agent_and_model() -> None:
    calls: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            calls.append((request.url.path, json.loads(request.read())))
        return _handler(session={"id": "ses_1", "agent": "build"})(request)

    adapter = await _connected(handler)
    await adapter.prepare_turn(_turn())

    assert calls == [
        ("/api/session/ses_1/agent", {"agent": "probe-a"}),
        (
            "/api/session/ses_1/model",
            {"model": {"providerID": "mock", "id": "mock-model"}},
        ),
    ]


@pytest.mark.asyncio
async def test_admit_maps_admission_and_omits_resume() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/ses_1/prompt":
            captured.update(json.loads(request.read()))
        return _handler(session=_READY_SESSION)(request)

    adapter = await _connected(handler)

    with pytest.raises(RuntimeContractValidationError) as excinfo:
        await adapter.admit_turn(
            _turn(
                parts=(
                    RuntimeTextPart(text="hello"),
                    RuntimeFilePart(relative_path="input/a.txt", mime="text/plain"),
                )
            )
        )
    assert excinfo.value.details["field"] == "parts"

    admission = await adapter.admit_turn(_turn())

    assert admission.message_id == "msg_1"
    assert admission.runtime_session_id == "ses_1"
    assert (admission.admitted_seq, admission.delivery) == (5, "steer")
    assert captured == {"id": "msg_1", "prompt": {"text": "hello"}}

    conflict = await _connected(
        _handler(
            session=_READY_SESSION,
            prompt=_json(409, {"tag": "MessageConflict", "message": "conflict"}),
        )
    )
    with pytest.raises(RuntimeAdmissionConflictError):
        await conflict.admit_turn(_turn())


@pytest.mark.asyncio
async def test_subscribe_filters_location_and_maps_events() -> None:
    events = "".join(
        f"data: {json.dumps(item)}\n\n"
        for item in (
            _event(
                "evt_9",
                "session.next.text.delta",
                directory="/srv/other",
                seq=9,
                delta="x",
            ),
            _event("evt_12", "session.next.step.ended", timestamp=190),
            _event(
                "evt_10",
                "session.next.text.delta",
                timestamp=123,
                assistantMessageID="msg_2",
                delta="he",
            ),
            _event("evt_11", "session.next.step.ended", seq=11, timestamp=200),
            _event("evt_13", "session.idle", timestamp=210),
        )
    )
    adapter = await _connected(_handler(events=events))

    envelopes = [envelope async for envelope in adapter.subscribe("ws_a")]

    assert [item.type for item in envelopes] == [
        RuntimeEventType.MESSAGE_DELTA,
        RuntimeEventType.TURN_COMPLETED,
        RuntimeEventType.SESSION_IDLE,
    ]
    assert envelopes[0].durable_seq is None
    assert envelopes[1].durable_seq == 11
    assert envelopes[0].runtime_message_id == "msg_2"
    assert envelopes[0].payload.delta == "he"
    assert envelopes[0].occurred_at == 123


@pytest.mark.asyncio
async def test_replay_maps_events_and_keeps_has_more() -> None:
    adapter = await _connected(
        _handler(
            history={
                "data": [
                    _event("evt_1", "session.next.prompted", seq=1),
                    _event("evt_2", "session.next.step.started", seq=2, timestamp=100),
                ],
                "hasMore": True,
            }
        )
    )

    replay = await adapter.replay_events(
        ReplayQuery(
            workspace_id="ws_a", runtime_session_id="ses_1", after_seq=0, limit=10
        )
    )

    assert replay.has_more is True
    assert [event.type for event in replay.events] == [RuntimeEventType.TURN_STARTED]
    assert replay.events[0].durable_seq == 2


@pytest.mark.asyncio
async def test_history_maps_messages_and_scopes_cursor_to_session() -> None:
    seen_cursor: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/ses_1/message":
            seen_cursor.append(request.url.params.get("cursor", ""))
        return _handler(
            messages={
                "data": [
                    {
                        "id": "assistant_1",
                        "type": "assistant",
                        "time": {"created": 30},
                        "finish": "stop",
                        "content": [
                            {"type": "text", "text": "answer"},
                            {"type": "reasoning", "text": "think"},
                            {"type": "tool", "callID": "call_1"},
                        ],
                    },
                    {
                        "id": "switch_1",
                        "type": "agent-switched",
                        "time": {"created": 20},
                        "agent": "probe-a",
                    },
                    {
                        "id": "user_1",
                        "type": "user",
                        "time": {"created": 10},
                        "text": "question",
                    },
                ],
                "cursor": {"next": "next_1", "previous": None},
            }
        )(request)

    adapter = await _connected(handler)

    page = await adapter.read_history(
        HistoryQuery(workspace_id="ws_a", runtime_session_id="ses_1", limit=10)
    )

    assert [item.kind for item in page.items] == [
        RuntimeMessageKind.ASSISTANT,
        RuntimeMessageKind.AGENT_SWITCHED,
        RuntimeMessageKind.USER,
    ]
    assert [part.text for part in page.items[0].parts] == ["answer", "think"]
    assert (page.items[0].finish, page.items[2].parts[0].text) == ("stop", "question")
    assert (page.next_cursor, page.previous_cursor) == ("ses_1:next_1", None)

    await adapter.read_history(
        HistoryQuery(
            workspace_id="ws_a",
            runtime_session_id="ses_1",
            limit=10,
            cursor="ses_1:next_1",
        )
    )
    assert seen_cursor == ["", "next_1"]

    with pytest.raises(HistoryCursorSessionMismatchError):
        await adapter.read_history(
            HistoryQuery(
                workspace_id="ws_a",
                runtime_session_id="ses_1",
                limit=10,
                cursor="ses_other:next_1",
            )
        )
