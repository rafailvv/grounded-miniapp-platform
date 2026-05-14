---
description: Prove product workflows end-to-end through browser, API, persisted markers, and role surfaces.
whenToUse:
  - browser acceptance
  - preview proof
  - final gate
  - role workflow proof
paths:
  - miniapp/app/static/**
  - miniapp/app/routes/**
  - miniapp/tests/**
allowedTools:
  - browser_verify
  - run_checks
  - read_files
  - read_artifact_ref
model: default
effort: medium
validation:
  - browser_flow_smoke
  - api_workflow_smoke
  - final_gate
---
# Browser Acceptance Proof

Use this skill when a run needs end-to-end product proof.

## Rules

- Cover client, specialist, and manager surfaces when a create run touches all roles.
- Prove that prompt-required state changes appear where the relevant roles consume them.
- Capture console/network errors and mobile layout diagnostics.
- Treat missing browser diagnostics as incomplete proof.

## Acceptance

- Browser proof has steps.
- Required roles are checked.
- Prompt-required persisted markers are recorded when the workflow mutates state.
- Mobile layout status is not failed.
