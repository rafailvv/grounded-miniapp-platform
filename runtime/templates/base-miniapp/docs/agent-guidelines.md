# Agent Guidelines

The user's prompt is the only source of product meaning. Template files provide only startup, routing, preview, persistence, and styling primitives.

## Generate

- Replace starter screens with the requested app surface.
- Name models, routes, pages, and UI with the user's domain language.
- Add backend persistence only when shared or durable state is needed.
- Keep browser-only tools browser-only when server data is not needed.

## Layout

- `miniapp/app/main.py`: app creation, static mounting, route registration, exception handlers.
- `miniapp/app/db.py`: SQLAlchemy engine, `Base`, `SessionLocal`, generated persistent models.
- `miniapp/app/schemas.py`: Pydantic models used by generated API routes.
- `miniapp/app/routes/<feature>.py`: one coherent backend feature.
- `miniapp/app/static/<role>/<feature>/`: one generated page triplet.

## Invariants

- Preserve `/health`, `/static/shared/base.css`, `/static/preview_bridge.js`, and `<main class="page-shell">`.
- Keep page-local `index.html`, `styles.css`, and `app.js` files in sync.
- Keep generated routers, schemas, and database models importable.
- Run checks after meaningful edits and fix reported failures in app code.
