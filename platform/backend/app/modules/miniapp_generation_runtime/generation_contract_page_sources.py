from __future__ import annotations

from app.modules.miniapp_contract.runtime_contract_sync import MiniappRuntimeContractSync


class MiniappGenerationContractPageSources:
    @staticmethod
    def _deterministic_client_page_route_source() -> str:
        return """from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse


router = APIRouter(prefix="/client", tags=["client"])

STATIC_ROOT = Path(__file__).resolve().parents[1] / "static" / "client"


def _static_file(relative_path: str) -> FileResponse:
    return FileResponse(STATIC_ROOT / relative_path)


@router.get("/")
def client_index() -> FileResponse:
    return _static_file("index.html")


@router.get("/create")
def client_create() -> FileResponse:
    return _static_file("create/index.html")


@router.get("/requests")
def client_requests() -> FileResponse:
    return _static_file("requests/index.html")


@router.get("/requests/{request_id}")
def client_requests_detail(request_id: str) -> FileResponse:
    return _static_file("requests_detail/index.html")


@router.get("/profile")
def client_profile() -> FileResponse:
    return _static_file("profile/index.html")
"""

    @staticmethod
    def _deterministic_specialist_page_route_source() -> str:
        return """from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse


router = APIRouter(prefix="/specialist", tags=["specialist"])

STATIC_ROOT = Path(__file__).resolve().parents[1] / "static" / "specialist"


def _static_file(relative_path: str) -> FileResponse:
    return FileResponse(STATIC_ROOT / relative_path)


@router.get("/")
def specialist_index() -> FileResponse:
    return _static_file("index.html")


@router.get("/requests")
def specialist_requests() -> FileResponse:
    return _static_file("requests/index.html")


@router.get("/requests/{request_id}")
def specialist_request_detail(request_id: str) -> FileResponse:
    return _static_file("requests_detail/index.html")


@router.get("/profile")
def specialist_profile() -> FileResponse:
    return _static_file("profile/index.html")
"""

    @staticmethod
    def _deterministic_manager_page_route_source() -> str:
        return """from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse


router = APIRouter(prefix="/manager", tags=["manager"])

STATIC_ROOT = Path(__file__).resolve().parents[1] / "static" / "manager"


def _static_file(relative_path: str) -> FileResponse:
    return FileResponse(STATIC_ROOT / relative_path)


@router.get("/")
def manager_index() -> FileResponse:
    return _static_file("index.html")


@router.get("/requests")
def manager_requests() -> FileResponse:
    return _static_file("requests/index.html")


@router.get("/requests/{request_id}")
def manager_request_detail(request_id: str) -> FileResponse:
    return _static_file("requests_detail/index.html")


@router.get("/workload")
def manager_workload() -> FileResponse:
    return _static_file("workload/index.html")


@router.get("/profile")
def manager_profile() -> FileResponse:
    return _static_file("profile/index.html")
"""

    @staticmethod
    def _deterministic_main_runtime_source(route_modules: list[str]) -> str:
        return MiniappRuntimeContractSync.deterministic_main_runtime_source(route_modules)
