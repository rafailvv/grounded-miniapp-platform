---
description: Booking and reservation product workflow pack.
whenToUse:
  - запись
  - записи
  - записаться
  - записывается
  - бронирование
  - бронь
  - reservation
  - booking
  - appointment
  - slots
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
  - booking_workflow
  - persisted_workflow
  - role_coverage
  - browser_flow_smoke
---
# Booking / Reservations

Use this skill when the product creates appointments, reservations, slot booking, or waitlists.

## Rules

- Model bookable service, provider, date, slot, customer contact, status, price, and notes as real persisted records.
- Expose a client flow: choose service, choose provider or location, pick slot, enter contact, confirm, then see the saved booking.
- Expose a specialist flow: see today's bookings, update status, add notes, and block unavailable slots.
- Expose a manager flow: see schedule health, utilization, cancellations, and unresolved booking conflicts.
- Prevent double-booking in UI copy and state transitions; show sold-out or unavailable slots instead of letting users confirm them.
- Include reminders or next-step copy only when it is grounded in saved booking data.

## Acceptance

- API proof creates a booking and returns the same booking by id or list.
- Persistence proof shows the booking marker survives a reload or second read.
- Browser proof completes client booking and verifies the confirmed booking appears in specialist or manager view.
- Role coverage includes client, specialist, and manager for booking status visibility.
- Mobile proof shows slot picker, form, and confirmation without overflow.
