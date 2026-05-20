---
description: Telegram mobile UX patterns for generated mini-apps.
whenToUse:
  - telegram mobile ux
  - telegram ux
  - telegram
  - телеграм
  - миниапп
  - miniapp
  - mobile ux
  - tg webapp
paths:
  - miniapp/app/static/**
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
  - telegram_mobile_ux
  - mobile_layout
  - browser_flow_smoke
---
# Telegram Mobile UX

Use this skill when a Telegram mini-app needs production mobile ergonomics, not just a page that loads.

## Rules

- Design first for 360-430px width with safe top/bottom spacing, no horizontal scroll, and stable tap targets.
- Keep primary action close to the current task; avoid giant hero sections, decorative backgrounds, and nested cards.
- Use bottom or top role navigation only when it makes repeated switching easier.
- Use compact lists, segmented filters, status chips, and sticky action bars for operational workflows.
- Keep copy short enough for Russian labels and Telegram viewport constraints.
- Preserve preview bridge and Telegram-safe route behavior across all role pages.

## Acceptance

- Browser proof includes mobile viewport screenshots for the main role flow.
- Mobile layout proof has no overflow, overlap, clipped buttons, or unreadable controls.
- Role navigation is reachable with one or two taps from each role root.
- Forms and lists remain usable with realistic Russian text lengths.
