# Base Miniapp Template

Neutral FastAPI + static frontend shell for generated Telegram mini-apps.

- Product meaning must come from the user's prompt, not this template.
- Role roots are empty preview entrypoints only.
- Generated apps must create the requested product surface from the prompt.
- Keep FastAPI startup, static mounting, `/health`, `/static/shared/base.css`, `/static/preview_bridge.js`, and `route_manifest.json` valid.
- See `docs/agent-guidelines.md` for the compact agent guide.
