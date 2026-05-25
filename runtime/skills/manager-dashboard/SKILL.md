---
description: Manager dashboard, operations queue, and decision surface pack.
whenToUse:
  - manager dashboard
  - manager
  - менеджер
  - руководитель
  - управляющий
  - owner dashboard
  - operations dashboard
  - операционная панель
  - дашборд менеджера
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
validation:
  - manager_dashboard
  - persisted_workflow
  - role_coverage
  - browser_flow_smoke
---
# Manager Dashboard

Use this skill when the manager or owner needs an actual operations surface instead of a renamed client page.

## Rules

- Show manager-specific jobs: triage queue, overdue or blocked records, team load, revenue/status metrics, and recent activity.
- Derive every metric and queue from persisted product records.
- Provide drill-down actions: open record, assign owner, change status, resolve issue, or export/report when relevant.
- Separate manager permissions from client/specialist actions; do not expose privileged controls to other roles.
- Keep the dashboard compact, scan-friendly, and optimized for repeated daily use.
- Use alerts sparingly for records that require manager action.

## Acceptance

- Browser proof verifies a record created by client/specialist appears on manager dashboard.
- API or state proof shows at least one manager metric or queue count changes from persisted data.
- Role coverage proves manager surface is distinct from client and specialist surfaces.
- Mobile proof shows dashboard metrics and queue controls without overlap.
