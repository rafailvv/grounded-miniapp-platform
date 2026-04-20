# Status Lifecycle Pattern

Statuses belong to the shared entity contract, not to one isolated layer.

- Declare canonical status values in `schemas.py`.
- Route handlers must reuse only those declared status values.
- Frontend UI labels and filters must map from the same status set.
- Do not invent alternate spellings or shadow status sets in only one route or one page.
- When a role action changes status, the resulting value must be visible consistently for `client`, `specialist`, and `manager`.

Good pattern:

- `schemas.py` declares the allowed statuses
- route modules validate and persist only those statuses
- the UI renders labels from the same set

Bad pattern:

- backend uses `approved`
- one page uses `confirmed`
- another page uses `done`
- manager page invents `pending_review`

