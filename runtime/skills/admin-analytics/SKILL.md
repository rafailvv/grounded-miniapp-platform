---
description: Admin analytics, operational metrics, and reporting workflow pack.
whenToUse:
  - админка
  - аналитика
  - метрики
  - отчеты
  - отчёты
  - dashboard
  - analytics
  - admin
  - metrics
  - reporting
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
  - analytics_workflow
  - persisted_workflow
  - role_coverage
  - browser_flow_smoke
---
# Admin / Analytics

Use this skill when the product needs admin operations, analytics, reporting, metrics, exports, quality control, or owner dashboards.

## Rules

- Derive metrics from saved product records: counts, status buckets, conversion, revenue, workload, delays, and risks.
- Expose filters for time range, status, role/assignee, category, and unresolved problems when they are relevant.
- Show actionable management blocks: today, blocked, overdue, top performers/items, trend, and next decisions.
- Keep analytics dense and readable; avoid marketing hero layouts or decorative cards.
- Include drill-down links or lists so a manager can move from metric to affected records.
- Use empty analytics states when there is no data and explain the first action to create signal.

## Acceptance

- API or state proof shows analytics are computed from persisted records.
- Browser proof creates or reads sample records, then verifies metrics update in manager/admin view.
- Role coverage proves manager/admin sees metrics and non-manager roles do not get privileged actions.
- Mobile proof validates metric cards, tables/lists, and filters without overflow.
- Tests cover at least one metric derived from saved data.
