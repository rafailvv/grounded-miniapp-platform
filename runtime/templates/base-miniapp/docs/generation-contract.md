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
- A local alias like `const apiFetch = window.miniappApiFetch || fetch;` is valid, but write surfaces must still visibly target `/api/...` with a write method.

## Workflow rules

- Build one real shared persisted entity lifecycle.
- `client` creates records.
- `specialist` reads and updates the same records.
- `manager` observes the same shared state or an aggregate of it.
- Do not ship form UI, lists, or role dashboards without real read/write API paths in the same draft.
- Derive the dominant workflow entity, route names, and page names from the prompt and grounded spec. Do not hard-code domain nouns from previous apps.
- Prefer one canonical backend route module per dominant workflow entity instead of splitting the same lifecycle across multiple near-duplicate route files.
- If the prompt implies time-bound reservations, bookings, requests, loans, or appointments, keep the API and UI vocabulary internally consistent instead of mixing several synonyms in parallel.

## Backend rules

- Keep routers under `miniapp/app/routes` on FastAPI with top-level `router = APIRouter(...)`.
- Extend `db.py` and `schemas.py` when new persistent entities are introduced.
- Keep route wiring, runtime manifests, and generated tests derived from realized code.
- Keep schema enum/status values consistent across `db.py`, `schemas.py`, route handlers, and frontend UI labels. Do not invent alternate status literals in only one layer.

## Related docs

- See `docs/ownership-contract.md` for file and module ownership.
- See `docs/generic-persisted-workflow.md` for the canonical CRUD and role-lifecycle pattern.
- See `docs/anti-patterns.md` for generation mistakes that should be avoided.
