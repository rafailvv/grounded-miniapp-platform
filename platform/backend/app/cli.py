from __future__ import annotations

import argparse
import json
import sys
import time
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
    parser = argparse.ArgumentParser(prog="grounded", description="Upmini AI Studio CLI companion")
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

    generate = sub.add_parser("generate")
    generate.add_argument("prompt")
    generate.add_argument("--workspace-id", default="")
    generate.add_argument("--workspace-name", default="Generated App")
    generate.add_argument("--generation-mode", choices=["fast", "balanced", "quality"], default="balanced")
    generate.add_argument("--quality", action="store_true")
    generate.add_argument("--export", action="store_true")
    generate.add_argument("--timeout-seconds", type=int, default=1800)

    fix = sub.add_parser("fix")
    fix.add_argument("workspace_id")
    fix.add_argument("--from-run", required=True)
    fix.add_argument("--prompt", default="Resume and repair the failed run using its checkpoint and repair signatures.")

    checks = sub.add_parser("checks")
    checks.add_argument("run_id")

    export_cmd = sub.add_parser("export")
    export_cmd.add_argument("workspace_id")
    export_cmd.add_argument("--kind", choices=["zip", "git-patch", "deploy-bundle", "docker-validation-report", "manifest", "browser-proof-bundle"], default="zip")

    final_report = sub.add_parser("final-report")
    final_report.add_argument("run_id")

    state = sub.add_parser("state")
    state.add_argument("run_id")

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
    elif args.command == "generate":
        workspace_id = args.workspace_id
        if not workspace_id:
            workspace = _request(
                args.base_url,
                "POST",
                "/workspaces",
                {
                    "name": args.workspace_name,
                    "description": "Created by grounded generate.",
                    "target_platform": "telegram_mini_app",
                    "preview_profile": "telegram_mock",
                },
            )
            workspace_id = workspace["workspace_id"]
        run_result = _request(
            args.base_url,
            "POST",
            f"/workspaces/{urllib.parse.quote(workspace_id)}/runs",
            {
                "prompt": args.prompt,
                "mode": "generate",
                "generation_mode": "quality" if args.quality else args.generation_mode,
            },
        )
        run_result = _wait_for_terminal_run(args.base_url, str(run_result["run_id"]), timeout_seconds=args.timeout_seconds)
        final = _request(args.base_url, "GET", f"/runs/{urllib.parse.quote(str(run_result['run_id']))}/final-report")
        result = {"workspace_id": workspace_id, "run": run_result, "final_report": final}
        if args.export:
            if final.get("status") != "passed":
                raise SystemExit("Reliability Gate did not pass; export is blocked by CLI.")
            result["export"] = _request(args.base_url, "POST", f"/workspaces/{urllib.parse.quote(workspace_id)}/export/zip", {})
    elif args.command == "fix":
        result = _request(
            args.base_url,
            "POST",
            f"/workspaces/{urllib.parse.quote(args.workspace_id)}/runs",
            {
                "prompt": args.prompt,
                "mode": "fix",
                "intent": "edit",
                "resume_from_run_id": args.from_run,
            },
        )
    elif args.command == "checks":
        result = _request(args.base_url, "GET", f"/runs/{urllib.parse.quote(args.run_id)}/gate")
    elif args.command == "export":
        result = _request(args.base_url, "POST", f"/workspaces/{urllib.parse.quote(args.workspace_id)}/export/{args.kind}", {})
    elif args.command == "final-report":
        result = _request(args.base_url, "GET", f"/runs/{urllib.parse.quote(args.run_id)}/final-report")
    elif args.command == "state":
        result = _request(args.base_url, "GET", f"/runs/{urllib.parse.quote(args.run_id)}/state")
    else:
        parser.error("unknown command")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _wait_for_terminal_run(base_url: str, run_id: str, *, timeout_seconds: int) -> dict[str, Any]:
    started = time.monotonic()
    while True:
        run = _request(base_url, "GET", f"/runs/{urllib.parse.quote(run_id)}")
        if str(run.get("status") or "") in {"completed", "blocked", "failed", "awaiting_approval"}:
            return run
        if time.monotonic() - started >= timeout_seconds:
            raise SystemExit(f"Run {run_id} did not reach a terminal state within {timeout_seconds}s.")
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
