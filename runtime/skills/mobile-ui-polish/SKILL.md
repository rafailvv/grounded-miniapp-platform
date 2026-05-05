---
description: Polish generated role UI for Telegram-sized mobile screens and layout proof.
whenToUse:
  - mobile polish
  - visual style edit
  - browser overflow
  - layout failure
paths:
  - miniapp/app/static/**/styles.css
  - miniapp/app/static/**/index.html
  - miniapp/app/static/**/app.js
allowedTools:
  - read_files
  - search_files
  - apply_patch_to_draft
  - write_file
  - run_checks
  - browser_verify
model: default
effort: medium
validation:
  - browser_flow_smoke
  - mobile_layout
---
# Mobile UI Polish

Use this skill for quality-mode visual passes or UI fixes.

## Rules

- Treat cards, forms, buttons, lists, tabs, and route links as production controls.
- Keep text within containers at mobile and desktop widths.
- Use stable dimensions for toolbars, counters, grids, and repeated items.
- Avoid one-note color palettes and decorative backgrounds that reduce clarity.
- Make empty states useful without fake data.

## Acceptance

- Tap targets are large enough for mobile.
- No critical overlap.
- No horizontal overflow.
- Primary workflow is visible without reading implementation text.
