---
metadata_schema: grounded.skill.v2
description: Generate or refine production-quality Telegram mini-app role surfaces.
whenToUse:
  - telegram mini app
  - create run
  - balanced generation
  - quality generation
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
effort: high
triggerRules:
  - Match prompts using this skill domain and path scope.
validation:
  - platform_invariants
  - frontend_interaction_static_smoke
  - browser_flow_smoke
requiredProof:
  - Final readiness proof covers this skill.
incompatibleSkills:
  - ""
outputExpectations:
  - Produce working product changes and cite proof artifacts.
---
# Telegram Mini-App Product

Use this skill when generating or refining a Telegram mini-app product.

## Rules

- Design for 360-430px mobile widths first.
- Keep each role surface focused on that role's next useful action.
- Preserve shared product vocabulary across client, specialist, and manager.
- Use light operational UI by default.
- Keep Telegram safe spacing and the preview bridge in every routeable page.

## Acceptance

- Role roots load.
- Prompt-derived workflow is reachable.
- There is no horizontal overflow on mobile.
- User-facing labels avoid raw implementation terms.
