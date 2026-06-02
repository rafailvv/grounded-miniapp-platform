# Grounded Mini-App Platform

This repository runs mini-app generation through one universal workspace code agent.

The active path is:

`user prompt -> workspace code agent -> draft patch -> platform checks -> apply -> preview`

The agent reads the workspace, edits files through draft operations, runs checks, inspects failures, and repeats until the draft is safe to apply. Product semantics come only from the user prompt. The base template is technical shell context, not a product model.

## Active Runtime

- Backend API: `platform/backend/app/api/`
- Agent runtime: `platform/backend/app/modules/workspace_code_agent_runtime/`
- Agent loop primitives: `platform/backend/app/modules/miniapp_agent_loop/`
- Workspace storage and draft/apply: `platform/backend/app/services/workspace/`
- Platform checks: `platform/backend/app/services/check_runner.py`
- Validators: `platform/backend/app/validators/`
- Frontend: `platform/frontend/`
- Base mini-app template: `runtime/templates/base-miniapp/`

## Generation Enhancement Layer

The platform now exposes reusable generation-quality primitives inspired by mature coding-agent workflows:

- Product config contract: `runtime/platform.config.json` defines generation modes, required checks, model profiles, skill activation, browser proof, and SLA policy. Its generated schema is `platform/backend/app/schemas/platform.config.schema.json`.
- Project instructions: `AGENTS.md` and template `AGENTS.md` are loaded into context summaries.
- Runtime skill packs: `runtime/skills/*/SKILL.md` provide focused guidance for Telegram product generation, FastAPI persistence, mobile polish, repair, browser proof, and existing-app edits.
- Persistent workspace memory: `/workspaces/{id}/memory` stores preferences, decisions, known failures, and stale-reference checks.
- Slash commands: `/slash-commands` lists Workbench command contracts such as `/generate`, `/fix`, `/polish`, `/review`, `/acceptance`, `/visual-qa`, and `/docs`.
- Acceptance scenarios: `/runs/{id}/acceptance-scenarios` derives proof scenarios from the run contract.
- Visual QA: `/runs/{id}/visual-qa` combines static mobile checks with browser mobile diagnostics.
- Trace reducer: `/runs/{id}/trace-reducer` summarizes phases, blockers, quality signals, changed files, and next action.
- Magic Docs: `/workspaces/{id}/magic-docs/product-architecture` previews or writes the current product architecture doc.
- Worker roles: `/system/worker-roles` formalizes planner, backend, role UI, tests, and verifier ownership.

## Generation Contract

Generation, edit, fix, visual change, retry, and failed-check apply all use `WorkspaceCodeAgentRuntime`.

Fresh successful runs must reach:

`running -> agent_turn -> checks -> applying -> complete`

Terminal success requires:

- `status=completed`
- `apply_status=applied`
- platform invariant checks passed
- contract smoke passed
- draft diff applied to the source workspace

## Workspace Creation

`POST /workspaces` is the cold-create path. It creates workspace storage, clones the base template, indexes files, and starts preview once. The frontend opens existing workspaces with `GET /workspaces/{id}`.

## Public Run APIs

- `POST /workspaces/{id}/runs`
- `POST /workspaces/{id}/generate`
- `GET /jobs/{id}`
- `POST /jobs/{id}/retry`
- `GET /runs/{id}/artifacts`
- `GET /runs/{id}/iterations`
- `GET /runs/{id}/checks`
- `GET /runs/{id}/patch`

Run artifacts are agent-native: `run`, `job`, `iterations`, `checks`, `patch`, `diff`, `trace`, and `preview`.

## Checks

Backend:

```bash
PYTHONPATH=platform/backend python3 -m compileall -q platform/backend/app
PYTHONPATH=platform/backend pytest -q platform/backend/tests
```

Static cleanup is covered by the backend test suite.
