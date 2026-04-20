# Detail Page Pattern

When a role needs to inspect one persisted record in depth, use a dedicated route-backed detail page.

- A list page links to a separate detail page. Do not force inline expansion or a modal unless the prompt explicitly asks for it.
- The detail page reads one real record by id from the backend.
- Detail actions must persist through the real backend and shared DB state.
- After a detail-page action, the originating list and the other role views must reflect the updated persisted state.
- Keep detail pages inside the same shell contract and preview bridge behavior as the rest of the app.

Canonical flow:

1. list page renders links to `/<role>/<entity-stem>/{id}`
2. detail page loads one record from `/api/<entity-stem>/{id}`
3. action buttons write to `/api/<entity-stem>/{id}`
4. returning to the list shows the updated shared state

