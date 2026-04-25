# Generation Contract

Use this template as an extension target, not as something to replace.

## Product invariants

- The user prompt is the only source of product/domain semantics.
- Keep exactly three role entry points available by default: `client`, `specialist`, `manager`.
- Preserve the existing profile flow for all three roles unless the user explicitly asks to remove it.
- Preserve the FastAPI runtime layout under `miniapp/app`.
- Do not assume a request, status, approval, or workflow lifecycle unless the prompt asks for it.

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

## Product behavior rules

- Build the app type implied by the prompt: content site, commerce app, calculator, dashboard, booking flow, CRUD tool, or another domain.
- Add backend persistence only when the requested behavior needs shared or durable data.
- Do not pre-render invented live business records just to make a page look populated. Use real API data, static catalog/config data, or an honest empty state according to the app type.
- Static sample content is acceptable for content, catalog, marketing, portfolio, or tool surfaces when it is the product itself rather than fake live state.
- Derive route names, page names, schemas, and UI vocabulary from the prompt. Do not hard-code domain nouns from previous apps.
- If the prompt is an internet shop, use commerce vocabulary such as products, catalog, cart, orders, checkout, inventory, and customers; never generic applications or requests.

## Backend rules

- Keep routers under `miniapp/app/routes` on FastAPI with top-level `router = APIRouter(...)`.
- Extend `db.py` and `schemas.py` only when persistent entities are introduced.
- Keep route wiring and runtime surfaces derived from realized code.
- Keep enum values and frontend labels consistent when a domain actually uses enums.

## Related docs

- See `docs/ownership-contract.md` for file and module ownership.
- See `docs/anti-patterns.md` for generation mistakes that should be avoided.
