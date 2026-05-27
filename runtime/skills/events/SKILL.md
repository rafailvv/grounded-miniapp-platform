---
metadata_schema: grounded.skill.v2
description: Events, registration, tickets, attendance, and schedule workflow pack.
whenToUse:
  - мероприятие
  - мероприятия
  - событие
  - события
  - регистрация
  - билет
  - билеты
  - event
  - events
  - ticket
  - registration
  - attendance
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
  - event_workflow
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
# Events

Use this skill when the product manages events, schedules, tickets, attendance, sessions, or registrations.

## Rules

- Persist event title, date, place or online link, capacity, ticket type, attendee, registration status, and check-in state.
- Expose a client flow: browse events, register or buy ticket, receive confirmation, and see ticket or registration status.
- Expose a specialist flow: check in attendees, update attendance, and see event-day tasks.
- Expose a manager flow: registrations, capacity, waitlist, attendance rate, revenue or conversion, and event issues.
- Show capacity and waitlist honestly; do not allow registration beyond configured capacity without a waitlist state.
- Include schedule detail and after-registration next steps in saved data.

## Acceptance

- API proof creates a registration and reads capacity or attendee state.
- Persistence proof confirms attendee marker survives reload.
- Browser proof registers for an event and verifies attendee appears in specialist or manager view.
- Role coverage includes client ticket, specialist check-in, and manager capacity views.
- Tests cover registration and capacity or status update.
