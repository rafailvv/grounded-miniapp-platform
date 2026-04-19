# Base Mini-App Template Documentation

## Purpose

This canonical template is the smallest supported Telegram-first mini-app baseline for generation.

The current baseline is a tri-role app:

- `client`
- `specialist`
- `manager`

## Miniapp contract

- `GET /api/profiles/{role}` loads the persisted role profile.
- `PUT /api/profiles/{role}` saves the role profile.
- `GET /health` reports backend readiness.
- `miniapp/app/routes/client.py`, `miniapp/app/routes/specialist.py`, and `miniapp/app/routes/manager.py` serve the role page surfaces.
- `GET /client/profile`, `GET /specialist/profile`, and `GET /manager/profile` serve the role profile pages.

## UI contract

- Each role has its own path-based pages instead of query-param bootstrapping.
- Home pages are simple role entry screens.
- Profile pages load and save through the backend profile API.
- Static files live in `miniapp/app/static`.
- Shared preview route sync lives in `miniapp/app/static/preview_bridge.js`.

## Canonical roots

- The miniapp runtime uses `miniapp/app/main.py`, `miniapp/app/db.py`, `miniapp/app/schemas.py`, `miniapp/app/routes/*`, and `miniapp/app/static/*`.
- Extend `db.py` when new persistent entities are introduced, and keep routers thin consumers of that storage.

## Workspace rules

- Extend this template by editing real source files instead of layering parallel runtime systems.
- Preserve manual edits as separate git revisions.
- Keep all three roles available in preview simultaneously.

## Generation references

- `docs/generation-contract.md`: product, shell, workflow, and backend invariants.
- `docs/ownership-contract.md`: file ownership and module responsibilities.
- `docs/generic-persisted-workflow.md`: canonical CRUD and cross-role lifecycle pattern.
- `docs/anti-patterns.md`: generation mistakes that should be avoided.
