# Generated Mini-App Agent Guide

The user's prompt is the only product source. This workspace is the generated app shell for a Telegram mini-app product.

## Product Surface

- Keep `/client`, `/specialist`, and `/manager` useful and role-specific.
- All roles must share the same persisted product state when the workflow needs shared state.
- Use product language from the prompt for routes, labels, fields, statuses, and empty states.
- Do not show internal terms such as raw API paths, role slugs, generated app, or platform route names in user-facing UI.

## Implementation

- Put backend workflows in `miniapp/app/routes/`, shared models and persistence in `miniapp/app/db.py`, and schemas in `miniapp/app/schemas.py`.
- New route modules in `miniapp/app/routes/` are auto-included when they export `router`; edit `miniapp/app/main.py` only for app setup changes.
- Put role UI under `miniapp/app/static/<role>/`.
- Use shared `/static/shared/app_helpers.js` and `/static/shared/base.css` primitives for API calls, escaping, forms, lists, cards, status text, and mobile spacing.
- Every child page must have its own `index.html` and be reachable from the role surface.
- Keep `preview_bridge.js`, shared base CSS, `/health`, and the page shell intact.
- Do not directly edit generated route/contract metadata unless a platform-owned compiler produced the update.

## Tests And Proof

- Keep `miniapp/tests/test_generated_app.py` and `miniapp/tests/generated_app.test.mjs` aligned with the app.
- Python tests should prove route/API/persistence behavior with `unittest` and `FastAPI TestClient`.
- JS tests should prove actual HTML/JS selectors, route pages, API calls, and event handlers by reading source text unless DOM mocks are explicit.
- Browser proof should cover the role workflow and mobile layout without horizontal overflow or blocking overlap.
