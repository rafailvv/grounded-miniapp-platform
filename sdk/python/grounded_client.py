from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib import parse, request
import json


@dataclass
class GroundedClient:
    base_url: str = "http://127.0.0.1:8000"

    def list_workspaces(self) -> Any:
        return self._json("GET", "/workspaces")

    def create_run(self, workspace_id: str, payload: dict[str, Any]) -> Any:
        return self._json("POST", f"/workspaces/{parse.quote(workspace_id)}/runs", payload)

    def get_run(self, run_id: str) -> Any:
        return self._json("GET", f"/runs/{parse.quote(run_id)}")

    def timeline(self, run_id: str) -> Any:
        return self._json("GET", f"/runs/{parse.quote(run_id)}/timeline")

    def approvals(self, run_id: str) -> Any:
        return self._json("GET", f"/runs/{parse.quote(run_id)}/approvals")

    def approve(self, run_id: str, approval_id: str) -> Any:
        return self._json("POST", f"/runs/{parse.quote(run_id)}/approvals/{parse.quote(approval_id)}/approve")

    def search_files(self, workspace_id: str, query: str, run_id: str | None = None) -> Any:
        params = {"q": query}
        if run_id:
            params["run_id"] = run_id
        return self._json("GET", f"/workspaces/{parse.quote(workspace_id)}/files/search?{parse.urlencode(params)}")

    def export(self, workspace_id: str, kind: str) -> Any:
        return self._json("POST", f"/workspaces/{parse.quote(workspace_id)}/export/{kind}")

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
