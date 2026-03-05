# Canonical Base Mini-App Template

The grounded platform compiles `AppIR` into this template instead of regenerating entire applications.

## Template shape

- `backend/`: FastAPI service scaffold and generated artifact ingress.
- `frontend/`: React scaffold that can consume `src/generated/app-config.json`.
- `artifacts/`: generated `GroundedSpec`, traceability, and validation payloads.
- `docs/`: template documentation used as part of the grounded source-of-truth set.
- `docker/`: preview-oriented compose topology.

## Generated files

- `backend/app/generated/app_ir.json`
- `frontend/src/generated/app-config.json`
- `artifacts/grounded_spec.json`
- `artifacts/traceability.json`

