---
description: Repair a failed generation from its failure signature, repair packet, and proof requirements.
whenToUse:
  - failed generation
  - repair packet
  - check failed
  - preview boot failure
  - browser proof failed
paths:
  - miniapp/app/**
  - miniapp/tests/**
allowedTools:
  - read_files
  - search_files
  - apply_patch_to_draft
  - write_file
  - run_checks
  - browser_verify
model: default
effort: high
validation:
  - failing_check
  - changed_files_static
  - final_gate
---
# Repair Failed Generation

Use this skill when checks, preview, API smoke, browser proof, or generated tests fail.

## Rules

- Start from the failing check, failure signature, and repair packet.
- Read the smallest relevant file set before editing.
- Fix the concrete mismatch across backend, frontend, and tests.
- Rerun the failing check before widening scope.
- Do not restart the product design unless the failure proves the contract is wrong.

## Acceptance

- The original failing signature is gone.
- The repair is verified by the named check.
- No unrelated generated metadata was edited by hand.
