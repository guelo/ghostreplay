from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import uuid

_SECRET_KEYS = {"password", "token", "secret", "jwt"}

BODY_BUFFER_CAP = 4096
BODY_LOG_CAP = 1024

logger = logging.getLogger("ghostreplay.http")


def _single_line(value: object) -> str:
    return str(value).replace("\n", "\\n")


def _build_http_message(fields: dict[str, object]) -> str:
    parts = [
        _single_line(fields["method"]),
        _single_line(fields["path"]),
        str(fields["status_code"]),
        f'{fields["duration_ms"]}ms',
    ]

    client_ip = fields.get("client_ip")
    if client_ip:
        client_port = fields.get("client_port")
        if client_port is not None:
            parts.append(f"client={client_ip}:{client_port}")
        else:
            parts.append(f"client={client_ip}")

    query = fields.get("query")
    if query:
        parts.append(f"query={_single_line(query)}")

    user_id = fields.get("user_id")
    if user_id is not None:
        parts.append(f"user_id={user_id}")

    parts.append(f'request_id={fields["request_id"]}')

    if "request_body" in fields:
        parts.append(f'request_body={_single_line(fields["request_body"])}')
    if "response_body" in fields:
        parts.append(f'response_body={_single_line(fields["response_body"])}')

    return " ".join(parts)


def _header_value(scope, header_name: bytes) -> str | None:
    for name, value in scope.get("headers", []):
        if name == header_name:
            return value.decode("latin-1").strip()
    return None


def _extract_client(scope) -> tuple[str | None, int | None]:
    forwarded_for = _header_value(scope, b"x-forwarded-for")
    if forwarded_for:
        first_hop = forwarded_for.split(",", 1)[0].strip()
        if first_hop:
            return first_hop, None

    real_ip = _header_value(scope, b"x-real-ip")
    if real_ip:
        return real_ip, None

    client = scope.get("client")
    if isinstance(client, (list, tuple)) and len(client) >= 2:
        return client[0], client[1]

    return None, None


def _redact_query(query_string: bytes) -> str:
    pairs = urllib.parse.parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)
    redacted = []
    for k, v in pairs:
        if any(s in k.lower() for s in _SECRET_KEYS):
            redacted.append((k, "[REDACTED]"))
        else:
            redacted.append((k, v))
    return urllib.parse.urlencode(redacted)


def _redact_dict(obj: object) -> object:
    if isinstance(obj, dict):
        return {k: "[REDACTED]" if any(s in k.lower() for s in _SECRET_KEYS) else _redact_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_dict(item) for item in obj]
    return obj


def _process_body(chunks: list[bytes], content_type: str) -> str:
    raw = b"".join(chunks)
    ct = content_type.lower()
    if "text/" in ct or "application/json" in ct or ct == "":
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return "[binary]"
        try:
            parsed = json.loads(text)
            redacted = _redact_dict(parsed)
            serialized = json.dumps(redacted)
            return serialized[:BODY_LOG_CAP]
        except (json.JSONDecodeError, ValueError):
            return text[:BODY_LOG_CAP]
    else:
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            return "[binary]"
        return raw.decode("utf-8")[:BODY_LOG_CAP]


class HTTPLoggingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        method = scope.get("method", "")
        path = scope.get("path", "")
        query_string = scope.get("query_string", b"")
        query = _redact_query(query_string)
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        log_body = os.environ.get("LOG_HTTP_BODY", "").lower() == "true"

        req_body_chunks: list[bytes] = []
        req_content_type = ""

        if log_body:
            for name, value in scope.get("headers", []):
                if name == b"content-type":
                    req_content_type = value.decode("latin-1").split(";")[0].strip()
                    break

            async def receive_wrapper():
                message = await receive()
                if message.get("type") == "http.request":
                    chunk = message.get("body", b"")
                    if sum(len(c) for c in req_body_chunks) + len(chunk) <= BODY_BUFFER_CAP:
                        req_body_chunks.append(chunk)
                return message

            effective_receive = receive_wrapper
        else:
            effective_receive = receive

        res_status: list[int | None] = [None]
        res_body_chunks: list[bytes] = []
        body_truncated = False
        body_byte_total = 0
        res_content_type = ""

        async def send_wrapper(message):
            nonlocal body_truncated, body_byte_total, res_content_type
            if message["type"] == "http.response.start":
                res_status[0] = message["status"]
                if log_body:
                    for name, value in message.get("headers", []):
                        if name == b"content-type":
                            res_content_type = value.decode("latin-1").split(";")[0].strip()
                            break
            elif message["type"] == "http.response.body" and log_body:
                chunk = message.get("body", b"")
                body_byte_total += len(chunk)
                if body_byte_total <= BODY_BUFFER_CAP:
                    res_body_chunks.append(chunk)
                else:
                    body_truncated = True
            await send(message)

        start = time.monotonic()
        try:
            await self.app(scope, effective_receive, send_wrapper)
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            status_code = res_status[0] if res_status[0] is not None else 500

            state = scope.get("state") or {}
            user = state.get("user") if isinstance(state, dict) else getattr(state, "user", None)
            user_id = getattr(user, "user_id", None)
            client_ip, client_port = _extract_client(scope)

            http_fields: dict = {
                "request_id": request_id,
                "method": method,
                "path": path,
                "query": query,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 3),
                "user_id": user_id,
                "client_ip": client_ip,
                "client_port": client_port,
            }

            if log_body:
                if req_body_chunks:
                    http_fields["request_body"] = _process_body(req_body_chunks, req_content_type)
                if body_truncated:
                    http_fields["response_body"] = "[body too large to log]"
                elif res_body_chunks:
                    http_fields["response_body"] = _process_body(res_body_chunks, res_content_type)

            logger.info(_build_http_message(http_fields), extra={"http": http_fields})
