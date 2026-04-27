# Agent Guidelines

The user's prompt is the only source of product meaning. Template files provide only startup, routing, preview, persistence, and styling primitives.

## Generate

- Replace starter screens with the requested app surface.
- Create connected `client`, `specialist`, and `manager` role surfaces for every new app.
- Keep the three roles tied to the same product content: shared app title, shared domain objects, and consistent labels/actions.
- Never leave a role as a generic preview, blank page, or neutral starter.
- Generate a multi-page app, not one long page. Each role root (`/client`, `/specialist`, `/manager`) is a hub, and each role needs at least two domain-specific child pages.
- Declare child pages in `miniapp/app/generated/route_manifest.json`. Use a compact `routes` map, either top-level or per-role, for example `"/client/catalog": "static/client/catalog/index.html"`.
- The older profile flow is only a routing pattern: use a profile/settings child page when useful, but keep its content specific to the user's product.
- Root pages should link to child pages and summarize the role workflow; detailed catalog/order/task/report/profile content belongs on child pages.
- Name models, routes, pages, and UI with the user's domain language.
- Generate light-mode UI by default: light backgrounds, dark readable text, and accessible contrast. Use a dark theme only when the user explicitly asks for it.
- Every generated create app must be usable, not static-only: include forms/buttons, frontend `fetch` calls, and backend APIs that save user-provided records.
- Do not include mock data, seed data, demo data, sample data, fixtures, preloaded records, or hard-coded business records in generated app source. Start with empty persistent state and domain-specific empty states.
- Fast create needs at least one persistent resource with `GET` and `POST`. Balanced should connect two or three resources or add status/update behavior. Quality should provide detailed multi-role workflows with several endpoints.
- Add generated app tests in `miniapp/tests/test_generated_app.py` and `miniapp/tests/generated_app.test.mjs`.
- Keep generated tests dependency-free beyond the template runtime: Python `unittest` + FastAPI `TestClient`, and Node `node:test` + `fs/path`.
- Generated JS tests run from the `miniapp/` directory; read `app/static/<role>/...`, not `miniapp/app/static/<role>/...`.
- Generated JS tests should assert only exact strings that literally appear in the file being read. Do not paraphrase expected UI text in `includes()` or regex assertions.
- In generated JS tests, pass string paths to `path`/`fs`: prefer `path.join(process.cwd(), "app/static/client/index.html")`, or wrap URL fixtures with `fileURLToPath(new URL(..., import.meta.url))`.
- Python `TestClient` tests see HTML before browser JavaScript runs; use them for route/static shell/API checks, including empty `GET`, `POST`, and post-create `GET` persistence. Put JS-rendered content/data assertions in `generated_app.test.mjs`.
- During edits, preserve existing selectors, ids, and data-testid attributes that generated tests assert, unless the requested behavior intentionally changes them and the test is updated in the same patch.

## Layout

- `miniapp/app/main.py`: app creation, static mounting, route registration, exception handlers.
- `miniapp/app/db.py`: SQLAlchemy engine, `Base`, `SessionLocal`, generated persistent models.
- `miniapp/app/schemas.py`: Pydantic models used by generated API routes.
- `miniapp/app/routes/<feature>.py`: one coherent backend feature.
- `miniapp/app/static/<role>/index.html`: role hub page.
- `miniapp/app/static/<role>/<feature>/index.html`: role child page declared in the route manifest.
- `miniapp/tests/`: product-specific generated tests for role roots, backend APIs when present, and shared role content.

## Invariants

- Preserve `/health`, `/static/shared/base.css`, `/static/preview_bridge.js`, and `<main class="page-shell">`.
- Keep `<script src="/static/preview_bridge.js" defer></script>` in every generated HTML route page, including child pages. Role root pages should place it before the role app script.
- Keep page-local `index.html`, `styles.css`, and `app.js` files in sync.
- Keep generated routers, schemas, and database models importable.
- Keep all `/client`, `/specialist`, and `/manager` role roots loadable.
- Keep at least three routeable pages per role: the role root plus at least two child pages.
- Keep generated Python and JS tests present and passing after create/edit/fix.
- Run checks after meaningful edits and fix reported failures in app code.
