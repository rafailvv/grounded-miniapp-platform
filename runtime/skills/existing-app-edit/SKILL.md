---
description: Preserve and extend an already generated mini-app without broad rewrites.
whenToUse:
  - existing app edit
  - refine generated product
  - add behavior without replacing architecture
paths:
  - miniapp/app/**
  - miniapp/tests/**
  - docs/**
allowedTools:
  - read_files
  - search_files
  - apply_patch_to_draft
  - write_file
  - run_checks
model: default
effort: medium
validation:
  - changed_files_static
  - frontend_interaction_static_smoke
---
# Existing App Edit

Use this skill when modifying an already generated app.

## Rules

- Preserve existing selectors, ids, routes, and tests unless the requested behavior changes them.
- Use workspace memory and Magic Docs before inventing new architecture.
- Prefer focused patches over rewrites.
- Keep existing role workflows working while adding the requested behavior.

## Acceptance

- Existing tests still pass or are updated for intentional behavior changes.
- New behavior has API/frontend/test coverage.
- Final report explains changed files and remaining risks.
