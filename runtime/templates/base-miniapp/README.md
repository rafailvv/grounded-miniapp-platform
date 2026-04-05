# Canonical Base Mini-App Template

Minimal Telegram-first starter used as the canonical workspace baseline.

## Template shape

- `miniapp/`: FastAPI service that serves the UI, static assets, health endpoint, and persistent API endpoints.
- `docs/`: lightweight template notes and environment examples.
- `docker/`: single-service preview compose for the backend app.
- `miniapp/app`: simplified to `main.py`, `db.py`, `schemas.py`, `routes/`, and `static/`.
- `miniapp/app/static/<role>/<page>/`: each starter page keeps its own `index.html`, `styles.css`, and `app.js`.

## Baseline contract

- Three roles are always available: `client`, `specialist`, `manager`.
- Persistent models and sessions live in `db.py`, and routers should extend that storage instead of inventing route-local data stores.
- The UI is plain HTML, CSS, and JS served by FastAPI, with page-local assets stored in separate folders.
- Only home and profile flows are included by default.
- No authentication or domain-specific business logic is included.
