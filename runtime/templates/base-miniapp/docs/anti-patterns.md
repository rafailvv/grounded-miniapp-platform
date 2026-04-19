# Anti-Patterns

Avoid these generation mistakes.

## Backend anti-patterns

- ORM models declared inside route modules.
- Route-local Pydantic models that duplicate types already owned by `schemas.py`.
- Business CRUD logic implemented inside `main.py`.
- Several route modules representing the same dominant entity with different synonyms.
- Status literals that diverge between backend layers.

## Frontend anti-patterns

- Form UI without a real `/api/...` write path in the same draft.
- Live workflow lists rendered from hardcoded arrays.
- Seeded request cards, approval rows, conflict items, or other filled business records baked into HTML or JS before any API read happens.
- Role dashboards that are only renamed copies of each other.
- Loading-only shells as the primary first paint.
- Pages that bypass `preview_bridge.js` or drop the shared shell contract.

## Runtime anti-patterns

- Generated code that rewrites source to match a stale manifest instead of regenerating artifacts from code.
- New parallel architectures under `frontend/`, `app/domain/`, or other unsupported roots.
- Replacing real FastAPI routers with placeholder HTML handlers just to satisfy route checks.
