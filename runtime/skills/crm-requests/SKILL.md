---
metadata_schema: grounded.skill.v2
description: CRM leads, requests, and pipeline workflow pack.
whenToUse:
  - crm
  - срм
  - заявка
  - заявки
  - заявок
  - лид
  - лиды
  - pipeline
  - lead
  - request
  - requests
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
  - crm_workflow
  - persisted_workflow
  - role_coverage
  - browser_flow_smoke
requiredProof:
  - Final readiness proof covers this skill.
incompatibleSkills:
  - ""
outputExpectations:
  - Produce working product changes and cite proof artifacts.
---
# CRM / Requests

Use this skill when the product captures leads, support requests, sales applications, callbacks, or pipeline tasks.

## Rules

- Persist request title, contact, source, priority, status, assignee, due date, comments, and activity timestamps.
- Expose a client flow: submit request, see current status, add clarifying information, and view the next expected action.
- Expose a specialist flow: claim or assign a request, change status, add internal notes, and schedule follow-up.
- Expose a manager flow: pipeline by status, SLA risk, stale requests, assignee load, conversion or closure metrics.
- Use operational statuses, not vague labels: new, qualified, in progress, waiting for client, done, lost, canceled.
- Keep request lists scannable with status chips, priority, owner, and age.

## Acceptance

- API proof creates a request and updates its status or assignee.
- Persistence proof reads back the same request and latest activity.
- Browser proof submits a client request and verifies it appears in specialist or manager queue.
- Role coverage proves client status view and manager pipeline view are both connected to saved data.
- Tests cover create, list, and update behavior for requests.
