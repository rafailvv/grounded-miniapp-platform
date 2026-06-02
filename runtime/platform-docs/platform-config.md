# Platform Config

`runtime/platform.config.json` is the product-quality contract for generation.
It controls the quality profile without editing Python code.

Authoritative schema:

- `platform/backend/app/schemas/platform.config.schema.json`
- API: `GET /system/platform-config/schema`

Current loaded config:

- API: `GET /system/platform-config`
- Manifest: `GET /system/platform-config/manifest`

## Contract Areas

- `generation_modes`: fast, balanced, quality, production, and basic mode objectives, proof requirements, final gates, audit level, output style, repair attempts, and completion budgets.
- `checks`: check labels, categories, blocking behavior, required modes, diagnostic evidence, and repair hints.
- `model_profiles`: model routing by task role, including `agent_turn`, `code_edit`, `repair`, `summarize`, and `cheap_task`.
- `default_profile_by_mode`: maps each generation mode to the model profile used when the request does not specify one.
- `skill_activation`: skill roots, mode-specific selection budgets, and required skill packs by quality mode.
- `browser_proof`: mobile viewport, modes that require browser proof, UI step evidence, persisted markers, screenshots, and replay artifacts.
- `sla`: default mode, full-audit modes, visual-snapshot modes, quality-like modes, compatibility notes, and second-queue platform capabilities.

## Local Overrides

Set `PLATFORM_CONFIG_PATH` to load a different config file. The file must match
`platform.config.schema.json`.

Existing model and budget environment overrides still apply on top of the config:

- `OPENAI_CODE_FAST_MODEL`
- `OPENAI_CODE_BALANCED_MODEL`
- `OPENAI_CODE_QUALITY_MODEL`
- `OPENAI_CODE_REPAIR_MODEL`
- `OPENAI_CODE_SUMMARY_MODEL`
- `CODE_AGENT_FAST_TIME_LIMIT_MS`
- `CODE_AGENT_FAST_TOKEN_LIMIT`
- `CODE_AGENT_BALANCED_TIME_LIMIT_MS`
- `CODE_AGENT_BALANCED_TOKEN_LIMIT`
- `CODE_AGENT_QUALITY_TIME_LIMIT_MS`
- `CODE_AGENT_QUALITY_TOKEN_LIMIT`
- `CODE_AGENT_PRODUCTION_TIME_LIMIT_MS`
- `CODE_AGENT_PRODUCTION_TOKEN_LIMIT`

## Regenerating Schema

Run from the repository root:

```bash
PYTHONPATH=platform/backend python3 - <<'PY'
import json
from pathlib import Path
from app.services.platform_config import platform_config_schema

Path("platform/backend/app/schemas/platform.config.schema.json").write_text(
    json.dumps(platform_config_schema(), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY
```
