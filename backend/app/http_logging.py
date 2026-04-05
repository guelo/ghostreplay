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

_handler = logging.StreamHandler()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"level": record.levelname, **getattr(record, "http", {})})


_handler.setFormatter(_JsonFormatter())
logger = logging.getLogger("ghostreplay.http")
logger.addHandler(_handler)
logger.setLevel(logging.INFO)
logger.propagate = False


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

            user = scope.get("state", {}).get("user")
            user_id = getattr(user, "user_id", None)

            http_fields: dict = {
                "request_id": request_id,
                "method": method,
                "path": path,
                "query": query,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 3),
                "user_id": user_id,
            }

            if log_body:
                if req_body_chunks:
                    http_fields["request_body"] = _process_body(req_body_chunks, req_content_type)
                if body_truncated:
                    http_fields["response_body"] = "[body too large to log]"
                elif res_body_chunks:
                    http_fields["response_body"] = _process_body(res_body_chunks, res_content_type)

            logger.info("", extra={"http": http_fields})
