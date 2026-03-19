# Canonical Base Mini-App Template

Minimal Telegram-first starter used as the canonical workspace baseline.

## Template shape

- `backend/`: FastAPI service with health and profile persistence endpoints.
- `frontend/`: React app with role bootstrap, role home pages, and role profile pages.
- `docs/`: lightweight template notes and environment examples.
- `docker/`: preview compose topology for backend, frontend, database, and proxy.

## Baseline contract

- Three roles are always available: `client`, `specialist`, `manager`.
- Backend persists profiles in the database.
- Frontend supports only home and profile flows by default.
- No authentication, runtime manifest system, or domain-specific business logic is included.
