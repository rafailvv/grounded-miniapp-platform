# Generic Persisted Workflow

When the prompt implies a real internal workflow app, use one shared persisted lifecycle instead of disconnected role pages.

## Canonical lifecycle

- `client` creates a record.
- `specialist` reads and updates the same record.
- `manager` observes the same record set or an aggregate derived from it.

## Canonical backend pattern

- Add the persisted entity model to `miniapp/app/db.py`.
- Add request/response models and enum/status literals to `miniapp/app/schemas.py`.
- Add one feature route module under `miniapp/app/routes/<feature>.py`.
- Keep the feature route module focused on:
  - `POST /api/<feature>`
  - `GET /api/<feature>`
  - `PUT` or `PATCH /api/<feature>/{id}`

## Canonical frontend pattern

- Root or feature pages may define an alias:

```js
const apiFetch = window.miniappApiFetch || fetch;
```

- Real write surfaces must visibly use the API:

```js
await apiFetch("/api/<feature>", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
}, role);
```

- Real read surfaces must visibly load from `/api/...`.
- Do not render create/update forms unless the same draft includes the corresponding write path.
- Do not render workflow lists from hardcoded arrays when the page claims to show live records.

## Consistency rules

- Keep one canonical route name for the dominant entity.
- Keep one canonical set of status literals across `db.py`, `schemas.py`, route handlers, and frontend labels.
- If the prompt uses synonyms like booking, request, reservation, or appointment, choose one internal vocabulary and keep it consistent.
