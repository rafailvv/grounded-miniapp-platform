# Ownership Contract

Use this template as a contract-guided codebase. Keep file ownership stable.

## Backend ownership

- `miniapp/app/main.py` owns FastAPI bootstrap, middleware, router inclusion, and startup wiring only.
- `miniapp/app/db.py` owns SQLAlchemy models, engine/session setup, and persisted entity storage.
- `miniapp/app/schemas.py` owns shared API input/response models, enums, and shared API literals.
- `miniapp/app/routes/profiles.py` owns role profile reads and writes.
- `miniapp/app/routes/profiles.py` is the simplest DB-backed route example: load one record, upsert one record, and return a response schema with server-owned fields.
- `miniapp/app/routes/<feature>.py` owns API logic for one prompt-derived feature or entity.
- `miniapp/app/routes/client.py`, `miniapp/app/routes/specialist.py`, and `miniapp/app/routes/manager.py` own page-serving routes only.
- `miniapp/app/routes/role_pages.py` owns shared role-page resolution helpers only.
- `miniapp/app/routes/role_pages.py` is helper-only and does not export a FastAPI `router`; never import `router` from it in `main.py`.
- `miniapp/app/generated/*` owns derived manifests and generated runtime metadata only.

## Frontend ownership

- `miniapp/app/static/shared/base.css` owns the shared shell baseline and common spacing.
- `miniapp/app/static/preview_bridge.js` owns preview-aware role wiring and `window.miniappApiFetch(...)`.
- `miniapp/app/static/<role>/index.html` owns the role root dashboard surface.
- `miniapp/app/static/<role>/profile/*` owns the role profile page surface only.
- `miniapp/app/static/<role>/<feature>/*` owns one feature page triplet: `index.html`, `styles.css`, `app.js`.

## Rules

- Do not define ORM models inside route modules.
- Do not define inline Pydantic input/response models inside route modules when the type belongs in `schemas.py`.
- Use separate input and response schemas when the server owns timestamps, ids, or derived fields.
- Do not move business logic into `main.py`.
- Do not split one prompt-derived feature or entity across several near-duplicate route modules.
- Keep enum-like values owned by `schemas.py` and reused everywhere else when the domain needs enums.
