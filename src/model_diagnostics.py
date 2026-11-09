"""Bounded provider failure metadata; never persist exception bodies or messages."""

import json
import time
from pathlib import Path


_TYPES = {
    "TimeoutError",
    "APITimeoutError",
    "ReadTimeout",
    "ConnectTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "APIConnectionError",
    "ConnectError",
    "ReadError",
    "RemoteProtocolError",
    "APIStatusError",
    "BadRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    "APIResponseValidationError",
    "ValueError",
}
_TIMEOUTS = {
    "TimeoutError",
    "APITimeoutError",
    "ReadTimeout",
    "ConnectTimeout",
    "WriteTimeout",
    "PoolTimeout",
}
_NETWORK = {"APIConnectionError", "ConnectError", "ReadError", "RemoteProtocolError"}


def model_error_details(error):
    name = type(error).__name__
    status = getattr(error, "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    if isinstance(error, TimeoutError) or name in _TIMEOUTS:
        kind, message = "timeout", "模型接口响应超时"
    elif name in _NETWORK:
        kind, message = "connection", "连接模型接口失败或中断"
    elif status in {401, 402, 403, 429}:
        kind, message = {
            401: ("authentication", "API Key 无效"),
            402: ("balance", "DeepSeek 余额不足"),
            403: ("permission", "模型接口拒绝访问"),
            429: ("rate_limit", "DeepSeek 请求限流"),
        }[status]
    elif status and status >= 500:
        kind, message = "upstream_service", f"DeepSeek 服务暂时异常（HTTP {status}）"
    elif status and status >= 400:
        kind, message = "request", f"模型接口拒绝请求（HTTP {status}）"
    else:
        kind, message = "unexpected", "模型请求处理异常"
    return {
        "kind": kind,
        "exception_type": name if name in _TYPES else "OtherError",
        "http_status": status,
        "user_message": message,
    }


def record_model_error(directory: Path, error, *, usage_id, round_index, elapsed):
    record = {
        "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phase": "model_request",
        "usage_id": int(usage_id),
        "round_index": int(round_index),
        "elapsed_seconds": round(max(0, elapsed), 3),
        **model_error_details(error),
    }
    # A failed diagnostics write must never prevent an ordinary QQ error reply.
    try:
        path = directory / "model-errors.jsonl"
        if path.exists() and path.stat().st_size >= 262144:
            path.replace(directory / "model-errors.previous.jsonl")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return record
