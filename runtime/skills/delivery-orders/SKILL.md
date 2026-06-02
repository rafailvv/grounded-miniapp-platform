---
metadata_schema: grounded.skill.v2
description: Delivery, orders, fulfillment, courier, and status tracking workflow pack.
whenToUse:
  - доставка
  - доставку
  - заказ
  - заказы
  - курьер
  - courier
  - delivery
  - order tracking
  - fulfillment
  - dispatch
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
  - delivery_workflow
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
# Delivery / Orders

Use this skill when the product manages orders, delivery, pickup, courier assignment, fulfillment, or status tracking.

## Rules

- Persist order id, customer, address or pickup point, items, payment state, delivery window, courier, status, and issue notes.
- Expose a client flow: create order, choose delivery or pickup, track status, and see ETA or next step.
- Expose a specialist/courier flow: accept assignment, update status, mark delivered, and report issue.
- Expose a manager flow: order queue, late deliveries, courier load, fulfillment bottlenecks, and failed orders.
- Use status transitions that fit fulfillment: new, confirmed, preparing, ready, assigned, on the way, delivered, issue, canceled.
- Keep ETA/status tied to persisted order data, not just static labels.

## Acceptance

- API proof creates an order and updates its fulfillment or delivery status.
- Persistence proof reads the same order with latest status and courier or pickup marker.
- Browser proof creates an order and verifies status appears in courier/specialist or manager queue.
- Role coverage includes client tracking and manager operations.
- Tests cover order creation and status update.
