---
metadata_schema: grounded.skill.v2
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
triggerRules:
  - Match prompts using this skill domain and path scope.
validation:
  - browser_flow_smoke
  - api_workflow_smoke
  - final_gate
requiredProof:
  - Browser product proof artifact covers required roles and acceptance-contract scenarios.
  - Console/network capture is present even when empty.
  - Persisted markers are verified after reload.
  - Mobile overflow/overlap diagnostics pass.
incompatibleSkills:
  - ""
outputExpectations:
  - Produce working product changes and cite proof artifacts.
---
# Browser Acceptance Proof

Use this skill when a run needs end-to-end product proof.

## Rules

- Cover client, specialist, and manager surfaces when a create run touches all roles.
- Capture at least one screenshot per required role surface.
- Prove that prompt-required state changes appear where the relevant roles consume them.
- Capture console/network errors explicitly; empty arrays are valid, missing fields are incomplete proof.
- Reload after a mutating workflow and record the same persisted marker after reload.
- Run scenarios derived from the acceptance contract; do not replace them with a generic smoke.
- Treat missing browser diagnostics as incomplete proof.

## Acceptance

- Browser proof has steps.
- Required roles are checked.
- Required roles have screenshot evidence.
- Acceptance-contract flows are represented as passed browser scenarios.
- Console and network error capture fields are present and contain no errors.
- Prompt-required persisted markers are recorded when the workflow mutates state.
- Persisted markers survive reload.
- Mobile layout status is not failed and reports no horizontal overflow or critical overlap.
