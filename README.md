# Grounded Mini-App Platform

Web-based research platform for grounded generation of mini-applications from project documentation, platform constraints, user prompts, and controlled compilation rules.

The core product target is not "chat that writes code". The target is a workspace-oriented system where a user writes a prompt, the platform derives a grounded specification, validates it, compiles patch-based changes into a canonical mini-app template, and shows a live preview immediately. In the current implementation the canonical template is a three-role mini-app baseline with:

- `client`
- `specialist`
- `manager`

The platform preview reflects that directly: instead of one phone, the UI shows three phones side by side so all roles can be used simultaneously.

## Why This Project Exists

This repository is designed around the research framing you described:

- generation must be documentation-grounded;
- intermediate representations must be typed and validated;
- deterministic gates must decide whether code can proceed to compilation;
- assumptions, contradictions, and traceability must be explicit artifacts;
- preview must be a runtime-oriented experience, not a screenshot or synthetic HTML-only mock;
- manual edits must remain first-class and survive future AI iterations.

The implementation therefore follows a controlled pipeline:

`prompt -> retrieval -> GroundedSpec -> AppIR -> validators -> ArtifactPlan -> patch apply -> build/preview -> traceability`

That sequence was chosen deliberately because it gives three things a direct LLM-to-code approach does not give reliably:

1. Inspectable intermediate artifacts.
2. Deterministic blocking rules before code is applied.
3. A research-grade audit trail from prompt and docs to generated code and preview.

## What Has Been Implemented

The repository is now a greenfield monorepo with a working research MVP scaffold that includes:

- a FastAPI platform backend;
- a React platform frontend;
- versioned JSON contracts for `GroundedSpec` and `AppIR`;
- deterministic validators for spec, IR, platform, and build gates;
- a canonical `base-miniapp` template;
- bundled platform corpora for Telegram and MAX;
- file-backed workspaces with real git revisions;
- patch-based artifact compilation into the template;
- machine-readable validation, assumptions, and traceability reports;
- a triple-phone live preview surface for the three mini-app roles;
- a three-role mini-app template backend/frontend integration scaffold;
- per-workspace Docker preview orchestration with isolated runtime ports;
- live OpenRouter-backed structured generation with deterministic fallback.

## What Was Done and Why

### 1. Monorepo structure was created first

The repository started empty, so the first step was to define a stable top-level structure:

- `contracts/`
- `platform/backend/`
- `platform/frontend/`
- `runtime/templates/base-miniapp/`
- `runtime/platform-docs/`
- `docker/`
- `data/`

This was done first because the whole project depends on a clean separation between:

- platform code;
- generated runtime template code;
- research artifacts and schemas.

Without that separation, the platform and generated mini-app template would quickly collapse into one mixed codebase, making traceability, compilation boundaries, and future evaluation much harder.

### 2. Strict contracts were added before expanding generation behavior

The repository includes two versioned JSON Schema contracts:

- [contracts/grounded-spec.v1.json](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/contracts/grounded-spec.v1.json)
- [contracts/app-ir.v1.json](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/contracts/app-ir.v1.json)

And matching typed backend models:

- [platform/backend/app/models/grounded_spec.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/models/grounded_spec.py)
- [platform/backend/app/models/app_ir.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/models/app_ir.py)
- [platform/backend/app/models/artifacts.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/models/artifacts.py)

This was done because the platform needs clear contracts between:

- document intelligence and synthesis;
- synthesis and validation;
- validation and compilation;
- compilation and preview.

If these layers exchange loose dictionaries, then validation loses value and research claims about typed IR and grounded synthesis become weak. By making them strict Pydantic models and exporting schema files from the same source, the implementation keeps the external contract and internal backend types aligned.

### 3. Deterministic validator gates were implemented as blocking layers

Validator modules now exist in:

- [platform/backend/app/validators/grounded_spec_validator.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/validators/grounded_spec_validator.py)
- [platform/backend/app/validators/app_ir_validator.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/validators/app_ir_validator.py)
- [platform/backend/app/validators/platform_validator.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/validators/platform_validator.py)
- [platform/backend/app/validators/build_validator.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/validators/build_validator.py)
- [platform/backend/app/validators/suite.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/validators/suite.py)

This was done because correctness in this system must not depend on the model "probably being right". The intended architecture is hybrid:

- the LLM proposes structured artifacts;
- validators decide whether those artifacts are acceptable;
- compilation only proceeds if the gates pass.

Current checks already enforce key invariants such as:

- required spec sections exist;
- critical contradictions block generation;
- high-impact unknowns block generation;
- IR references must point to existing screens, variables, and integrations;
- trusted data cannot originate from raw user input;
- Telegram session flows require validated init data;
- generated role artifacts required by the current template must exist.

### 4. Workspace management was made revision-aware from the start

Workspace handling lives in:

- [platform/backend/app/services/workspace_service.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/workspace_service.py)

The platform creates a real workspace on disk, clones the canonical template into it, initializes git, and records revisions for:

- template clone;
- AI patch application;
- manual file edits;
- reset.

This was done because one of the critical product and research requirements is that manual edits must be first-class. If the platform rewrites the whole generated project every time, it breaks both product stability and the research claim about controlled patch-based synthesis.

### 5. Document intelligence was implemented as a required source-of-truth layer

Document ingestion and retrieval logic lives in:

- [platform/backend/app/services/document_intelligence.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/document_intelligence.py)

The current implementation is intentionally simple but structurally correct:

- it stores documents per workspace;
- splits them into chunks;
- indexes them in a file-backed store;
- retrieves chunks by lexical overlap;
- merges workspace docs with bundled template docs and platform docs;
- blocks generation if required template/platform corpora are missing.

This was done because your requirement is explicit: generation cannot rely only on the prompt. It must have mandatory documentation sources. Even though the current retrieval is not yet full hybrid lexical + embeddings + reranking, the architecture and hard blocking rule are already in place.

### 6. OpenRouter integration was added as a dedicated backend concern

The LLM gateway is represented by:

- [platform/backend/app/ai/openrouter_client.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/ai/openrouter_client.py)
- [platform/backend/app/ai/model_registry.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/ai/model_registry.py)
- prompt templates under [platform/backend/app/ai/prompts](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/ai/prompts)

This was done to keep model routing and prompting separate from orchestration logic. The platform should not hardcode provider-specific behavior into unrelated services. The registry keeps distinct roles for:

- spec analysis;
- IR/code generation;
- repair;
- cheap support tasks;
- embeddings.

The generation service now calls OpenRouter directly for the two main structured artifacts:

- `GroundedSpec`
- `AppIR`

The implementation keeps the provider-specific details isolated in the client:

- GPT-5.x models go through the OpenRouter `responses` endpoint for structured output;
- other models use chat completions with strict JSON Schema output;
- provider routing is sent with `allow_fallbacks=true`, `require_parameters=true`, and `data_collection=deny`.

The platform still keeps a deterministic synthesis fallback. That is deliberate: if `OPENROUTER_API_KEY` is absent or a provider call fails, the pipeline remains usable for local research instead of failing hard at prompt intake.

### 7. A canonical base mini-app template was implemented, then simplified to one miniapp runtime

The runtime template is under:

- [runtime/templates/base-miniapp](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp)

The current template is a single FastAPI-served miniapp with separate static pages per role:

- `client`
- `specialist`
- `manager`

The template miniapp now exposes:

- `GET /api/profiles/{role}`
- `PUT /api/profiles/{role}`
- `GET /health`
- `GET /client`
- `GET /client/profile`
- `GET /specialist`
- `GET /specialist/profile`
- `GET /manager`
- `GET /manager/profile`

Key files:

- [runtime/templates/base-miniapp/miniapp/app/main.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/main.py)
- [runtime/templates/base-miniapp/miniapp/app/db.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/db.py)
- [runtime/templates/base-miniapp/miniapp/app/routes/profiles.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/routes/profiles.py)
- [runtime/templates/base-miniapp/miniapp/app/static/preview-bridge.js](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/static/preview-bridge.js)

This was done because the template is the compilation target. If the target template already has real role routing and role pages, the backend and generated artifacts must respect that structure. Otherwise the platform would compile artifacts into a template that expects a different runtime model, which would create an architectural mismatch immediately.

### 8. Role pages are served directly from the miniapp runtime

The current template no longer keeps a separate generated frontend application. Instead, each role has its own static HTML, CSS, and JS files under the miniapp runtime:

- `/miniapp/app/static/client/*`
- `/miniapp/app/static/specialist/*`
- `/miniapp/app/static/manager/*`
- one shared preview bridge under `/miniapp/app/static/preview-bridge.js`

Key files:

- [runtime/templates/base-miniapp/miniapp/app/static/client/index.html](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/static/client/index.html)
- [runtime/templates/base-miniapp/miniapp/app/static/client/profile.html](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/static/client/profile.html)
- [runtime/templates/base-miniapp/miniapp/app/static/specialist/index.html](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/static/specialist/index.html)
- [runtime/templates/base-miniapp/miniapp/app/static/manager/index.html](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/static/manager/index.html)

This keeps the generation target flat and whole-file friendly instead of splitting a tiny miniapp into a separate frontend stack.

### 9. The platform preview was changed from one phone to three phones

Platform preview logic lives in:

- [platform/backend/app/services/preview_service.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/preview_service.py)
- [platform/backend/app/api/routes_preview.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/api/routes_preview.py)
- [platform/frontend/src/App.tsx](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/frontend/src/App.tsx)
- [platform/frontend/src/styles/app.css](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/frontend/src/styles/app.css)

The platform now returns per-role preview URLs and renders three role-labeled phones at once:

- first phone: `client`
- second phone: `specialist`
- third phone: `manager`

This was done because your requirement is not just that the template supports roles internally. The platform itself must let the user interact with all roles simultaneously during evaluation. That is especially important for a research prototype where cross-role workflows and side-by-side validation matter.

### 10. Per-workspace Docker runtime orchestration was implemented

Preview is no longer limited to the in-process fallback renderer. The backend now contains a runtime manager and a preview service that can start, rebuild, inspect, and reset a dedicated Docker stack per workspace:

- [platform/backend/app/services/runtime_manager.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/runtime_manager.py)
- [platform/backend/app/services/preview_service.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/preview_service.py)

The canonical template now includes the runtime assets required for that lifecycle:

- [runtime/templates/base-miniapp/miniapp/Dockerfile](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/Dockerfile)
- [runtime/templates/base-miniapp/docker/docker-compose.yml](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/docker/docker-compose.yml)

Each workspace runtime gets:

- an isolated compose project name;
- an allocated proxy port;
- a single miniapp preview container;
- health-check polling before the preview is marked ready;
- `rebuild` after generation and `reset` for cleanup.

The inline preview path still exists, but now only as a safe fallback for tests or machines where Docker is unavailable.

### 11. The compiler output was adapted to the three-role template

Artifact generation in:

- [platform/backend/app/services/generation_service.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/generation_service.py)

no longer writes only a single preview payload. It now writes:

- `artifacts/grounded_spec.json`
- `miniapp/app/generated/app_ir.json`
- `miniapp/app/generated/static_runtime_manifest.json`
- `miniapp/app/generated/role_experience.json`
- `artifacts/traceability.json`

This was done because the canonical template is no longer a one-screen stub. It is now a tri-role baseline, so generated artifacts must provide:

- role-aware static runtime metadata;
- role-aware miniapp experience descriptors;
- the IR and traceability artifacts for audit and future compilation steps.

### 12. Root environment configuration was added

A root `.env` file now exists:

- [.env](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/.env)

and docker compose was updated to consume environment-driven values:

- [docker/docker-compose.yml](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/docker/docker-compose.yml)

Backend settings now also read:

- `PREVIEW_BASE_URL`
- `PLATFORM_DATA_DIR`

from environment in:

- [platform/backend/app/core/config.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/core/config.py)

This was done because a project-level README without a real env contract is incomplete. The root `.env` now acts as the operational baseline for:

- platform services;
- OpenRouter configuration;
- local ports;
- preview runtime mode and preview port allocation;
- data directory;
- database and redis settings;
- base mini-app defaults.

## Repository Structure

```text
grounded-miniapp-platform/
  contracts/
    grounded-spec.v1.json
    app-ir.v1.json

  platform/
    backend/
      app/
        ai/
        api/
        core/
        models/
        repositories/
        services/
        validators/
        workers/
      tests/

    frontend/
      src/
        App.tsx
        lib/
        styles/

  runtime/
    templates/
      base-miniapp/
        backend/
        frontend/
        docs/
        artifacts/
        docker/
    platform-docs/
      telegram/
      max/

  docker/
    docker-compose.yml

  data/
  .env
  README.md
```

## Core Backend API

### Workspace management

- `POST /workspaces`
- `GET /workspaces/{workspace_id}`
- `POST /workspaces/{workspace_id}/clone-template`
- `POST /workspaces/{workspace_id}/reset`

### Documents

- `POST /workspaces/{workspace_id}/documents`
- `GET /workspaces/{workspace_id}/documents`
- `POST /documents/{document_id}/index`
- `GET /documents/{document_id}/chunks`

### Chat and prompt turns

- `POST /workspaces/{workspace_id}/chat/turns`
- `GET /workspaces/{workspace_id}/chat/turns`
- `GET /turns/{turn_id}/summary`

### Generation

- `POST /workspaces/{workspace_id}/generate`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/events`
- `POST /jobs/{job_id}/retry`

### Spec, IR, validation, assumptions, traceability

- `GET /workspaces/{workspace_id}/spec/current`
- `GET /workspaces/{workspace_id}/ir/current`
- `GET /workspaces/{workspace_id}/validation/current`
- `GET /workspaces/{workspace_id}/assumptions/current`
- `GET /workspaces/{workspace_id}/traceability/current`
- `POST /workspaces/{workspace_id}/validation/run`

### Files and revisions

- `GET /workspaces/{workspace_id}/files/tree`
- `GET /workspaces/{workspace_id}/files/content`
- `POST /workspaces/{workspace_id}/files/save`
- `GET /workspaces/{workspace_id}/diff`

### Preview

- `POST /workspaces/{workspace_id}/preview/start`
- `POST /workspaces/{workspace_id}/preview/rebuild`
- `POST /workspaces/{workspace_id}/preview/reset`
- `GET /workspaces/{workspace_id}/preview/url`
- `GET /workspaces/{workspace_id}/preview/logs`
- `GET /preview/{workspace_id}` for the preview shell
- per-role preview URLs returned by `GET /workspaces/{workspace_id}/preview/url`

### Export

- `POST /workspaces/{workspace_id}/export/zip`
- `POST /workspaces/{workspace_id}/export/git-patch`
- `GET /exports/{export_id}/download`

## Canonical Base Mini-App Template

The current canonical template is not a blank starter. It is a structured three-role mini-app scaffold intended to be a stable compilation target.

### Roles

- `client`
- `specialist`
- `manager`

### Current template capabilities

- role-specific static pages served by one miniapp runtime
- role-specific profile persistence endpoint
- SQLite-backed profile persistence
- per-role preview URLs and shared preview bridge behavior

### Why one canonical template was kept

Only one internal template is supported at this stage because the research risk is not “how many templates can be imported”, but “can the grounded pipeline remain stable and traceable end-to-end”.

Supporting arbitrary templates too early would make these things harder:

- documentation normalization;
- artifact compiler stability;
- validator specificity;
- preview reproducibility;
- evaluation comparability.

## Root Environment File

The project now includes a root environment template and a local environment file:

- [.env.example](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/.env.example)
- [.env](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/.env)

### What is inside

- local platform ports;
- docker image names;
- PostgreSQL and Redis defaults;
- backend preview base URL;
- backend data directory;
- OpenRouter base URL and API key slot;
- base mini-app runtime defaults.

### Important note

`OPENROUTER_API_KEY` is intentionally left blank. That is the correct state for a committed research scaffold. Secrets should be filled locally and not hardcoded into versioned files.

### Recommended setup

Start from the committed example:

```bash
cd /Users/rafailvv/PycharmProjects/grounded-miniapp-platform
cp .env.example .env
```

Then fill only the values that are environment-specific, primarily:

- `OPENROUTER_API_KEY`
- `PLATFORM_DATA_DIR` if you want a different local storage path
- ports if `8000`, `5173`, `5432`, `6379`, or the preview range near `16000` are occupied

## How To Run

### 1. Backend only

```bash
cd /Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m pytest
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m uvicorn app.main:app --reload
```

### 2. Platform frontend

```bash
cd /Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/frontend
npm install
npm run dev
```

### 3. Docker compose

From the repository root:

```bash
docker compose --env-file .env -f docker/docker-compose.yml up --build
```

This starts:

- platform backend
- platform frontend
- PostgreSQL
- Redis

### 4. Base mini-app template runtime

The template is now a single miniapp runtime:

```bash
cd /Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/docker
docker compose up -d
```

## What Has Been Verified

Verified in this environment:

- backend contract and validator tests;
- end-to-end API smoke flow for workspace creation, template clone, document indexing, generation, preview URL creation, diff, and export;
- Python compilation of the template backend scaffold.

Verified test file set:

- [platform/backend/tests/test_contract_validators.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/tests/test_contract_validators.py)
- [platform/backend/tests/test_api_smoke.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/tests/test_api_smoke.py)

### Current verified result

Backend tests currently pass:

- `5 passed`

## What Is Still Intentionally Incomplete

This repository is already structurally aligned with the target plan, but it is still a research MVP scaffold rather than final production software.

The main gaps are:

- retrieval is lexical and deterministic for now, not full hybrid embeddings + reranking;
- PostgreSQL and Redis are included in topology, but the current platform persistence layer is file-backed, not yet DB-backed;
- repair loops are architecturally represented but not yet full autonomous multi-iteration code repair against real compiler/test diagnostics;
- platform frontend build was runtime-verified, while template verification now focuses on the single miniapp runtime rather than a separate template frontend.

These choices were intentional to keep the current state coherent:

- architecture is already correct;
- pipeline boundaries already exist;
- template/preview/revision model already exists;
- the next iterations can replace internal implementations without redesigning the whole system.

## Main Files to Read First

If you want to understand the project quickly, start here:

- [platform/backend/app/main.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/main.py)
- [platform/backend/app/services/generation_service.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/generation_service.py)
- [platform/backend/app/services/workspace_service.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/workspace_service.py)
- [platform/backend/app/services/preview_service.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/preview_service.py)
- [platform/frontend/src/App.tsx](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/frontend/src/App.tsx)
- [runtime/templates/base-miniapp/docs/README.md](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/docs/README.md)
- [runtime/templates/base-miniapp/miniapp/app/main.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/main.py)

## Summary

The project now matches the intended direction substantially better than a generic AI code generator scaffold.

It is a grounded workspace platform with:

- typed contracts;
- deterministic validator gates;
- a controlled compilation path;
- traceability artifacts;
- revision-safe workspace editing;
- a role-aware canonical mini-app template;
- and a triple-role simultaneous preview surface.

That combination is exactly why the architecture was implemented this way: it supports both the product behavior you described and the methodological requirements of a research-grade thesis implementation.
