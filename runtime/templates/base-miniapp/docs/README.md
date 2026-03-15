# Base Mini-App Template Documentation

## Purpose

This canonical template exists to receive controlled compilation outputs from the grounded generation pipeline.

The current baseline is a tri-role mini-app:

- `client`
- `specialist`
- `manager`

## Backend contract

- `POST /api/auth/telegram` resolves the active role and returns auth tokens.
- `GET /api/roles` returns the supported role catalog.
- `GET /api/dashboard/{role}` returns role-specific dashboard data.
- `GET /api/profiles/{role}` loads the persisted role profile.
- `PUT /api/profiles/{role}` saves the role profile.
- `POST /api/submissions` accepts the generated form payload.
- `GET /health` reports backend readiness.

## Frontend contract

- The frontend bootstraps the role via Telegram auth or `?role=`.
- The frontend consumes runtime manifests from `/api/runtime/{role}/manifest` and uses an inline placeholder when runtime data is absent.
- Profile pages sync with backend if `VITE_API_BASE_URL` is set.
- Role home pages query backend dashboard data if the API is reachable.

## Workspace rules

- Prefer patching generated artifacts over rewriting template source files.
- Preserve manual edits as separate git revisions.
- Keep all three roles available in preview simultaneously.
