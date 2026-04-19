ROLE_ORDER = ("client", "specialist", "manager")
ROLE_COMPONENT_PREFIX = {
    "client": "Client",
    "specialist": "Specialist",
    "manager": "Manager",
}
DESIGN_REFERENCE_FILES = (
    "docs/README.md",
    "docs/components.md",
    "docs/generation-contract.md",
    "docs/ownership-contract.md",
    "docs/generic-persisted-workflow.md",
    "docs/anti-patterns.md",
    "miniapp/app/main.py",
    "miniapp/app/db.py",
    "miniapp/app/schemas.py",
    "miniapp/app/routes/client.py",
    "miniapp/app/routes/specialist.py",
    "miniapp/app/routes/manager.py",
    "miniapp/app/routes/profiles.py",
    "miniapp/app/routes/role_pages.py",
    "miniapp/app/static/shared/base.css",
    "miniapp/app/static/shared/common.js",
    "miniapp/app/static/client/index.html",
    "miniapp/app/static/specialist/index.html",
    "miniapp/app/static/manager/index.html",
    "miniapp/app/generated/route_manifest.json",
    "miniapp/app/generated/runtime_manifest.json",
)
SHARED_GENERATED_FILES = (
    "miniapp/app/main.py",
    "miniapp/app/routes/client.py",
    "miniapp/app/routes/specialist.py",
    "miniapp/app/routes/manager.py",
    "miniapp/app/routes/profiles.py",
    "miniapp/app/db.py",
    "miniapp/app/generated/route_manifest.json",
    "miniapp/app/generated/runtime_manifest.json",
)
WRITE_STRATEGIES = ("minimal_patch", "whole_file_build")
CANONICAL_FRONTEND_ROOTS = (
    "miniapp/app/static/client/",
    "miniapp/app/static/specialist/",
    "miniapp/app/static/manager/",
)
CANONICAL_BACKEND_ROOTS = (
    "miniapp/app/main.py",
    "miniapp/app/db.py",
    "miniapp/app/schemas.py",
    "miniapp/app/routes/",
    "miniapp/app/static/",
    "miniapp/app/generated/",
    "miniapp/requirements.txt",
    "miniapp/tests/",
)
CANONICAL_FILE_ROOTS = (*CANONICAL_FRONTEND_ROOTS, *CANONICAL_BACKEND_ROOTS, "artifacts/")
TEMPLATE_OWNED_SHARED_FILES = (
    "miniapp/app/static/preview_bridge.js",
)
LEGACY_ARCHITECTURE_MARKERS = (
    "frontend/",
    "miniapp/app/api/",
    "miniapp/app/application/",
    "miniapp/app/domain/",
    "miniapp/app/infrastructure/",
)
BUNDLE_CLUSTER_ORDER = (
    "backend_core",
    "role_manager_ui",
    "role_specialist_ui",
    "role_client_ui",
)
WORKFLOW_HEAVY_MARKERS = (
    "catalog",
    "storefront",
    "checkout",
    "cart",
    "product",
    "products",
    "order",
    "orders",
    "queue",
    "dashboard",
    "management",
    "workspace",
    "workflow",
    "detail",
    "details",
    "booking",
)
