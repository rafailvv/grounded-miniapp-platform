from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def api_error_payload(
    *,
    code: str,
    message: str,
    status_code: int,
    retryable: bool = False,
    deterministic: bool = True,
    failure_class: str | None = None,
    failure_signature: str | None = None,
    artifact_refs: list[str] | None = None,
    details: dict[str, Any] | list[Any] | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    error = {
        "code": code,
        "message": message,
        "status_code": status_code,
        "retryable": bool(retryable),
        "deterministic": bool(deterministic),
        "failure_class": failure_class or f"api.{code}",
        "failure_signature": failure_signature or f"api.{code}",
        "artifact_refs": list(artifact_refs or []),
        "details": details or {},
    }
    if path:
        error["path"] = path
    return {
        "status": "failed",
        "blocking": True,
        "issues": [
            {
                "kind": "api_error",
                "check": code,
                "details": message,
                "blocking": True,
                "evidence": error,
            }
        ],
        "error": error,
    }


def _message_from_detail(detail: Any) -> str:
    if isinstance(detail, dict):
        if isinstance(detail.get("message"), str):
            return detail["message"]
        if isinstance(detail.get("error"), dict) and isinstance(detail["error"].get("message"), str):
            return detail["error"]["message"]
    if isinstance(detail, list):
        return "Request validation failed."
    text = str(detail or "").strip()
    return text or "Request failed."


def _code_for_status(status_code: int) -> str:
    if status_code == 400:
        return "invalid_request"
    if status_code == 401:
        return "unauthorized"
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if status_code == 409:
        return "conflict"
    if status_code == 422:
        return "validation_error"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "internal_error"
    return "api_error"


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        payload = dict(detail)
        payload.setdefault("status", "failed")
        payload.setdefault("blocking", True)
        payload.setdefault("issues", [])
    else:
        code = _code_for_status(exc.status_code)
        payload = api_error_payload(
            code=code,
            message=_message_from_detail(detail),
            status_code=exc.status_code,
            retryable=exc.status_code in {408, 409, 429} or exc.status_code >= 500,
            deterministic=exc.status_code < 500,
            failure_class=f"api.{code}",
            failure_signature=f"api.{code}:{request.url.path}",
            details={"detail": detail} if detail is not None else {},
            path=request.url.path,
        )
    return JSONResponse(status_code=exc.status_code, content=payload, headers=getattr(exc, "headers", None))


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    payload = api_error_payload(
        code="validation_error",
        message="Request validation failed.",
        status_code=422,
        retryable=False,
        deterministic=True,
        failure_class="api.validation_error",
        failure_signature=f"api.validation_error:{request.url.path}",
        details={"errors": exc.errors()},
        path=request.url.path,
    )
    return JSONResponse(status_code=422, content=payload)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    payload = api_error_payload(
        code="internal_error",
        message=str(exc) or "Unhandled server error.",
        status_code=500,
        retryable=False,
        deterministic=False,
        failure_class=f"api.{type(exc).__name__}",
        failure_signature=f"api.{type(exc).__name__}:{request.url.path}",
        details={"error_type": type(exc).__name__},
        path=request.url.path,
    )
    return JSONResponse(status_code=500, content=payload)
