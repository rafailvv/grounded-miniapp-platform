---
metadata_schema: grounded.skill.v2
description: Services, specialists, masters, and provider marketplace workflow pack.
whenToUse:
  - услуги
  - услуга
  - мастер
  - мастера
  - специалист
  - специалисты
  - provider
  - specialist
  - service marketplace
  - professionals
paths:
  - miniapp/app/static/**
  - miniapp/app/routes/**
  - miniapp/tests/**
allowedTools:
  - read_files
  - search_files
  - apply_patch_to_draft
  - write_file
  - run_checks
  - browser_verify
model: default
effort: high
triggerRules:
  - Match prompts using this skill domain and path scope.
validation:
  - services_workflow
  - persisted_workflow
  - role_coverage
  - browser_flow_smoke
requiredProof:
  - Final readiness proof covers this skill.
incompatibleSkills:
  - ""
outputExpectations:
  - Produce working product changes and cite proof artifacts.
---
# Services / Masters

Use this skill when the product connects clients with masters, specialists, professionals, clinics, salons, tutors, or service providers.

## Rules

- Persist service, provider profile, skill tags, availability, rating or reviews, location or format, price, and request or booking status.
- Expose a client flow: compare providers, view service detail, select provider, submit request or booking, and see status.
- Expose a specialist flow: manage profile, availability, incoming requests, and completed work.
- Expose a manager flow: provider quality, workload, blocked providers, service coverage, and unresolved requests.
- Keep provider cards compact: name, role, rating or proof, price, next available time, and primary action.
- Avoid fake marketplaces where provider selection does not change the saved request.

## Acceptance

- API proof creates a provider-backed request or booking and reads it with provider details.
- Browser proof selects a real provider and verifies the selected provider is visible in specialist or manager view.
- Role coverage proves client, specialist, and manager see role-specific service data.
- Mobile proof validates provider list, detail, and action form on Telegram width.
- Tests cover provider selection persistence.
