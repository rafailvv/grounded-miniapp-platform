# Grounded Mini-App Platform Agent Guide

This repository is a product-generation platform, not a single generated app. Keep changes scoped to the platform layer that owns the requested behavior.

## Product Goal

- Optimize for fast creation of complete, working mini-app products.
- A completed generation must have usable role surfaces, persisted state, tests, preview proof, and a clear path for later fixes.
- Product semantics come from the user's prompt and stored workspace memory, not from the base template.

## Generation Quality

- Keep the `client`, `specialist`, and `manager` surfaces connected through shared backend state while preserving distinct role actions.
- Generate mobile-first Telegram mini-app UI for 360-430px widths.
- Prefer light, readable, operational UI unless the user explicitly requests a different theme.
- Do not add mock, demo, seed, sample, fixture, or hard-coded product records.
- Every create run should produce acceptance scenarios that prove the prompt-derived workflow without assuming a fixed domain or CRUD pattern.

## Platform Rules

- Preserve draft/apply boundaries. Agent writes go through draft tools and platform checks before apply.
- Keep generated/platform-owned files treated as metadata unless the owning compiler/materializer updates them.
- Add tests for platform behavior when changing validators, run lifecycle, workbench APIs, repair logic, tool policy, or context assembly.
- Keep APIs typed and additive. New Workbench surfaces should expose stable JSON with `status`, `items` or `issues`, and relevant artifact refs.
- When adding persistent records, reject secret-like content and include stale checks for path or route references.

## Verification

- Run focused backend tests for changed services.
- If frontend behavior changes, run the frontend build or the smallest available static check.
- For generation/runtime behavior, prefer evidence from check results, browser proof, trace reducer, and final report over prose claims.
