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
- `GET /client`, `GET /specialist`, and `GET /manager` serve the role home pages.
- `GET /client/profile`, `GET /specialist/profile`, and `GET /manager/profile` serve the role profile pages.

## UI contract

- Each role has its own path-based pages instead of query-param bootstrapping.
- Home pages are simple role entry screens.
- Profile pages load and save through the backend profile API.
- Static files live in `miniapp/app/static`.
- Shared preview route sync lives in `miniapp/app/static/preview-bridge.js`.

## Canonical roots

- The miniapp runtime uses `miniapp/app/main.py`, `miniapp/app/db.py`, `miniapp/app/schemas.py`, `miniapp/app/routes/*`, and `miniapp/app/static/*`.
- Do not reintroduce separate frontend applications, extra service layers, or parallel backend API trees.

## Workspace rules

- Extend this template by editing real source files instead of layering parallel runtime systems.
- Preserve manual edits as separate git revisions.
- Keep all three roles available in preview simultaneously.
