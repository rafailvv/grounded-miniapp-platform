# Base Mini-App Template Documentation

## Purpose

This canonical template exists to receive controlled compilation outputs from the grounded generation pipeline.

The current baseline is a tri-role mini-app:

- `client`
- `specialist`
- `manager`

## Backend contract

- `GET /api/profiles/{role}` loads the persisted role profile.
- `PUT /api/profiles/{role}` saves the role profile.
- `GET /health` reports backend readiness.

## Frontend contract

- The frontend bootstraps the role via `?role=` or the Telegram start parameter.
- Profile pages sync with backend if `VITE_API_BASE_URL` is set.
- Role home pages are static starter screens that can be extended by generation.

## Workspace rules

- Prefer patching generated artifacts over rewriting template source files.
- Preserve manual edits as separate git revisions.
- Keep all three roles available in preview simultaneously.
