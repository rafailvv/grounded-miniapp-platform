---
metadata_schema: grounded.skill.v2
description: Empty, error, and loading state quality pack.
whenToUse:
  - empty state
  - loading state
  - error state
  - empty
  - loading
  - error
  - пустое состояние
  - ошибка
  - загрузка
  - нет данных
paths:
  - miniapp/app/static/**
  - miniapp/app/routes/**
  - miniapp/tests/**
allowedTools:
  - read_files
  - search_files
  - apply_patch_to_draft
  - write_file
  - run_checks
  - browser_verify
model: default
effort: medium
triggerRules:
  - Match prompts using this skill domain and path scope.
validation:
  - empty_error_loading_states
  - browser_flow_smoke
requiredProof:
  - Final readiness proof covers this skill.
incompatibleSkills:
  - ""
outputExpectations:
  - Produce working product changes and cite proof artifacts.
---
# Empty / Error / Loading States

Use this skill when generated screens need robust non-happy-path states.

## Rules

- Add loading states for API reads and action submissions, with disabled duplicate-submit controls.
- Add empty states for first run, filtered-out lists, no search results, no assignments, and no analytics data.
- Add error states for failed API requests, validation failures, unavailable actions, and retryable network problems.
- Empty states must offer the next useful action, not just "nothing here".
- Error states must preserve user input where possible and expose retry or navigation recovery.
- Loading, empty, and error states must use the same product vocabulary as the happy path.

## Acceptance

- Browser proof exercises at least one loading or empty state when practical.
- Tests or diagnostics cover a failed or missing-data path when the workflow depends on remote data.
- UI never crashes or renders raw stack traces for failed data reads.
- Empty states on manager/specialist queues explain how records will appear.
