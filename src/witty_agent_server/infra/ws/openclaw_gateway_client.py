from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from witty_agent_server.infra.ws.client_base import ClientBase

logger = logging.getLogger(__name__)


DEFAULT_GATEWAY_WS_URL = "ws://127.0.0.1:18789"
_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_EVENT_TIMEOUT = 30.0
_DEFAULT_IDLE_TIMEOUT = 30.0
_DEFAULT_SCOPES = [
    "operator.admin",
    "operator.read",
    "operator.write",
    "operator.approvals",
]
_DEFAULT_CAPS = ["tool-events"]
_DEVICE_IDENTITY_FILE = "device.json"
_DEVICE_AUTH_FILE = "device-auth.json"


class OpenClawGatewayClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OpenClawGatewayClient(ClientBase):
    def __init__(
        self,
        *,
        url: str = DEFAULT_GATEWAY_WS_URL,
        token: str | None = None,
        state_dir: Path | None = None,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        event_timeout: float = _DEFAULT_EVENT_TIMEOUT,
        idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self._url = url
        logger.debug("OpenClawGatewayClient init token arg=%r", token)
        self._token = token
        self._state_dir_override = state_dir
        self._resolved_token: str | None = None
        logger.debug(
            "OpenClawGatewayClient init state_dir_override=%r",
            self._state_dir_override,
        )
        self._connect_timeout = connect_timeout
        self._event_timeout = event_timeout
        self._idle_timeout = idle_timeout

    # 对接openclaw流式化接口，适用于长时间运行的对话，能够持续接收事件直到对话结束。
    def stream_turn(
        self, *, session_key: str, message: str
    ) -> Iterator[dict[str, Any]]:
        logger.debug("stream_turn called: session_key=%s", session_key)
        try:
            with self._open_connection() as ws:
                self._rpc(
                    ws,
                    method="sessions.messages.subscribe",
                    params={"key": session_key},
                )
                # self._ensure_session_change_streaming(ws=ws)
                self._ensure_tool_output_streaming(ws=ws, session_key=session_key)
                payload = self._rpc(
                    ws,
                    method="sessions.send",
                    params={
                        "key": session_key,
                        "message": message,
                        "idempotencyKey": str(uuid.uuid4()),
                    },
                )
                run_id = payload.get("runId") if isinstance(payload, dict) else None
                # 接收并解析openclaw event
                yield from self._collect_stream_events(
                    ws,
                    session_key=session_key,
                    run_id=run_id if isinstance(run_id, str) and run_id else None,
                )
        except OpenClawGatewayClientError as exc:
            logger.warning(
                "stream_turn caught OpenClawGatewayClientError: code=%s message=%s",
                exc.code,
                exc.message,
            )
            if exc.code == "GATEWAY_AUTH_FAILED" and "pairing required" in exc.message:
                logger.info("stream_turn pairing required, attempting auto-approve")
                if self._auto_approve_pairing():
                    logger.info("stream_turn pairing approved, retrying connection")
                    with self._open_connection() as ws:
                        # ... (same as above)

                        self._rpc(
                            ws,
                            method="sessions.messages.subscribe",
                            params={"key": session_key},
                        )
                        self._ensure_tool_output_streaming(
                            ws=ws, session_key=session_key
                        )
                        payload = self._rpc(
                            ws,
                            method="sessions.send",
                            params={
                                "key": session_key,
                                "message": message,
                                "idempotencyKey": str(uuid.uuid4()),
                            },
                        )
                        run_id = (
                            payload.get("runId") if isinstance(payload, dict) else None
                        )
                        yield from self._collect_stream_events(
                            ws,
                            session_key=session_key,
                            run_id=run_id
                            if isinstance(run_id, str) and run_id
                            else None,
                        )
                        return
            raise

    def list_agents(self) -> dict[str, Any]:
        with self._open_connection() as ws:
            payload = self._rpc(ws, method="agents.list", params={})
        logger.info(f"list_agents success")
        return payload

    def probe_gateway_auth(self) -> None:
        """只做 connect 握手预检，让 start 阶段提前暴露并恢复鉴权问题。"""
        with self._open_connection():
            logger.info("probe_gateway_auth success")

    def list_sessions(self, *, agent_id: str) -> dict[str, Any]:
        logger.info("list_sessions start by gateway rpc, agent_id=%s", agent_id)
        with self._open_connection() as ws:
            payload = self._rpc(
                ws,
                method="sessions.list",
                params={"agentId": agent_id},
            )
        logger.info(
            "list_sessions success, agent_id=%s payload_keys=%s",
            agent_id,
            sorted(payload.keys()),
        )
        return payload

    def get_agent(self, *, agent_id: str) -> dict[str, Any] | None:
        payload = self.list_agents()
        agents = payload.get("agents")
        if isinstance(agents, list):
            for item in agents:
                if not isinstance(item, dict):
                    continue
                if item.get("id") == agent_id:
                    logger.info(
                        "get_agent found gateway agent: agent_id=%s",
                        agent_id,
                    )
                    return item
        logger.warning("get_agent missing gateway agent: agent_id=%s", agent_id)
        return None

    def create_session(self, *, session_key: str) -> None:
        with self._open_connection() as ws:
            self._rpc(
                ws,
                method="sessions.create",
                params={"key": session_key},
            )

    # 通过 gateway RPC 查询指定 agent workspace 下可见的技能状态。
    def get_skills_status(self, *, agent_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if isinstance(agent_id, str) and agent_id:
            params["agentId"] = agent_id
        logger.info("get_skills_status start by gateway rpc, agent_id=%s", agent_id)
        with self._open_connection() as ws:
            payload = self._rpc(
                ws,
                method="skills.status",
                params=params,
            )
        logger.info(
            "get_skills_status success, agent_id=%s payload_keys=%s",
            agent_id,
            sorted(payload.keys()),
        )
        return payload

    # 删除 runtime 侧会话，优先使用 session.delete，若网关不支持则回退 sessions.delete。
    def delete_session(self, *, session_key: str) -> None:
        with self._open_connection() as ws:
            try:
                self._rpc(
                    ws,
                    method="session.delete",
                    params={"key": session_key},
                )
                logger.info(
                    "delete_session success by session.delete, session_key=%s",
                    session_key,
                )
                return
            except OpenClawGatewayClientError as exc:
                logger.warning(
                    (
                        "delete_session fallback to sessions.delete, "
                        "session_key=%s code=%s message=%s"
                    ),
                    session_key,
                    exc.code,
                    exc.message,
                )
            self._rpc(
                ws,
                method="sessions.delete",
                params={"key": session_key},
            )
            logger.info(
                "delete_session success by sessions.delete, session_key=%s",
                session_key,
            )

    # 终止会话中的运行任务，使用 OpenClaw sessions.abort 接口。
    def abort_session(self, *, session_key: str) -> None:
        with self._open_connection() as ws:
            self._rpc(
                ws,
                method="sessions.abort",
                params={"key": session_key},
            )
            logger.info(
                "abort_session success by sessions.abort, session_key=%s",
                session_key,
            )

    def _assign_session_key(self, *, target: dict[str, str], value: str) -> None:
        target["value"] = value

    def _open_connection(self) -> Any:
        token = self._resolve_gateway_token()
        if not token:
            logger.warning("_open_connection token check failed, token=%r", token)
            raise OpenClawGatewayClientError(
                code="GATEWAY_AUTH_MISSING",
                message="openclaw gateway token not configured",
            )

        try:
            ws = connect(self._url, open_timeout=self._connect_timeout)
            logger.info("WebSocket connection established: %s", self._url)
        except Exception as exc:  # pragma: no cover - network specific
            raise OpenClawGatewayClientError(
                code="GATEWAY_CONNECT_FAILED",
                message="failed to connect openclaw gateway",
            ) from exc

        try:
            self._handshake(ws)
        except OpenClawGatewayClientError as exc:
            ws.close()
            if self._is_pairing_required_error(exc) and self._auto_approve_pairing():
                logger.info(
                    "_open_connection pairing approved, retrying websocket connect: url=%s",
                    self._url,
                )
                retry_ws = connect(self._url, open_timeout=self._connect_timeout)
                logger.info("WebSocket connection established: %s", self._url)
                try:
                    self._handshake(retry_ws)
                except Exception:
                    retry_ws.close()
                    raise
                return retry_ws
            raise
        except Exception:
            ws.close()
            raise

        return ws

    def _handshake(self, ws: Any) -> None:
        challenge = self._recv_json(ws, timeout=self._connect_timeout)
        token = self._resolve_gateway_token()
        if (
            challenge.get("type") != "event"
            or challenge.get("event") != "connect.challenge"
        ):
            raise OpenClawGatewayClientError(
                code="GATEWAY_PROTOCOL_ERROR",
                message="openclaw gateway handshake challenge missing",
            )

        req_id = str(uuid.uuid4())
        role = "operator"
        scopes = list(_DEFAULT_SCOPES)
        params: dict[str, Any] = {
            "minProtocol": 3,
            "maxProtocol": 3,
            "client": {
                "id": "cli",
                "version": "2026.4.2",
                "platform": "linux",
                "mode": "cli",
            },
            "role": role,
            "scopes": scopes,
            "commands": [],
            "permissions": {},
            "caps": list(_DEFAULT_CAPS),
            "auth": {"token": token},
            "locale": "zh-CN",
            "userAgent": "witty-agent-server/0.1.0",
        }
        nonce = challenge.get("payload", {}).get("nonce")
        logger.debug("_handshake nonce=%s", nonce)
        if isinstance(nonce, str) and nonce:
            device = self._build_device_auth(
                nonce=nonce,
                role=role,
                scopes=scopes,
                signature_token=token,
                client_id="cli",
                client_mode="cli",
                platform="linux",
                device_family="",
            )
            logger.debug("_handshake device=%s", device)
            if device is not None:
                params["device"] = device
        else:
            logger.debug("_handshake nonce is empty or invalid, skipping device auth")

        response = self._send_connect_and_recv_response(ws, req_id=req_id, params=params)
        if response.get("ok") is not True:
            error = response.get("error")
            if self._should_retry_with_cached_device_token(error):
                retry_params = dict(params)
                retry_params["auth"] = dict(params["auth"])
                cached_device_token = self._load_cached_device_token()
                if cached_device_token:
                    retry_params["auth"]["deviceToken"] = cached_device_token
                    logger.warning(
                        "_handshake retrying with cached device token after auth mismatch"
                    )
                    response = self._send_connect_and_recv_response(
                        ws,
                        req_id=req_id,
                        params=retry_params,
                    )
            if response.get("ok") is not True:
                message = "openclaw gateway handshake failed"
                if isinstance(error, dict):
                    err_message = error.get("message")
                    if isinstance(err_message, str) and err_message:
                        message = err_message
                raise OpenClawGatewayClientError(
                    code="GATEWAY_AUTH_FAILED",
                    message=message,
                )
        payload = response.get("payload")
        if isinstance(payload, dict):
            self._store_device_token(payload=payload, fallback_role=role)

    def _send_connect_and_recv_response(
        self,
        ws: Any,
        *,
        req_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        ws.send(
            json.dumps(
                {
                    "type": "req",
                    "id": req_id,
                    "method": "connect",
                    "params": params,
                }
            )
        )
        response = self._recv_json(ws, timeout=self._connect_timeout)
        if response.get("type") != "res" or response.get("id") != req_id:
            raise OpenClawGatewayClientError(
                code="GATEWAY_PROTOCOL_ERROR",
                message="openclaw gateway handshake response mismatch",
            )
        return response

    def _should_retry_with_cached_device_token(
        self, error: dict[str, Any] | None
    ) -> bool:
        if not isinstance(error, dict):
            return False
        details = error.get("details")
        if not isinstance(details, dict):
            return False
        if details.get("code") != "AUTH_TOKEN_MISMATCH":
            return False
        return details.get("canRetryWithDeviceToken") is True

    def _load_cached_device_token(self) -> str | None:
        auth_path = self._identity_dir() / _DEVICE_AUTH_FILE
        if not auth_path.exists():
            return None
        try:
            body = json.loads(auth_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(body, dict):
            return None
        tokens = body.get("tokens")
        if not isinstance(tokens, dict):
            return None
        for value in tokens.values():
            if not isinstance(value, dict):
                continue
            token = value.get("token")
            if isinstance(token, str) and token:
                return token
        return None

    def _rpc(
        self,
        ws: Any,
        *,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        req_id = str(uuid.uuid4())
        ws.send(
            json.dumps(
                {
                    "type": "req",
                    "id": req_id,
                    "method": method,
                    "params": params,
                }
            )
        )

        deadline = self._event_timeout
        while True:
            message = self._recv_json(ws, timeout=deadline)
            if message.get("type") != "res":
                continue
            if message.get("id") != req_id:
                continue
            if message.get("ok") is True:
                payload = message.get("payload")
                return payload if isinstance(payload, dict) else {}

            error = message.get("error")
            code = "GATEWAY_RPC_ERROR"
            text = f"openclaw rpc failed: {method}"
            if isinstance(error, dict):
                raw_code = error.get("code")
                if isinstance(raw_code, str) and raw_code:
                    code = raw_code
                raw_message = error.get("message")
                if isinstance(raw_message, str) and raw_message:
                    text = raw_message
            raise OpenClawGatewayClientError(code=code, message=text)

    def _collect_stream_events(
        self,
        ws: Any,
        *,
        session_key: str,
        run_id: str | None,
    ) -> Iterator[dict[str, Any]]:
        while True:
            try:
                message = self._recv_json(ws, timeout=self._idle_timeout)
                logger.info(
                    "received message: %s",
                    json.dumps(message, indent=4, ensure_ascii=False),
                )
            except OpenClawGatewayClientError as exc:
                if self._is_recv_timeout_error(exc):
                    return
                raise
            if message.get("type") != "event":
                continue
            event_name = message.get("event")
            if not isinstance(event_name, str):
                continue
            if event_name not in {"agent", "session.message"}:
                continue
            payload = message.get("payload")
            normalized_payload = payload if isinstance(payload, dict) else {}
            event_session_key = self._extract_session_key(normalized_payload)
            if event_session_key is not None and not self._is_same_session_key(
                expected=session_key,
                actual=event_session_key,
            ):
                logger.debug(
                    (
                        "drop event due to session key mismatch: "
                        "expect=%s actual=%s event=%s"
                    ),
                    session_key,
                    event_session_key,
                    event_name,
                )
                continue
            if event_name in {"agent", "chat"} and run_id is not None:
                event_run_id = self._extract_run_id(normalized_payload)
                if event_run_id is not None and event_run_id != run_id:
                    logger.debug(
                        (
                            "drop event due to run id mismatch: "
                            "expect=%s actual=%s event=%s"
                        ),
                        run_id,
                        event_run_id,
                        event_name,
                    )
                    continue
            if event_name == "session.message":
                message_payload = normalized_payload.get("message")
                if isinstance(message_payload, dict):
                    stop_reason = message_payload.get("stopReason")
                    # 过滤resaon为stop的thinking
                    if stop_reason == "stop":
                        continue
            if event_name == "agent" and normalized_payload.get("stream") == "lifecycle":
                phase = normalized_payload.get("data", {}).get("phase")
                if phase == "end":
                    # 刷新token usage
                    usage_payload = self._fetch_session_usage(
                        ws=ws, session_key=session_key
                    )
                    if usage_payload is not None:
                        yield {
                            "type": "session.usage",
                            "payload": usage_payload,
                        }
            event = {
                "type": event_name,
                "payload": normalized_payload,
            }
            yield event

            if event_name != "agent":
                continue
            stream = normalized_payload.get("stream")
            data = normalized_payload.get("data")
            if stream == "lifecycle" and isinstance(data, dict):
                phase = data.get("phase")
                if phase in {"end", "error"}:
                    logger.debug(
                        (
                            "finish stream by lifecycle event: "
                            "session_key=%s run_id=%s phase=%s"
                        ),
                        session_key,
                        run_id,
                        phase,
                    )
                    return

    # 统一抽取事件里的 session key，用于并发场景隔离不同会话事件流。
    def _extract_session_key(self, payload: dict[str, Any]) -> str | None:
        direct = payload.get("sessionKey")
        if isinstance(direct, str) and direct:
            return direct
        nested = payload.get("message")
        if isinstance(nested, dict):
            nested_key = nested.get("sessionKey")
            if isinstance(nested_key, str) and nested_key:
                return nested_key
        data = payload.get("data")
        if isinstance(data, dict):
            data_key = data.get("sessionKey")
            if isinstance(data_key, str) and data_key:
                return data_key
        return None

    # 兼容网关返回带命名空间前缀的 sessionKey（如 agent:main:<session_id>）。
    def _is_same_session_key(self, *, expected: str, actual: str) -> bool:
        if actual == expected:
            return True
        normalized_actual = self._normalize_session_key(actual)
        return normalized_actual == expected

    def _normalize_session_key(self, value: str) -> str:
        prefix = "agent:main:"
        if value.startswith(prefix):
            return value[len(prefix) :]
        return value

    # 统一抽取 runId，仅用于 agent/chat 事件的同轮过滤。
    def _extract_run_id(self, payload: dict[str, Any]) -> str | None:
        direct = payload.get("runId")
        if isinstance(direct, str) and direct:
            return direct
        data = payload.get("data")
        if isinstance(data, dict):
            nested = data.get("runId")
            if isinstance(nested, str) and nested:
                return nested
        return None

    def _ensure_tool_output_streaming(self, *, ws: Any, session_key: str) -> None:
        try:
            self._rpc(
                ws,
                method="sessions.patch",
                params={"key": session_key, "verboseLevel": "full"},
            )
        except OpenClawGatewayClientError:
            # Best-effort tuning. If patch is rejected, keep normal turn flow.
            return

    def _ensure_session_change_streaming(self, *, ws: Any) -> None:
        try:
            self._rpc(
                ws,
                method="sessions.subscribe",
                params={},
            )
        except OpenClawGatewayClientError:
            # Best-effort tuning. If subscribe is rejected, keep normal turn flow.
            return

    def _fetch_session_usage(
        self,
        *,
        ws: Any,
        session_key: str,
    ) -> dict[str, Any] | None:
        try:
            payload = self._rpc(
                ws,
                method="sessions.usage",
                params={"key": session_key},
            )
            logger.debug("session usage: %s", json.dumps(payload, ensure_ascii=False))
        except OpenClawGatewayClientError as exc:
            # Best-effort usage snapshot. If rejected, keep normal turn flow.
            logger.debug(
                "skip session usage snapshot due to fetch failure: session_key=%s error=%r",
                session_key,
                exc,
            )
            return None
        return payload if payload else None

    def _recv_json(self, ws: Any, *, timeout: float) -> dict[str, Any]:
        try:
            raw = ws.recv(timeout=timeout)
        except TimeoutError as exc:
            raise OpenClawGatewayClientError(
                code="GATEWAY_RECV_TIMEOUT",
                message="openclaw gateway receive timeout",
            ) from exc
        except ConnectionClosed as exc:
            raise OpenClawGatewayClientError(
                code="GATEWAY_CLOSED",
                message=f"openclaw gateway closed: {exc}",
            ) from exc
        except Exception as exc:
            raise OpenClawGatewayClientError(
                code="GATEWAY_RECV_FAILED",
                message="failed to receive openclaw gateway message",
            ) from exc

        if not isinstance(raw, str):
            raise OpenClawGatewayClientError(
                code="GATEWAY_PROTOCOL_ERROR",
                message="openclaw gateway returned non-text frame",
            )

        try:
            message = json.loads(raw)
        except ValueError as exc:
            raise OpenClawGatewayClientError(
                code="GATEWAY_PROTOCOL_ERROR",
                message="openclaw gateway returned invalid json",
            ) from exc

        if not isinstance(message, dict):
            raise OpenClawGatewayClientError(
                code="GATEWAY_PROTOCOL_ERROR",
                message="openclaw gateway returned invalid frame",
            )

        return message

    def _resolve_gateway_token(self) -> str | None:
        if self._resolved_token is not None:
            return self._resolved_token
        token = self._token or self._token_from_env()
        if token:
            self._resolved_token = token
            return token
        self._resolved_token = self._token_from_config()
        return self._resolved_token

    def _token_from_env(self) -> str | None:
        from os import environ

        token = environ.get("OPENCLAW_GATEWAY_TOKEN")
        if isinstance(token, str) and token:
            return token
        return None

    def _token_from_config(self) -> str | None:
        config_path = self._state_dir() / "openclaw.json"
        logger.debug(
            "_token_from_config config_path=%s exists=%s",
            config_path,
            config_path.exists(),
        )
        if not config_path.exists():
            return None
        try:
            body = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("_token_from_config JSON parse failed: %s", exc)
            return None
        if not isinstance(body, dict):
            return None
        gateway = body.get("gateway")
        if not isinstance(gateway, dict):
            logger.debug("_token_from_config gateway not a dict")
            return None
        auth = gateway.get("auth")
        if not isinstance(auth, dict):
            logger.debug("_token_from_config auth not a dict")
            return None
        token = auth.get("token")
        logger.debug("_token_from_config token=%s", token)
        if isinstance(token, str) and token:
            return token
        return None

    def _state_dir(self) -> Path:
        """返回当前 client 绑定的 state 目录。"""
        if self._state_dir_override is not None:
            return self._state_dir_override
        return Path.home() / ".openclaw"

    def _is_recv_timeout_error(self, exc: OpenClawGatewayClientError) -> bool:
        """判断是否为网关接收超时错误。"""
        return exc.code == "GATEWAY_RECV_TIMEOUT"

    def _is_pairing_required_error(self, exc: OpenClawGatewayClientError) -> bool:
        """判断网关错误是否要求先完成 device pairing。"""
        return exc.code == "GATEWAY_AUTH_FAILED" and "pairing required" in exc.message

    def _identity_dir(self) -> Path:
        return self._state_dir() / "identity"

    def _load_device_identity(self) -> dict[str, str] | None:
        identity_path = self._identity_dir() / _DEVICE_IDENTITY_FILE
        logger.debug(
            "_load_device_identity path=%s exists=%s",
            identity_path,
            identity_path.exists(),
        )
        if not identity_path.exists():
            return None
        try:
            body = json.loads(identity_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("_load_device_identity JSON parse failed: %s", exc)
            return None
        if not isinstance(body, dict):
            return None
        raw_device_id = body.get("deviceId")
        raw_public_key_pem = body.get("publicKeyPem")
        raw_private_key_pem = body.get("privateKeyPem")
        if not isinstance(raw_device_id, str) or not raw_device_id:
            logger.debug("_load_device_identity deviceId missing or invalid")
            return None
        if not isinstance(raw_public_key_pem, str) or not raw_public_key_pem:
            logger.debug("_load_device_identity publicKeyPem missing or invalid")
            return None
        if not isinstance(raw_private_key_pem, str) or not raw_private_key_pem:
            logger.debug("_load_device_identity privateKeyPem missing or invalid")
            return None
        logger.debug("_load_device_identity success")
        return {
            "device_id": raw_device_id,
            "public_key_pem": raw_public_key_pem,
            "private_key_pem": raw_private_key_pem,
        }

    def _build_device_auth(
        self,
        *,
        nonce: str,
        role: str,
        scopes: list[str],
        signature_token: str | None,
        client_id: str,
        client_mode: str,
        platform: str,
        device_family: str,
    ) -> dict[str, Any] | None:
        logger.debug("_build_device_auth called")
        identity = self._load_device_identity()
        logger.debug("_build_device_auth identity_exists=%s", identity is not None)
        if identity is None:
            return None
        signed_at_ms = self._now_ms()
        payload = self._build_device_auth_payload_v3(
            device_id=identity["device_id"],
            client_id=client_id,
            client_mode=client_mode,
            role=role,
            scopes=scopes,
            signed_at_ms=signed_at_ms,
            token=signature_token,
            nonce=nonce,
            platform=platform,
            device_family=device_family,
        )
        signature = self._sign_device_payload(
            private_key_pem=identity["private_key_pem"],
            payload=payload,
        )
        public_key = self._public_key_raw_base64url_from_pem(
            public_key_pem=identity["public_key_pem"]
        )
        return {
            "id": identity["device_id"],
            "publicKey": public_key,
            "signature": signature,
            "signedAt": signed_at_ms,
            "nonce": nonce,
        }

    def _store_device_token(
        self, *, payload: dict[str, Any], fallback_role: str
    ) -> None:
        auth = payload.get("auth")
        if not isinstance(auth, dict):
            return
        token = auth.get("deviceToken")
        if not isinstance(token, str) or not token:
            return
        identity = self._load_device_identity()
        if identity is None:
            return
        role = auth.get("role")
        role_name = role if isinstance(role, str) and role else fallback_role
        raw_scopes = auth.get("scopes")
        scopes: list[str] = []
        if isinstance(raw_scopes, list):
            scopes = [scope for scope in raw_scopes if isinstance(scope, str)]

        auth_path = self._identity_dir() / _DEVICE_AUTH_FILE
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        store: dict[str, Any] = {
            "version": 1,
            "deviceId": identity["device_id"],
            "tokens": {},
        }
        if auth_path.exists():
            try:
                body = json.loads(auth_path.read_text(encoding="utf-8"))
            except Exception:
                body = None
            if (
                isinstance(body, dict)
                and body.get("deviceId") == identity["device_id"]
                and isinstance(body.get("tokens"), dict)
            ):
                store = body
        tokens = store.setdefault("tokens", {})
        if not isinstance(tokens, dict):
            tokens = {}
            store["tokens"] = tokens
        tokens[role_name] = {
            "token": token,
            "role": role_name,
            "scopes": scopes,
            "updatedAtMs": self._now_ms(),
        }
        auth_path.write_text(
            json.dumps(store, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _build_device_auth_payload_v3(
        self,
        *,
        device_id: str,
        client_id: str,
        client_mode: str,
        role: str,
        scopes: list[str],
        signed_at_ms: int,
        token: str | None,
        nonce: str,
        platform: str,
        device_family: str | None,
    ) -> str:
        fields = [
            "v3",
            device_id,
            client_id,
            client_mode,
            role,
            ",".join(scopes),
            str(signed_at_ms),
            token or "",
            nonce,
            self._normalize_device_metadata_for_auth(platform),
            self._normalize_device_metadata_for_auth(device_family),
        ]
        return "|".join(fields)

    def _normalize_device_metadata_for_auth(self, value: str | None) -> str:
        if not isinstance(value, str):
            return ""
        trimmed = value.strip()
        if not trimmed:
            return ""
        return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in trimmed)

    def _public_key_raw_base64url_from_pem(self, *, public_key_pem: str) -> str:
        key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        raw = key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return self._base64url_encode(raw)

    def _sign_device_payload(self, *, private_key_pem: str, payload: str) -> str:
        key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
        if not isinstance(key, Ed25519PrivateKey):
            raise OpenClawGatewayClientError(
                code="GATEWAY_AUTH_FAILED",
                message="invalid ed25519 private key",
            )
        signature = key.sign(payload.encode("utf-8"))
        return self._base64url_encode(signature)

    def _base64url_encode(self, raw: bytes) -> str:
        from base64 import urlsafe_b64encode

        return urlsafe_b64encode(raw).rstrip(b"=").decode("utf-8")

    def _now_ms(self) -> int:
        from time import time

        return int(time() * 1000)

    def _auto_approve_pairing(self) -> bool:
        import subprocess

        token = self._resolve_gateway_token() or ""
        env = dict(os.environ)
        env["OPENCLAW_STATE_DIR"] = str(self._state_dir())
        latest_cmd = [
            "openclaw",
            "devices",
            "approve",
            "--latest",
            "--token",
            token,
            "--url",
            self._url,
        ]
        try:
            result = subprocess.run(
                latest_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            logger.debug("_auto_approve_pairing returncode=%s", result.returncode)
            if result.stdout:
                logger.debug("_auto_approve_pairing stdout: %s", result.stdout[:200])
            if result.stderr:
                logger.debug("_auto_approve_pairing stderr: %s", result.stderr[:200])
            if result.returncode == 0:
                return True

            request_id = self._extract_pairing_request_id(
                stdout=result.stdout,
                stderr=result.stderr,
            )
            if not request_id:
                return False

            approve_cmd = [
                "openclaw",
                "devices",
                "approve",
                request_id,
                "--token",
                token,
                "--url",
                self._url,
            ]
            retry = subprocess.run(
                approve_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            logger.debug("_auto_approve_pairing explicit returncode=%s", retry.returncode)
            if retry.stdout:
                logger.debug("_auto_approve_pairing explicit stdout: %s", retry.stdout[:200])
            if retry.stderr:
                logger.debug("_auto_approve_pairing explicit stderr: %s", retry.stderr[:200])
            return retry.returncode == 0
        except Exception as exc:
            logger.warning("_auto_approve_pairing failed: %s", exc)
            return False

    def _extract_pairing_request_id(self, *, stdout: str, stderr: str) -> str | None:
        """从 CLI 输出中提取待审批 pairing request id。"""
        text = "\n".join(part for part in (stdout, stderr) if part)
        match = re.search(
            r"approve\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            text,
            re.IGNORECASE,
        )
        if match is None:
            return None
        return match.group(1)
