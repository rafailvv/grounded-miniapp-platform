# Base Mini-App Template Documentation

## Purpose

This canonical template is the smallest supported Telegram-first mini-app baseline for generation.

The current baseline is a tri-role app:

- `client`
- `specialist`
- `manager`

## Backend contract

- `GET /api/profiles/{role}` loads the persisted role profile.
- `PUT /api/profiles/{role}` saves the role profile.
- `GET /health` reports backend readiness.

## Frontend contract

- The frontend bootstraps the role via `?role=` or the Telegram start parameter.
- Home pages are simple role entry screens.
- Profile pages load and save through the backend profile API.
- Telegram helpers remain available for viewport, theme, back button, and haptics.

## Workspace rules

- Extend this template by patching real source files instead of layering parallel runtime systems.
- Preserve manual edits as separate git revisions.
- Keep all three roles available in preview simultaneously.
