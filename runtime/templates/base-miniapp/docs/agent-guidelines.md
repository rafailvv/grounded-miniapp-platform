# Agent Guidelines

The user's prompt is the only source of product meaning. Template files provide only startup, routing, preview, persistence, and styling primitives.

## Generate

- Replace starter screens with the requested app surface.
- Create connected `client`, `specialist`, and `manager` role surfaces for every new app.
- Keep the three roles tied to the same product content: shared app title, shared domain objects, and consistent labels/actions.
- Never leave a role as a generic preview, blank page, or neutral starter.
- Name models, routes, pages, and UI with the user's domain language.
- Add backend persistence only when shared or durable state is needed.
- Keep browser-only tools browser-only when server data is not needed.
- Add generated app tests in `miniapp/tests/test_generated_app.py` and `miniapp/tests/generated_app.test.mjs`.
- Keep generated tests dependency-free beyond the template runtime: Python `unittest` + FastAPI `TestClient`, and Node `node:test` + `fs/path`.
- Generated JS tests run from the `miniapp/` directory; read `app/static/<role>/...`, not `miniapp/app/static/<role>/...`.
- In generated JS tests, pass string paths to `path`/`fs`: prefer `path.join(process.cwd(), "app/static/client/index.html")`, or wrap URL fixtures with `fileURLToPath(new URL(..., import.meta.url))`.
- Python `TestClient` tests see HTML before browser JavaScript runs; use them for route/static shell/API checks, and put JS-rendered content/data assertions in `generated_app.test.mjs`.

## Layout

- `miniapp/app/main.py`: app creation, static mounting, route registration, exception handlers.
- `miniapp/app/db.py`: SQLAlchemy engine, `Base`, `SessionLocal`, generated persistent models.
- `miniapp/app/schemas.py`: Pydantic models used by generated API routes.
- `miniapp/app/routes/<feature>.py`: one coherent backend feature.
- `miniapp/app/static/<role>/<feature>/`: one generated page triplet.
- `miniapp/tests/`: product-specific generated tests for role roots, backend APIs when present, and shared role content.

## Invariants

- Preserve `/health`, `/static/shared/base.css`, `/static/preview_bridge.js`, and `<main class="page-shell">`.
- Keep `<script src="/static/preview_bridge.js" defer></script>` in every role `index.html` before the role app script.
- Keep page-local `index.html`, `styles.css`, and `app.js` files in sync.
- Keep generated routers, schemas, and database models importable.
- Keep all `/client`, `/specialist`, and `/manager` role roots loadable.
- Keep generated Python and JS tests present and passing after create/edit/fix.
- Run checks after meaningful edits and fix reported failures in app code.
