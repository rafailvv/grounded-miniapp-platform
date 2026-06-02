# Grounded Mini-App Platform

Grounded Mini-App Platform is a local workbench for generating, repairing, validating, and previewing Telegram-style mini-apps from natural-language product prompts.

The active runtime is a workspace code agent, not a loose chat-to-code demo:

```text
prompt -> workspace code agent -> draft patch -> checks and gates -> apply -> live preview
```

The agent works inside a cloned mini-app workspace, edits files through draft/apply operations, runs platform checks, inspects failures, and repeats until the draft is safe to apply. Product semantics come from the user prompt and generated contract, while the base template stays a technical shell.

## Current Architecture

- Backend API: `platform/backend/app/api/`
- Backend services: `platform/backend/app/services/`
- Workspace storage, preview, draft/apply: `platform/backend/app/services/workspace/`
- Agent runtime: `platform/backend/app/modules/workspace_code_agent_runtime/`
- Agent loop, tools, workers, repair packets: `platform/backend/app/modules/miniapp_agent_loop/`
- Generated-app validation: `platform/backend/app/services/check_runner.py`
- Static validators: `platform/backend/app/validators/`
- Platform UI: `platform/frontend/`
- Base mini-app template: `runtime/templates/base-miniapp/`
- Runtime config, checks, models, mode SLAs: `runtime/platform.config.json`
- Runtime skills and prompt guidance: `runtime/skills/*/SKILL.md`
- Execution policy: `runtime/policies/agent_exec_policy.codexpolicy`

The mini-app target is a single FastAPI app serving role-specific mobile pages:

- `/client`
- `/specialist`
- `/manager`

Each role owns static HTML/CSS/JS under `miniapp/app/static/{role}/`, with shared backend routes and shared persistent state.

## Generation Modes

Generation behavior is driven by `runtime/platform.config.json`.

- `basic`: minimal scaffold/sanity path.
- `fast`: compact happy-path generation with real API/browser proof.
- `balanced`: role coverage, persistence, generated tests, and mobile usability.
- `quality`: deeper browser proof, visual checks, edge states, and worker branch depth.
- `production`: release-style gate with security, export, docs, regression, and audit evidence.

The default mode in the frontend is Balanced.

### Model Routing

Current OpenAI routing separates coding from lightweight support tasks:

- Code-writing roles (`agent_turn`, `code_edit`, `repair`): `gpt-5.2-codex`
- Summary/support roles (`summarize`, `cheap_task`): `gpt-4.1-mini`
- Embeddings: `text-embedding-3-large`

Environment overrides are supported through:

- `OPENAI_CODE_FAST_MODEL`
- `OPENAI_CODE_BALANCED_MODEL`
- `OPENAI_CODE_QUALITY_MODEL`
- `OPENAI_CODE_REPAIR_MODEL`
- `OPENAI_CODE_SUMMARY_MODEL`
- `OPENAI_CODE_MINI_MODEL`
- `OPENAI_CODE_MAX_MODEL`

## Generation Quality Layer

The platform includes generation primitives that make runs inspectable and repairable:

- Prompt contracts and mini-app route manifests.
- Product task ledgers for required role/backend/test work.
- Repair cases with evidence, likely files, focused checks, and retry policy.
- Worker roles for planner, backend, role UI, tests, verifier, and mobile polish.
- Guardian and readiness gates before apply.
- Context pressure and compaction reports.
- Browser proof and replayable acceptance artifacts.
- Visual QA and mobile overflow checks.
- Trace bundles, rollout trace, final reports, and run state diagnostics.

Successful fresh runs must reach:

```text
running -> agent/workers -> checks -> applying -> completed
```

Terminal success means:

- `status=completed`
- `apply_status=applied`
- required checks passed for the selected mode
- draft diff was applied to source workspace
- preview can load the generated app

## Workspaces

`POST /workspaces` creates a workspace by cloning `runtime/templates/base-miniapp`, initializing git, indexing files, and starting preview state.

Workspace data is stored under `data/workspaces/{workspace_id}`. The repo also includes a Bloom starter/demo workspace path used by the platform when starter bootstrap is enabled.

Core workspace APIs:

- `GET /workspaces`
- `POST /workspaces`
- `GET /workspaces/{workspace_id}`
- `DELETE /workspaces/{workspace_id}`
- `GET /workspaces/{workspace_id}/files/tree`
- `GET /workspaces/{workspace_id}/files/content`
- `POST /workspaces/{workspace_id}/files/save`
- `GET /workspaces/{workspace_id}/git/status`

## Runs

Runs are the main generation/edit/fix lifecycle objects.

Core run APIs:

- `POST /workspaces/{workspace_id}/runs`
- `POST /workspaces/{workspace_id}/generate`
- `GET /workspaces/{workspace_id}/runs`
- `GET /runs/{run_id}`
- `POST /runs/{run_id}/stop`
- `POST /runs/{run_id}/resume`
- `GET /runs/{run_id}/checks`
- `GET /runs/{run_id}/patch`
- `GET /runs/{run_id}/diff`
- `GET /runs/{run_id}/artifacts`
- `GET /runs/{run_id}/iterations`
- `GET /runs/{run_id}/tasks`
- `GET /runs/{run_id}/repair-cases`
- `GET /runs/{run_id}/context-pressure`
- `GET /runs/{run_id}/final-report`

Runs are intentionally draft-first: generated changes are validated in an isolated draft before being applied to the source workspace.

## Preview

Preview is managed per workspace and can run locally or through Docker depending on `PREVIEW_RUNTIME_MODE`.

For local development, use:

```env
PREVIEW_RUNTIME_MODE=local
PREVIEW_PORT_BASE=16000
```

Preview APIs:

- `POST /workspaces/{workspace_id}/preview/start`
- `POST /workspaces/{workspace_id}/preview/rebuild`
- `POST /workspaces/{workspace_id}/preview/reset`
- `GET /workspaces/{workspace_id}/preview/url`
- `GET /workspaces/{workspace_id}/preview/logs`
- `GET /workspaces/{workspace_id}/preview/runtime-boundary`

The frontend renders role URLs side by side so client, specialist, and manager flows can be inspected together.

## Local Development

Backend:

```bash
PYTHONPATH=platform/backend PREVIEW_RUNTIME_MODE=local \
  python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Frontend:

```bash
cd platform/frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5174
```

Platform URL:

```text
http://127.0.0.1:5174
```

## Checks

Backend checks:

```bash
PYTHONPATH=platform/backend python3 -m compileall -q platform/backend/app
PYTHONPATH=platform/backend pytest -q platform/backend/tests
```

Frontend checks:

```bash
cd platform/frontend
npm run build
```

Generated mini-app checks are mode-dependent and orchestrated by `CheckRunner`; typical proof includes API persistence, browser flow smoke, generated Python tests, generated JS tests, and optional visual regression.

## Notes For Contributors

- Keep generated product behavior prompt-derived; avoid hardcoding a specific business domain into platform stabilizers.
- Do not replace a usable generated app with a scaffold unless an explicit emergency fallback policy allows it.
- Prefer focused repair based on failing evidence over broad rewrites.
- Keep role JS, backend API, generated tests, and browser proof aligned; most failures are contract mismatches across those surfaces.
- Treat `data/` as runtime state, not source code, unless a specific fixture/starter workspace is intentionally versioned.
