from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def _request(base_url: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{exc.code} {exc.reason}: {exc.read().decode('utf-8', errors='replace')}") from exc
    if not body:
        return None
    return json.loads(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grounded", description="Grounded Mini-App Platform CLI companion")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    sub = parser.add_subparsers(dest="command", required=True)

    create_workspace = sub.add_parser("create-workspace")
    create_workspace.add_argument("name")
    create_workspace.add_argument("--description", default="")

    sub.add_parser("list-workspaces")

    run = sub.add_parser("run")
    run.add_argument("workspace_id")
    run.add_argument("prompt")
    run.add_argument("--mode", choices=["generate", "fix"], default="generate")
    run.add_argument("--generation-mode", choices=["fast", "balanced", "quality", "basic"], default="balanced")

    list_runs = sub.add_parser("list-runs")
    list_runs.add_argument("workspace_id")

    run_info = sub.add_parser("run-info")
    run_info.add_argument("run_id")

    diff = sub.add_parser("diff")
    diff.add_argument("run_id")

    apply = sub.add_parser("apply")
    apply.add_argument("run_id")

    rollback = sub.add_parser("rollback")
    rollback.add_argument("run_id")

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--run", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "create-workspace":
        result = _request(
            args.base_url,
            "POST",
            "/workspaces",
            {
                "name": args.name,
                "description": args.description or None,
                "target_platform": "telegram_mini_app",
                "preview_profile": "telegram_mock",
            },
        )
    elif args.command == "list-workspaces":
        result = _request(args.base_url, "GET", "/workspaces")
    elif args.command == "run":
        result = _request(
            args.base_url,
            "POST",
            f"/workspaces/{urllib.parse.quote(args.workspace_id)}/runs",
            {
                "prompt": args.prompt,
                "mode": args.mode,
                "generation_mode": args.generation_mode,
            },
        )
    elif args.command == "list-runs":
        result = _request(args.base_url, "GET", f"/workspaces/{urllib.parse.quote(args.workspace_id)}/runs")
    elif args.command == "run-info":
        result = _request(args.base_url, "GET", f"/runs/{urllib.parse.quote(args.run_id)}")
    elif args.command == "diff":
        result = _request(args.base_url, "GET", f"/runs/{urllib.parse.quote(args.run_id)}/diff")
    elif args.command == "apply":
        result = _request(args.base_url, "POST", f"/runs/{urllib.parse.quote(args.run_id)}/apply/staged", {})
    elif args.command == "rollback":
        result = _request(args.base_url, "POST", f"/runs/{urllib.parse.quote(args.run_id)}/rollback", {})
    elif args.command == "doctor":
        result = _request(args.base_url, "POST" if args.run else "GET", "/doctor/run" if args.run else "/doctor", {} if args.run else None)
    else:
        parser.error("unknown command")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
