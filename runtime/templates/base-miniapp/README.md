# Canonical Base Mini-App Template

Minimal Telegram-first starter used as the canonical workspace baseline.

## Template shape

- `backend/`: FastAPI service that serves the UI, static assets, health endpoint, and profile persistence endpoints.
- `docs/`: lightweight template notes and environment examples.
- `docker/`: single-service preview compose for the backend app.
- `backend/app`: simplified to `main.py`, `db.py`, `schemas.py`, `routes/`, and `static/`.

## Baseline contract

- Three roles are always available: `client`, `specialist`, `manager`.
- Backend persists profiles in SQLite.
- The UI is plain HTML, CSS, and JS served by FastAPI.
- Only home and profile flows are included by default.
- No authentication or domain-specific business logic is included.
