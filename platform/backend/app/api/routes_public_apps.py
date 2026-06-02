from __future__ import annotations

import asyncio
import re
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app.api.deps import get_container
from app.services.container import ServiceContainer

router = APIRouter(tags=["public-miniapps"])

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

TEXTUAL_TYPES = (
    "text/html",
    "text/css",
    "application/javascript",
    "text/javascript",
    "application/json",
)


@router.get("/public/apps/{workspace_id}/links")
def public_app_links(
    workspace_id: str,
    request: Request,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, object]:
    preview = container.preview_service.get(workspace_id)
    app_url = _public_app_url_for_request(request, workspace_id, container.preview_service.public_app_url(workspace_id, preview))
    return {
        "workspace_id": workspace_id,
        "url": app_url or preview.url,
        "role_urls": _role_urls_for_app(app_url) if app_url else container.preview_service.public_role_urls(workspace_id, preview),
        "runtime_status": preview.status,
        "runtime_mode": preview.runtime_mode,
        "runtime_url": preview.url,
    }


@router.get("/apps/{workspace_id}")
def public_app_root(
    workspace_id: str,
    request: Request,
    container: ServiceContainer = Depends(get_container),
) -> RedirectResponse:
    base_url = _public_app_url_for_request(request, workspace_id, container.preview_service.public_app_url(workspace_id))
    if base_url:
        return RedirectResponse(f"{base_url}/client", status_code=307)
    return RedirectResponse(f"/apps/{workspace_id}/client", status_code=307)


@router.api_route("/apps/{workspace_id}/{target_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_public_app(
    workspace_id: str,
    target_path: str,
    request: Request,
    container: ServiceContainer = Depends(get_container),
) -> Response:
    preview = await _preview_ready(workspace_id, container)
    if not preview.url:
        raise HTTPException(status_code=503, detail="Mini-app runtime is not ready yet.")

    target_url = _target_url(preview.url, target_path, request.url.query)
    body = await request.body()
    headers = _forward_headers(request.headers)
    timeout = httpx.Timeout(connect=4.0, read=60.0, write=30.0, pool=4.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        upstream = await client.request(request.method, target_url, content=body, headers=headers)

    response_headers = _response_headers(upstream.headers)
    content = upstream.content
    content_type = upstream.headers.get("content-type", "")
    if _should_rewrite(content_type):
        rewritten = _rewrite_public_paths(content.decode(upstream.encoding or "utf-8", errors="replace"), f"/apps/{workspace_id}")
        content = rewritten.encode("utf-8")
    return Response(content=content, status_code=upstream.status_code, headers=response_headers, media_type=content_type or None)


async def _preview_ready(workspace_id: str, container: ServiceContainer):
    preview = container.preview_service.get(workspace_id)
    if preview.status == "running" and preview.url:
        return preview
    container.preview_service.ensure_started(workspace_id)
    for _ in range(60):
        await asyncio.sleep(0.5)
        preview = container.preview_service.get(workspace_id)
        if preview.status == "running" and preview.url:
            return preview
        if preview.status == "error":
            raise HTTPException(status_code=503, detail=preview.last_error or "Mini-app runtime failed to start.")
    raise HTTPException(status_code=503, detail="Mini-app runtime is still starting.")


def _target_url(runtime_url: str, target_path: str, query: str) -> str:
    normalized_path = (target_path or "client").lstrip("/")
    encoded_path = quote(normalized_path, safe="/:@")
    url = f"{runtime_url.rstrip('/')}/{encoded_path}"
    return f"{url}?{query}" if query else url


def _forward_headers(headers) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in HOP_BY_HOP_HEADERS or lowered in {"host", "content-length"}:
            continue
        forwarded[key] = value
    forwarded.setdefault("X-Forwarded-Proto", "https")
    return forwarded


def _response_headers(headers) -> dict[str, str]:
    returned: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in HOP_BY_HOP_HEADERS or lowered == "content-length":
            continue
        returned[key] = value
    return returned


def _public_app_url_for_request(request: Request, workspace_id: str, configured_url: str | None = None) -> str | None:
    configured = (configured_url or "").strip().rstrip("/")
    host = _request_host(request)
    if configured and (not host or host == "testserver" or urlparse(configured).netloc.lower() == host.lower()):
        return configured
    origin = _request_origin(request)
    if origin:
        return f"{origin}/apps/{workspace_id}"
    return configured or None


def _role_urls_for_app(app_url: str) -> dict[str, str]:
    base_url = app_url.rstrip("/")
    return {role: f"{base_url}/{role}" for role in ("client", "specialist", "manager")}


def _request_origin(request: Request) -> str:
    scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",", 1)[0].strip()
    host = _request_host(request)
    return f"{scheme}://{host}" if host else ""


def _request_host(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return host.split(",", 1)[0].strip()


def _should_rewrite(content_type: str) -> bool:
    normalized = content_type.lower().split(";", 1)[0].strip()
    return normalized in TEXTUAL_TYPES


def _rewrite_public_paths(content: str, prefix: str) -> str:
    # Generated mini-apps commonly use absolute /api and /static paths. Public
    # path hosting keeps those requests inside the workspace-specific proxy.
    replacements = {
        'href="/': f'href="{prefix}/',
        'src="/': f'src="{prefix}/',
        'action="/': f'action="{prefix}/',
        "href='/": f"href='{prefix}/",
        "src='/": f"src='{prefix}/",
        "action='/": f"action='{prefix}/",
        'fetch("/': f'fetch("{prefix}/',
        "fetch('/": f"fetch('{prefix}/",
        'url("/': f'url("{prefix}/',
        "url('/": f"url('{prefix}/",
        "url(/": f"url({prefix}/",
    }
    rewritten = content
    for old, new in replacements.items():
        rewritten = rewritten.replace(old, new)
    rewritten = re.sub(r'(?P<quote>["\'])/(api|static|assets|client|specialist|manager|health)\b', rf"\g<quote>{prefix}/\2", rewritten)
    rewritten = rewritten.replace(f"{prefix}/{prefix.lstrip('/')}/", f"{prefix}/")
    return rewritten
