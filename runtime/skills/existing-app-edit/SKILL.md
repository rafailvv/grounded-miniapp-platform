---
metadata_schema: grounded.skill.v2
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
- Use workspace memory and Magic Docs before inventing new architecture.
- Prefer focused patches over rewrites.
- Keep existing role workflows working while adding the requested behavior.

## Acceptance

- Existing tests still pass or are updated for intentional behavior changes.
- New behavior has API/frontend/test coverage.
- Final report explains changed files and remaining risks.
