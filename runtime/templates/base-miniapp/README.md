# Canonical Base Mini-App Template

The grounded platform compiles `AppIR` into this template instead of regenerating entire applications.

## Template shape

- `backend/`: FastAPI service scaffold and generated artifact ingress.
- `frontend/`: React scaffold that renders generated runtime manifests and backend-driven role flows.
- `artifacts/`: generated `GroundedSpec`, traceability, and validation payloads.
- `docs/`: template documentation used as part of the grounded source-of-truth set.
- `docker/`: preview-oriented compose topology.

## Generated files

- `backend/app/generated/app_ir.json`
- `backend/app/generated/runtime_manifest.json`
- `backend/app/generated/runtime_state.json`
- `artifacts/grounded_spec.json`
- `artifacts/traceability.json`
