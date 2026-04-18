# Generation Contract

Use this template as an extension target, not as something to replace.

## Product invariants

- Keep exactly three roles: `client`, `specialist`, `manager`.
- Keep one shared DB-backed state model for workflow records.
- Preserve the existing profile flow for all three roles.
- Preserve the FastAPI runtime layout under `miniapp/app`.

## Frontend shell rules

- Every generated page must keep the shared shell stylesheet at `/static/shared/base.css`.
- Every generated page must render a `<main class="page-shell">` root.
- Every generated page must keep `/static/preview_bridge.js`.
- Keep the current top-spacing baseline from the shared shell contract. Do not invent per-page replacements.
- Prefer page-local triplets when a feature page exists: `index.html`, `styles.css`, `app.js` in the same folder.

## Preview and navigation rules

- Generated pages must stay compatible with preview back navigation.
- Do not remove or bypass `setupPreviewBridge()` behavior.
- For frontend API calls, prefer `window.miniappApiFetch(...)` from the preview bridge over raw `fetch(...)`.

## Workflow rules

- Build one real shared persisted entity lifecycle.
- `client` creates records.
- `specialist` reads and updates the same records.
- `manager` observes the same shared state or an aggregate of it.
- Do not ship form UI, lists, or role dashboards without real read/write API paths in the same draft.

## Backend rules

- Keep routers under `miniapp/app/routes` on FastAPI with top-level `router = APIRouter(...)`.
- Extend `db.py` and `schemas.py` when new persistent entities are introduced.
- Keep route wiring, runtime manifests, and generated tests derived from realized code.
