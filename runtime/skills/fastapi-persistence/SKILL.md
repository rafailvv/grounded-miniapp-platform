---
description: Align FastAPI routes, schemas, SQLite persistence, frontend payloads, and generated tests.
whenToUse:
  - backend state
  - persistence
  - api workflow smoke
  - prompt-derived persisted workflow
paths:
  - miniapp/app/routes/**
  - miniapp/app/db.py
  - miniapp/app/schemas.py
  - miniapp/app/static/**/app.js
  - miniapp/tests/**
allowedTools:
  - read_files
  - search_files
  - apply_patch_to_draft
  - write_file
  - run_checks
model: default
effort: high
validation:
  - api_workflow_smoke
  - generated_app_python_tests
  - generated_app_js_tests
---
# FastAPI Persistence

Use this skill when the generated app needs backend state.

## Rules

- Persist only the records and state transitions implied by the prompt through FastAPI routes and SQLite/SQLAlchemy helpers.
- Align route payloads, Pydantic schemas, database fields, frontend fetch payloads, and generated tests.
- PATCH/update routes must preserve omitted nested fields unless explicitly replaced.
- Avoid SQLAlchemy mapped attributes named `metadata`, `registry`, `query`, or `type`.
- Do not seed product records.

## Acceptance

- Prompt-required read endpoint or role state view starts empty.
- Prompt-required write action persists user payload.
- Follow-up reads return stored user data through the app-owned API.
- Prompt-required follow-up changes preserve existing data unless the prompt explicitly replaces it.
