from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

from app.repositories.state_store import StateStore
from app.services.workspace.service import WorkspaceService


FAILED_RUN_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
PENDING_CHECK_STATES = {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"}
TRUSTED_AUTHOR_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
MERGE_BLOCKING_REVIEW_DECISIONS = {"REVIEW_REQUIRED", "CHANGES_REQUESTED"}
MERGE_CONFLICT_OR_BLOCKING_STATES = {"BLOCKED", "DIRTY", "DRAFT", "UNKNOWN"}
REVIEW_BOT_LOGIN_KEYWORDS = {"codex"}


class PrBabysitterError(RuntimeError):
    pass


class PrBabysitterService:
    """GitHub PR/CI watcher for exported app handoff workflows."""

    def __init__(self, *, store: StateStore, workspace_service: WorkspaceService) -> None:
        self.store = store
        self.workspace_service = workspace_service

    def snapshot(
        self,
        *,
        workspace_id: str,
        pr: str = "auto",
        repo: str | None = None,
        run_id: str | None = None,
        export_id: str | None = None,
        max_flaky_retries: int = 3,
        retry_failed_now: bool = False,
    ) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        try:
            report = self._snapshot(
                workspace_id=workspace_id,
                pr=pr or "auto",
                repo=repo,
                run_id=run_id,
                export_id=export_id,
                max_flaky_retries=max(0, int(max_flaky_retries or 3)),
                retry_failed_now=retry_failed_now,
            )
        except Exception as exc:
            report = self._blocked_report(
                workspace_id=workspace_id,
                pr=pr,
                repo=repo,
                run_id=run_id,
                export_id=export_id,
                error=exc,
            )
        self._store_latest(workspace_id, report)
        return report

    def list_reports(self, *, workspace_id: str, run_id: str | None = None) -> dict[str, Any]:
        self.workspace_service.get_workspace(workspace_id)
        items = [
            payload
            for key, payload in self.store.items("reports")
            if key.startswith(f"pr_babysitter:{workspace_id}:") and isinstance(payload, dict)
        ]
        if run_id:
            items = [item for item in items if str(item.get("run_id") or "") == run_id]
        items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return {
            "schema": "grounded.pr_babysitter_index.v1",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "status": "ok",
            "items": items[:50],
            "latest": items[0] if items else None,
        }

    def _snapshot(
        self,
        *,
        workspace_id: str,
        pr: str,
        repo: str | None,
        run_id: str | None,
        export_id: str | None,
        max_flaky_retries: int,
        retry_failed_now: bool,
    ) -> dict[str, Any]:
        resolved_pr = self._resolve_pr(pr, repo)
        state_key = self._state_key(workspace_id, resolved_pr)
        state = self._load_state(state_key)
        fresh_state = not bool(state.get("started_at"))
        state["started_at"] = state.get("started_at") or int(time.time())
        authenticated_login = self._authenticated_login()
        review_items = self._new_review_items(resolved_pr, state, fresh_state=fresh_state, authenticated_login=authenticated_login)
        checks = self._pr_checks(str(resolved_pr["number"]), resolved_pr["repo"])
        checks_summary = self._summarize_checks(checks)
        workflow_runs = self._workflow_runs_for_sha(resolved_pr["repo"], resolved_pr["head_sha"])
        failed_runs = self._failed_runs(workflow_runs, resolved_pr["head_sha"])
        retries_used = int(((state.get("retries_by_sha") or {}).get(resolved_pr["head_sha"]) or 0))
        actions = self._recommend_actions(resolved_pr, checks_summary, failed_runs, review_items, retries_used, max_flaky_retries)
        rerun_result = None
        if retry_failed_now and "retry_failed_checks" in actions:
            rerun_result = self._retry_failed_runs(resolved_pr, state, state_key, failed_runs, retries_used)
            retries_used = int(((state.get("retries_by_sha") or {}).get(resolved_pr["head_sha"]) or retries_used))
        state["pr"] = {"repo": resolved_pr["repo"], "number": resolved_pr["number"]}
        state["last_seen_head_sha"] = resolved_pr["head_sha"]
        state["last_snapshot_at"] = int(time.time())
        self.store.upsert("reports", state_key, state)
        report = {
            "schema": "grounded.pr_babysitter.v1",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "export_id": export_id,
            "status": self._status_for(actions),
            "pr": resolved_pr,
            "checks": checks_summary,
            "checks_items": checks[:100],
            "failed_runs": failed_runs,
            "new_review_items": review_items,
            "actions": actions,
            "retry_state": {"current_sha_retries_used": retries_used, "max_flaky_retries": max_flaky_retries},
            "failure_diagnostics": self._failure_diagnostics(failed_runs, checks),
            "automation_plan": self._automation_plan(actions, resolved_pr, run_id=run_id, export_id=export_id),
            "rerun_result": rerun_result,
            "state_ref": state_key,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert("reports", self._report_key(workspace_id, resolved_pr), report)
        return report

    def _blocked_report(self, *, workspace_id: str, pr: str, repo: str | None, run_id: str | None, export_id: str | None, error: Exception) -> dict[str, Any]:
        return {
            "schema": "grounded.pr_babysitter.v1",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "export_id": export_id,
            "status": "blocked",
            "input": {"pr": pr, "repo": repo},
            "actions": ["stop_user_help_required"],
            "blocker": {
                "reason": "github_cli_or_permission_error",
                "message": str(error),
                "required_action": "Configure gh auth/repo access or provide an explicit PR URL/number.",
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _store_latest(self, workspace_id: str, report: dict[str, Any]) -> None:
        self.store.upsert("reports", f"pr_babysitter_latest:{workspace_id}", report)

    def _resolve_pr(self, pr: str, repo: str | None) -> dict[str, Any]:
        parsed = self._parse_pr(pr)
        cmd = ["pr", "view"]
        if parsed["value"]:
            cmd.append(str(parsed["value"]))
        cmd.extend(["--json", "number,url,state,mergedAt,closedAt,headRefName,headRefOid,headRepository,headRepositoryOwner,mergeable,mergeStateStatus,reviewDecision"])
        data = self._gh_json(cmd, repo=repo)
        if not isinstance(data, dict):
            raise PrBabysitterError("Unexpected PR payload from gh pr view.")
        pr_url = str(data.get("url") or "")
        resolved_repo = repo or self._repo_from_pr_url(pr_url) or self._repo_from_pr_view(data)
        if not resolved_repo:
            raise PrBabysitterError("Unable to resolve OWNER/REPO for PR.")
        return {
            "number": int(data["number"]),
            "url": pr_url,
            "repo": resolved_repo,
            "head_sha": str(data.get("headRefOid") or ""),
            "head_branch": str(data.get("headRefName") or ""),
            "state": str(data.get("state") or ""),
            "merged": bool(data.get("mergedAt")),
            "closed": bool(data.get("closedAt")) or str(data.get("state") or "").upper() == "CLOSED",
            "mergeable": str(data.get("mergeable") or ""),
            "merge_state_status": str(data.get("mergeStateStatus") or ""),
            "review_decision": str(data.get("reviewDecision") or ""),
        }

    def _pr_checks(self, pr_number: str, repo: str) -> list[dict[str, Any]]:
        data = self._gh_json(["pr", "checks", pr_number, "--json", "name,state,bucket,link,workflow,event,startedAt,completedAt"], repo=repo)
        return data if isinstance(data, list) else []

    def _workflow_runs_for_sha(self, repo: str, sha: str) -> list[dict[str, Any]]:
        data = self._gh_json(["api", f"repos/{repo}/actions/runs", "-X", "GET", "-f", f"head_sha={sha}", "-f", "per_page=100"])
        return list(data.get("workflow_runs") or []) if isinstance(data, dict) else []

    def _new_review_items(self, pr: dict[str, Any], state: dict[str, Any], *, fresh_state: bool, authenticated_login: str) -> list[dict[str, Any]]:
        del fresh_state
        repo = str(pr["repo"])
        number = int(pr["number"])
        issue = self._normalize_issue_comments(self._gh_api_list(f"repos/{repo}/issues/{number}/comments"))
        comments = self._normalize_review_comments(self._gh_api_list(f"repos/{repo}/pulls/{number}/comments"))
        reviews = self._normalize_reviews(self._gh_api_list(f"repos/{repo}/pulls/{number}/reviews"))
        seen = {
            "issue_comment": {str(item) for item in state.get("seen_issue_comment_ids") or []},
            "review_comment": {str(item) for item in state.get("seen_review_comment_ids") or []},
            "review": {str(item) for item in state.get("seen_review_ids") or []},
        }
        out: list[dict[str, Any]] = []
        for item in [*issue, *comments, *reviews]:
            kind = str(item.get("kind") or "")
            item_id = str(item.get("id") or "")
            author = str(item.get("author") or "")
            if not item_id or not author or item_id in seen.get(kind, set()):
                continue
            if self._is_bot(author):
                if not any(keyword in author.lower() for keyword in REVIEW_BOT_LOGIN_KEYWORDS):
                    continue
            elif not self._trusted_author(item, authenticated_login):
                continue
            out.append(item)
            seen.setdefault(kind, set()).add(item_id)
        state["seen_issue_comment_ids"] = sorted(seen["issue_comment"])
        state["seen_review_comment_ids"] = sorted(seen["review_comment"])
        state["seen_review_ids"] = sorted(seen["review"])
        out.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("kind") or ""), str(item.get("id") or "")))
        return out

    def _retry_failed_runs(self, pr: dict[str, Any], state: dict[str, Any], state_key: str, failed_runs: list[dict[str, Any]], retries_used: int) -> dict[str, Any]:
        rerun_ids: list[Any] = []
        for run in failed_runs:
            run_id = run.get("run_id")
            if run_id in (None, ""):
                continue
            self._gh_text(["run", "rerun", str(run_id), "--failed"], repo=str(pr["repo"]))
            rerun_ids.append(run_id)
        if rerun_ids:
            retries = state.get("retries_by_sha") if isinstance(state.get("retries_by_sha"), dict) else {}
            retries[str(pr["head_sha"])] = retries_used + 1
            state["retries_by_sha"] = retries
            self.store.upsert("reports", state_key, state)
        return {"rerun_attempted": bool(rerun_ids), "rerun_run_ids": rerun_ids, "rerun_count": len(rerun_ids)}

    @staticmethod
    def _summarize_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
        pending = failed = passed = 0
        for check in checks:
            bucket = str(check.get("bucket") or "").lower()
            state = str(check.get("state") or "").upper()
            if bucket == "pending" or state in PENDING_CHECK_STATES:
                pending += 1
            if bucket == "fail":
                failed += 1
            if bucket == "pass":
                passed += 1
        return {"pending_count": pending, "failed_count": failed, "passed_count": passed, "all_terminal": pending == 0}

    @staticmethod
    def _failed_runs(runs: list[dict[str, Any]], sha: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for run in runs:
            if str(run.get("head_sha") or "") != sha:
                continue
            conclusion = str(run.get("conclusion") or "")
            if conclusion not in FAILED_RUN_CONCLUSIONS:
                continue
            out.append(
                {
                    "run_id": run.get("id"),
                    "workflow_name": run.get("name") or run.get("display_title") or "",
                    "status": str(run.get("status") or ""),
                    "conclusion": conclusion,
                    "html_url": str(run.get("html_url") or ""),
                }
            )
        return sorted(out, key=lambda item: (str(item.get("workflow_name") or ""), str(item.get("run_id") or "")))

    @classmethod
    def _recommend_actions(cls, pr: dict[str, Any], checks: dict[str, Any], failed_runs: list[dict[str, Any]], reviews: list[dict[str, Any]], retries_used: int, max_retries: int) -> list[str]:
        actions: list[str] = []
        if pr.get("closed") or pr.get("merged"):
            if reviews:
                actions.append("process_review_comment")
            actions.append("stop_pr_closed")
            return actions
        if cls._ready_to_merge(pr, checks, reviews):
            return ["ready_to_merge"]
        if reviews:
            actions.append("process_review_comment")
        if int(checks.get("failed_count") or 0) > 0:
            if checks.get("all_terminal") and retries_used >= max_retries:
                actions.append("stop_exhausted_retries")
            else:
                actions.append("diagnose_ci_failure")
                if checks.get("all_terminal") and failed_runs and retries_used < max_retries:
                    actions.append("retry_failed_checks")
        return actions or ["idle"]

    @staticmethod
    def _ready_to_merge(pr: dict[str, Any], checks: dict[str, Any], reviews: list[dict[str, Any]]) -> bool:
        return (
            not pr.get("closed")
            and not pr.get("merged")
            and bool(checks.get("all_terminal"))
            and int(checks.get("failed_count") or 0) == 0
            and int(checks.get("pending_count") or 0) == 0
            and not reviews
            and str(pr.get("mergeable") or "") == "MERGEABLE"
            and str(pr.get("merge_state_status") or "") not in MERGE_CONFLICT_OR_BLOCKING_STATES
            and str(pr.get("review_decision") or "") not in MERGE_BLOCKING_REVIEW_DECISIONS
        )

    @staticmethod
    def _status_for(actions: list[str]) -> str:
        if any(action.startswith("stop_") for action in actions):
            return "stopped"
        if "ready_to_merge" in actions:
            return "ready"
        if "diagnose_ci_failure" in actions or "process_review_comment" in actions:
            return "needs_action"
        return "watching"

    @staticmethod
    def _failure_diagnostics(failed_runs: list[dict[str, Any]], checks: list[dict[str, Any]]) -> dict[str, Any]:
        names = " ".join(str(item.get("workflow_name") or item.get("name") or "") for item in [*failed_runs, *checks]).lower()
        flaky_markers = ("timeout", "timed out", "startup", "network", "registry", "rate limit", "runner")
        branch_markers = ("test", "lint", "type", "build", "compile", "pytest", "vitest", "tsc")
        likely_flaky = any(marker in names for marker in flaky_markers) or any(str(item.get("conclusion")) in {"timed_out", "startup_failure"} for item in failed_runs)
        likely_branch = any(marker in names for marker in branch_markers) and not likely_flaky
        return {
            "classification": "likely_flaky_or_infra" if likely_flaky else "likely_branch_related" if likely_branch else "unknown_needs_log_inspection",
            "log_commands": [f"gh run view {item.get('run_id')} --log-failed" for item in failed_runs if item.get("run_id")],
            "inspect_commands": [f"gh run view {item.get('run_id')} --json jobs,name,workflowName,conclusion,status,url,headSha" for item in failed_runs if item.get("run_id")],
        }

    @staticmethod
    def _automation_plan(actions: list[str], pr: dict[str, Any], *, run_id: str | None, export_id: str | None) -> dict[str, Any]:
        return {
            "schema": "grounded.pr_babysitter_plan.v1",
            "priority_order": ["process_review_comment", "diagnose_ci_failure", "retry_failed_checks", "ready_to_merge"],
            "next_action": actions[0] if actions else "idle",
            "auto_fix_push": {
                "enabled": bool(run_id) and ("diagnose_ci_failure" in actions or "process_review_comment" in actions),
                "source_run_id": run_id,
                "export_id": export_id,
                "commit_messages": {
                    "ci": f"codex: fix CI failure on PR #{pr.get('number')}",
                    "review": f"codex: address PR review feedback (#{pr.get('number')})",
                },
                "steps": [
                    "inspect failed logs or review comment",
                    "create focused repair run against source_run_id",
                    "export updated deploy bundle",
                    "commit and push PR head branch",
                    "restart PR babysitter on the new SHA",
                ],
            },
            "flaky_retry": {"command": "gh run rerun <run-id> --failed", "only_when": "retry_failed_checks"},
        }

    def _gh_json(self, args: list[str], repo: str | None = None) -> Any:
        raw = self._gh_text(args, repo=repo).strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PrBabysitterError(f"Failed to parse gh JSON for {' '.join(args)}") from exc

    @staticmethod
    def _gh_text(args: list[str], repo: str | None = None) -> str:
        cmd = ["gh"]
        if repo and (not args or args[0] != "api"):
            cmd.extend(["-R", repo])
        cmd.extend(args)
        try:
            proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise PrBabysitterError("`gh` command not found.") from exc
        except subprocess.CalledProcessError as exc:
            raise PrBabysitterError((exc.stderr or exc.stdout or str(exc)).strip()) from exc
        return proc.stdout

    def _gh_api_list(self, endpoint: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            sep = "&" if "?" in endpoint else "?"
            payload = self._gh_json(["api", f"{endpoint}{sep}per_page=100&page={page}"])
            if not isinstance(payload, list):
                break
            items.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                break
            page += 1
        return items

    @staticmethod
    def _parse_pr(value: str) -> dict[str, Any]:
        if value == "auto":
            return {"mode": "auto", "value": None}
        if re.fullmatch(r"\d+", value or ""):
            return {"mode": "number", "value": value}
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc and "/pull/" in parsed.path:
            return {"mode": "url", "value": value}
        raise PrBabysitterError("PR must be 'auto', a number, or a PR URL.")

    @staticmethod
    def _repo_from_pr_url(url: str) -> str | None:
        parts = [part for part in urlparse(url).path.split("/") if part]
        if len(parts) >= 4 and parts[2] == "pull":
            return f"{parts[0]}/{parts[1]}"
        return None

    @staticmethod
    def _repo_from_pr_view(data: dict[str, Any]) -> str | None:
        owner = data.get("headRepositoryOwner")
        repo = data.get("headRepository")
        owner_name = owner.get("login") if isinstance(owner, dict) else owner if isinstance(owner, str) else None
        repo_name = repo.get("name") if isinstance(repo, dict) else repo if isinstance(repo, str) else None
        return f"{owner_name}/{repo_name}" if owner_name and repo_name else None

    @staticmethod
    def _authenticated_login_from_payload(payload: Any) -> str:
        return str(payload.get("login") or "") if isinstance(payload, dict) else ""

    def _authenticated_login(self) -> str:
        return self._authenticated_login_from_payload(self._gh_json(["api", "user"]))

    @staticmethod
    def _normalize_issue_comments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"kind": "issue_comment", "id": str(item.get("id") or ""), "author": _login(item.get("user")), "author_association": str(item.get("author_association") or ""), "created_at": str(item.get("created_at") or ""), "body": str(item.get("body") or ""), "path": None, "line": None, "url": str(item.get("html_url") or "")}
            for item in items
        ]

    @staticmethod
    def _normalize_review_comments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"kind": "review_comment", "id": str(item.get("id") or ""), "author": _login(item.get("user")), "author_association": str(item.get("author_association") or ""), "created_at": str(item.get("created_at") or ""), "body": str(item.get("body") or ""), "path": item.get("path"), "line": item.get("line") or item.get("original_line"), "url": str(item.get("html_url") or "")}
            for item in items
        ]

    @staticmethod
    def _normalize_reviews(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"kind": "review", "id": str(item.get("id") or ""), "author": _login(item.get("user")), "author_association": str(item.get("author_association") or ""), "created_at": str(item.get("submitted_at") or item.get("created_at") or ""), "body": str(item.get("body") or ""), "path": None, "line": None, "url": str(item.get("html_url") or "")}
            for item in items
        ]

    @staticmethod
    def _is_bot(login: str) -> bool:
        return bool(login) and login.endswith("[bot]")

    @staticmethod
    def _trusted_author(item: dict[str, Any], login: str) -> bool:
        author = str(item.get("author") or "")
        return bool(author) and (author == login or str(item.get("author_association") or "").upper() in TRUSTED_AUTHOR_ASSOCIATIONS)

    @staticmethod
    def _state_key(workspace_id: str, pr: dict[str, Any]) -> str:
        return f"pr_babysitter_state:{workspace_id}:{_slug(pr.get('repo'))}:pr{pr.get('number')}"

    @staticmethod
    def _report_key(workspace_id: str, pr: dict[str, Any]) -> str:
        return f"pr_babysitter:{workspace_id}:{_slug(pr.get('repo'))}:pr{pr.get('number')}"

    def _load_state(self, key: str) -> dict[str, Any]:
        payload = self.store.get("reports", key)
        if isinstance(payload, dict):
            return payload
        return {"schema": "grounded.pr_babysitter_state.v1", "started_at": None, "retries_by_sha": {}, "seen_issue_comment_ids": [], "seen_review_comment_ids": [], "seen_review_ids": []}


def _login(user: Any) -> str:
    return str(user.get("login") or "") if isinstance(user, dict) else ""


def _slug(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "unknown")).strip("-") or "unknown"
