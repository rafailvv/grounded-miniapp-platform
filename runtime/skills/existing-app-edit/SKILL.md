---
metadata_schema: grounded.skill.v2
description: Preserve and extend an already generated mini-app without broad rewrites.
whenToUse:
  - existing app edit
  - edit_mode=improve
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
triggerRules:
  - Match prompts using this skill domain and path scope.
validation:
  - changed_files_static
  - frontend_interaction_static_smoke
requiredProof:
  - Final readiness proof covers this skill.
incompatibleSkills:
  - ""
outputExpectations:
  - Produce working product changes and cite proof artifacts.
---
# Existing App Edit

Use this skill when modifying an already generated app.

## Rules

- Preserve existing selectors, ids, routes, and tests unless the requested behavior changes them.
- In improve mode, inspect the existing architecture/product map before patching and use the improve slice plan as the authoritative file scope.
- Use workspace memory and Magic Docs before inventing new architecture.
- Prefer focused patches over rewrites.
- Do not perform broad rewrites in improve mode unless the slice plan explicitly marks them required.
- Keep existing role workflows working while adding the requested behavior.

## Acceptance

- Existing tests still pass or are updated for intentional behavior changes.
- New behavior has API/frontend/test coverage.
- Final report explains changed files and remaining risks.
