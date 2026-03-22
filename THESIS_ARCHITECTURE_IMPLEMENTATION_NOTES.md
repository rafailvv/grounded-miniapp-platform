# Grounded Mini-App Platform: Architecture, Prototype and Implementation Notes

Дата фиксации: 2026-03-22

Основа этого документа: реальный код и артефакты репозитория, а не только исходная исследовательская идея. Важно учитывать, что в проекте сейчас сосуществуют два слоя:

1. Research/reference architecture:
   `prompt -> retrieval -> GroundedSpec -> AppIR -> validators -> artifact plan -> patch -> preview`
   Этот путь хорошо виден в `README.md`, контрактах, моделях и более ранних workspace-артефактах.

2. Current execution architecture:
   `prompt -> retrieval -> GroundedSpec -> role contract -> page graph / code plan -> context pack -> draft edits -> validators/checks -> preview -> auto-apply or manual approval`
   Именно этот путь сейчас является основным в `platform/backend/app/services/generation_service.py` и `run_service.py`.

Поэтому ниже я всегда явно отделяю:

- что является текущим рабочим pipeline;
- что присутствует как формализованный contract/legacy path;
- что уже реализовано частично или используется только в старых run.

## Chapter 3. Architecture

### 3.1. Краткое описание системы своими словами

Система представляет собой workspace-oriented платформу для grounded generation mini-app приложений из пользовательского prompt, встроенной документации шаблона, платформенных ограничений и состояния текущего workspace. Пользователь не просит систему “написать код с нуля”, а работает с контролируемым pipeline, в котором генерация проходит через формальные промежуточные артефакты, deterministic checks и preview runtime. Входом является prompt, а также документы workspace, код текущего workspace, template docs и platform docs. Выходом является не только код, но и набор исследовательских артефактов: `GroundedSpec`, page graph / plan, diff, reports, validation/check outputs, preview URL и revision history. LLM используется для synthesis и planning: построение grounded specification, role contract, page graph, таргетированных edits, repair steps и краткого summary. Deterministic logic используется для retrieval, indexing, patch application, revision control, validation, static checks, preview boot checks, rollback и export. Человек может подтверждать draft вручную, если run запущен в режиме `manual_approve`, а в текущем UI также может просматривать diff, артефакты, логи и откатывать уже применённый run. В авто-режиме draft может быть применён сразу после успешных checks, но даже тогда изменения остаются revision-aware и откатываемыми. Preview представляет собой не screenshot, а реальный runtime шаблона mini-app, поднятый отдельно через Docker. Это важно для исследовательской постановки, потому что система проверяет не только синтаксис, но и то, что результат реально стартует и отдаёт работающие role routes.

### 3.2. Реальный pipeline

#### 3.2.1. Research/reference pipeline

Это та цепочка, вокруг которой спроектированы contracts и ранние артефакты:

1. Пользователь пишет prompt.
2. Система выполняет retrieval по:
   - workspace documents;
   - template docs;
   - platform docs;
   - code index текущего workspace.
3. Строится `GroundedSpec`.
4. `GroundedSpec` проходит deterministic validation.
5. На базе `GroundedSpec` строится `AppIR`.
6. `AppIR` проходит `AppIRValidator` и `PlatformValidator`.
7. Из `AppIR` компилируется artifact plan / runtime manifest / traceability.
8. План превращается в patch operations.
9. Patch применяется к canonical template.
10. Выполняются build/preview checks.
11. Результат попадает в preview.
12. Далее возможны apply / rollback / export.

Этот путь действительно материализован в коде:

- contracts: `contracts/grounded-spec.v1.json`, `contracts/app-ir.v1.json`;
- models: `platform/backend/app/models/grounded_spec.py`, `platform/backend/app/models/app_ir.py`;
- legacy compiler helpers: `_resolve_app_ir()`, `_build_app_ir()`, `_build_artifact_plan()` в `generation_service.py`;
- ранний реальный пример: workspace `ws_980a7566d17d4d3a931b01899c5f41de`, где есть `source/backend/app/generated/app_ir.json`.

#### 3.2.2. Current operational pipeline

Это фактический основной путь в текущем `generate()`:

1. Пользователь создаёт или открывает workspace.
   На старте workspace клонируется canonical template в `data/workspaces/<workspace_id>/source`, инициализируется git и сохраняется первая revision `template_clone`.

2. Пользователь вводит prompt через platform frontend.
   UI вызывает `POST /workspaces/{workspace_id}/runs`.

3. `RunService` создаёт `RunRecord`.
   Фиксируются:
   - prompt;
   - mode (`generate` или `fix`);
   - apply strategy (`staged_auto_apply` или `manual_approve`);
   - source revision;
   - target role scope;
   - generation mode;
   - model profile.

4. `GenerationService` делает preflight.
   Проверяется:
   - что template был клонирован;
   - что есть required corpora;
   - что LLM configured;
   - что workspace index обновлён.

5. Retrieval.
   `DocumentIntelligenceService.retrieve()` собирает `DocRef` из:
   - пользовательских документов workspace;
   - template docs;
   - platform docs;
   - code chunks из `CodeIndexService`;
   - самого prompt как `user_prompt`.

6. GroundedSpec.
   `_resolve_grounded_spec()` синтезирует `GroundedSpec`, затем `ValidationSuite.validate_grounded_spec()` проверяет его на blocking issues.

7. Draft preparation.
   `WorkspaceService.prepare_draft()` создаёт draft-копию текущего source revision в `data/workspaces/<workspace_id>/drafts/<run_id>/source`.

8. Role contract.
   `_resolve_role_contract()` уточняет обязанности ролей `client`, `specialist`, `manager`, а затем проходит gate, проверяющий, что роли действительно различаются.

9. Planning / page graph / targeting.
   `_resolve_code_plan()` строит:
   - `page_graph`;
   - `files_to_read`;
   - `target_files`;
   - `backend_targets`;
   - `scope_mode`;
   - `write_strategy`;
   - execution plan.

10. Context pack.
    `ContextPackBuilder.build()` подбирает code chunks, recent diff и содержимое целевых файлов для следующего LLM step.

11. Editing.
    `_resolve_code_edits()` генерирует `DraftFileOperation[]` для целевых файлов.

12. Patch envelope.
    Draft operations конвертируются в `PatchEnvelope` с:
    - `base_revision_id`;
    - file-level operations;
    - unified diffs;
    - file hash preconditions.

13. Patch apply to draft.
    `WorkspaceService.apply_patch_envelope_to_draft()` применяет изменения сначала не к source, а к draft.

14. Validators and checks.
    `CheckRunner.run()` выполняет:
    - `schema_validators` через `BuildValidator`;
    - `connectivity_validators` через `ConnectivityValidator`;
    - `changed_files_static` через `npm run build` для frontend-проектов или `py_compile` для miniapp backend;
    - `preview_boot_smoke`;
    - `preview_connectivity_smoke`.

15. Automatic repair loop.
    Если checks не проходят, система пытается сделать bounded repair:
    - анализирует failure signature;
    - сужает или расширяет repair scope;
    - генерирует новый patch;
    - заново применяет его к draft;
    - перезапускает checks.

16. Draft ready.
    Если checks зелёные, сохраняются run artifacts:
    - grounded spec;
    - role contract;
    - page graph;
    - iterations;
    - checks;
    - patch summary;
    - preview info.

17. Apply.
    Дальше два режима:
    - `staged_auto_apply`: draft автоматически переносится в `source`, создаётся git revision `ai_patch`;
    - `manual_approve`: run переходит в `awaiting_approval`, и пользователь вручную жмёт approve.

18. Preview.
    После apply платформа делает rebuild preview и обновляет три role URLs:
    - `/client`;
    - `/specialist`;
    - `/manager`.

19. Rollback / discard / export.
    Дальше пользователь может:
    - rollback applied run;
    - discard draft;
    - вручную редактировать файлы;
    - export workspace в zip или git patch.

#### 3.2.3. Где в этой схеме сейчас находится AppIR

Здесь важна честная формулировка для thesis:

- `AppIR` в проекте строго определён и поддерживается как typed contract.
- Есть реальный код генерации и валидации `AppIR`.
- Есть реальный ранний run, где `AppIR` был materialized и использован.
- Но в текущем основном `generate()` path `AppIR` не является центральным execution artifact.
- Сейчас operational pipeline проходит через `GroundedSpec -> role contract -> page graph / code plan -> edits`, а не через full `GroundedSpec -> AppIR -> compiler`.

То есть для главы 3 лучше писать так: `AppIR` остаётся формализованным architectural layer и частью исследовательской конструкции, но текущий production-like prototype сместился к более прагматичному agentic planning/edit pipeline.

### 3.3. Что входит в GroundedSpec

Фактическая модель `GroundedSpecModel` содержит:

- `schema_version`
- `metadata`
  - `workspace_id`
  - `conversation_id`
  - `prompt_turn_id`
  - `template_revision_id`
  - `language`
  - `created_at`
- `target_platform`
- `preview_profile`
- `product_goal`
- `actors`
  - `actor_id`
  - `name`
  - `role`
  - `description`
  - `permissions_hint`
  - `evidence`
- `domain_entities`
  - `entity_id`
  - `name`
  - `description`
  - `attributes`
  - `evidence`
- `user_flows`
  - `flow_id`
  - `name`
  - `goal`
  - `steps`
  - `acceptance_criteria`
  - `evidence`
  - `preconditions`
  - `postconditions`
  - `error_paths`
- `ui_requirements`
- `api_requirements`
- `persistence_requirements`
- `integration_requirements`
- `security_requirements`
- `platform_constraints`
- `non_functional_requirements`
- `assumptions`
- `unknowns`
- `contradictions`
- `doc_refs`

Если перевести это на язык thesis-главы, то `GroundedSpec` покрывает:

- goal;
- actors;
- flows;
- entities;
- UI/API/persistence requirements;
- constraints;
- unknowns;
- contradictions;
- integrations;
- security boundaries;
- traceability back to retrieved evidence.

### 3.4. Что входит в AppIR

Фактическая модель `AppIRModel` содержит:

- `schema_version`
- `metadata`
  - `workspace_id`
  - `grounded_spec_version`
  - `template_revision_id`
  - `generated_at`
- `app_id`
- `title`
- `platform`
- `preview_profile`
- `entry_screen_id`
- `variables`
  - `variable_id`
  - `name`
  - `type`
  - `required`
  - `source`
  - `trust_level`
  - `scope`
  - `default`
  - `pii`
- `entities`
- `screens`
  - `screen_id`
  - `kind`
  - `title`
  - `components`
  - `actions`
  - `subtitle`
  - `on_enter_actions`
  - `platform_hints`
- `transitions`
- `route_groups`
  - role-based route mapping
- `screen_data_sources`
- `role_action_groups`
- `integrations`
- `storage_bindings`
- `auth_model`
- `permissions`
- `security`
  - `trusted_sources`
  - `untrusted_sources`
  - `secret_handling`
  - `pii_variables`
- `telemetry_hooks`
- `assumptions`
- `open_questions`
- `traceability`
- `terminal_screen_ids`

В терминах исследовательского описания это значит, что `AppIR` формализует:

- screens;
- actions;
- transitions;
- permissions;
- entities;
- variables;
- storage bindings;
- API/integrations;
- role mapping;
- security/trust model;
- traceability between source evidence and compiled app structure.

### 3.5. Какие validator gates реально есть

Ниже перечислены не теоретические gates, а именно существующие в коде.

| Gate | Где реализован | Статус | Что проверяет |
|---|---|---|---|
| GroundedSpec schema/model validation | Pydantic models + `GroundedSpecValidator` | Работает | Заполненность `product_goal`, наличие actors/flows/platform constraints, полнота API requirements, critical contradictions |
| AppIR validation | `AppIRValidator` | Реализован, но не основной gate текущего generate path | Ссылочная целостность screens/actions/variables/integrations/transitions, route groups на 3 роли, trust rules |
| Platform security validation | `PlatformValidator` | Реализован, но завязан на AppIR path | Для Telegram требует `validated_init_data` как trusted source; для MAX аналогично host session |
| Build shape validation | `BuildValidator` | Работает | Наличие required scaffold files, отсутствие contract drift, отсутствие legacy architecture roots, placeholder pages |
| Connectivity validation | `ConnectivityValidator` | Работает | Соответствие UI/API wiring, наличие backend routes, loading/error states, отсутствие “placeholder dynamic page” |
| Static compile/build checks | `CheckRunner._static_check()` | Работает | `npm run build` для frontend-проектов; `python -m py_compile` для miniapp backend |
| Preview boot smoke | `CheckRunner` + `PreviewService` | Работает | Что preview runtime действительно стартует |
| Preview route smoke | `CheckRunner._preview_connectivity_smoke()` | Работает | Что root role routes реально отвечают usable content |
| Patch base revision check | `WorkspaceService.apply_patch_envelope()` | Работает | Что patch строился относительно актуальной base revision |
| Patch precondition hash check | `WorkspaceService._apply_envelope_to_target()` | Работает | Что файл не изменился с момента построения patch |
| Runtime role checks в generated app | generated app routes | Работает в конкретном generated workspace | Например manager-only create/delete product, staff-only order listing |
| Formal schema JSON validation against external contract files | contracts есть, но не отдельный runtime gate в main path | Частично | Скорее architectural contract layer, чем отдельный вызов на каждом generate |
| Human approval gate | `manual_approve` | Работает | Пользователь вручную решает, переносить ли draft в source |
| Background queue/job worker gate | `workers/*` | Пока не реализован | Сейчас есть только placeholder-файлы |

Практически для текущего prototype самые важные active gates такие:

- spec gate;
- role separation gate;
- page graph gate;
- build shape gate;
- connectivity gate;
- compile gate;
- preview boot/preview route gate;
- patch conflict gate.

### 3.6. Как устроен patch / revision lifecycle

#### 3.6.1. Уровень patch

Patch в проекте сейчас file-level, а не AST-level и не block-level. Базовая операция описывается `PatchOperationModel`:

- `op`: `create`, `update`, `delete`
- `file_path`
- `content`
- `diff`
- `explanation`
- `trace_refs`
- `precondition`

То есть система обычно генерирует полные contents целевых файлов, а diff хранится как вспомогательный артефакт для обзора и traceability.

#### 3.6.2. Base revision

Да, base revision есть. Она хранится в `PatchEnvelope.base_revision_id`. При apply система проверяет:

- что `base_revision_id` совпадает с `workspace.current_revision_id`;
- что file hash целевого файла всё ещё совпадает с precondition hash.

Если нет, возвращается `ApplyPatchResult(status="conflict")`.

#### 3.6.3. Конфликты

Конфликт фиксируется в двух основных случаях:

- stale base revision;
- mismatch file hash на конкретном файле.

Это не merge conflict в git-стиле на уровне строк. Это controlled patch conflict на уровне “ты строил patch не на той версии файла”.

#### 3.6.4. Draft-first lifecycle

Текущий жизненный цикл выглядит так:

1. Из `source/` создаётся draft.
2. Patch применяется к draft.
3. На draft гоняются validators/checks/preview smoke.
4. Если всё проходит:
   - либо draft auto-apply в source;
   - либо draft ждёт ручного approve.
5. После apply создаётся git commit и новая revision.

Это важная архитектурная деталь: система не пишет сразу в основную рабочую копию.

#### 3.6.5. Approve / apply

Есть два режима:

- `staged_auto_apply`
  - draft автоматически переносится в source;
  - создаётся git revision с source `ai_patch`.

- `manual_approve`
  - run переходит в `awaiting_approval`;
  - пользователь жмёт `/runs/{run_id}/approve`;
  - тогда `WorkspaceService.approve_draft()` заменяет source содержимым draft и коммитит revision.

#### 3.6.6. Rollback

Есть два rollback-механизма:

- rollback workspace до предыдущей revision;
- rollback applied run через `git revert` конкретной AI revision.

В `RunService.rollback_run()` откат разрешён только для уже применённого completed run.

### 3.7. Какие ограничения mini-app platform учитываются

Фактические ограничения, которые явно зашиты в текущую систему:

1. Три фиксированные роли:
   - `client`
   - `specialist`
   - `manager`

2. Canonical template structure.
   Генерация должна укладываться в:
   - `miniapp/app/main.py`
   - `miniapp/app/db.py`
   - `miniapp/app/schemas.py`
   - `miniapp/app/routes/*`
   - `miniapp/app/static/*`
   - `artifacts/*`

3. Role-aware preview.
   Preview всегда предполагает одновременное существование трёх role surfaces и трёх URL.

4. Trusted vs untrusted inputs.
   В `AppIR` и platform validator формализована граница:
   - `user_input` не может быть trusted;
   - для Telegram trusted source должен быть `validated_init_data`.

5. Template fit.
   Запрещено “расползаться” в legacy roots вроде:
   - `frontend/`
   - `miniapp/app/api/`
   - `miniapp/app/application/`
   - `miniapp/app/domain/`
   - `miniapp/app/infrastructure/`
   Это прямо проверяется `BuildValidator`.

6. Ограниченный runtime.
   Preview работает в простом Docker runtime на `python:3.12-slim`; для miniapp template нет отдельной полноразмерной infra stack. Это значит:
   - ограниченный набор runtime dependencies;
   - boot должен быть достаточно простым;
   - generated app лучше держать в рамках lightweight FastAPI + static assets.

7. Telegram-first assumptions.
   В bundled docs и validators проект Telegram-first:
   - theme / viewport respect;
   - back behavior;
   - validated init data;
   - роль/сессия не должны доверяться сырому host payload.

8. Persistence should be local and simple.
   Canonical template ориентирован на SQLite в runtime, а не на внешнюю БД или распределённую очередь.

9. No parallel app architectures.
   Template docs прямо говорят не реинтродуцировать параллельный frontend stack и extra service layers внутри generated runtime.

### 3.8. Пример реального пользовательского сценария

Возьмём живой сценарий: flower shop mini-app.

#### Сценарий

Пользователь вводит prompt про мини-приложение для цветочного магазина:

- manager создаёт, редактирует и удаляет продукты;
- specialist видит продукты и заказы, но не создаёт и не удаляет продукты;
- client просматривает каталог, открывает карточки товаров, добавляет в корзину и оформляет заказ;
- каталог пустой на старте;
- категории фиксированы;
- данные реально сохраняются.

#### Как сценарий проходит через архитектуру

1. Prompt попадает в `RunService`.
2. Retrieval подтягивает template docs, platform docs и текущий workspace code.
3. `GroundedSpec` фиксирует:
   - actors: manager/specialist/client;
   - entities: product, category, order;
   - flows: catalog management, storefront browsing, checkout, staff-side order review;
   - constraints: fixed categories, empty catalog on startup, role restrictions.
4. Planning строит page graph:
   - client pages: storefront, product details, cart, profile;
   - specialist pages: product/order workspace, profile;
   - manager pages: catalog management, profile;
   - shared backend targets: `db.py`, `main.py`, `routes/*`, `schemas.py`.
5. Editing генерирует draft operations по конкретным файлам.
6. Patch применяется к draft.
7. Checks подтверждают:
   - fixed categories seeded;
   - catalog starts empty;
   - role permissions;
   - order flow;
   - preview runtime healthy.
8. Draft auto-applies в source, создаётся git revision.
9. Preview rebuild поднимает role URLs:
   - `/client`
   - `/specialist`
   - `/manager`
10. Если нужно, пользователь может посмотреть diff, роллбэкнуть run или экспортировать проект.

## Chapter 4. Prototype and Implementation

### 4.1. Технологический стек

#### Platform backend

- Python 3.12
- FastAPI
- Pydantic v2
- httpx
- Uvicorn

#### Platform frontend

- TypeScript
- React 18
- Vite

#### Generated mini-app runtime

- Python 3.12
- FastAPI
- SQLAlchemy 2
- plain HTML/CSS/JavaScript per role
- SQLite

#### Storage / state

- file-backed JSON state store для платформы (`data/platform-state.json`)
- git revision history внутри каждого workspace
- SQLite inside generated mini-app runtime (`miniapp/app/generated/app.db`)

#### Retrieval / index

- custom file-backed code index
- lexical + lightweight local embedding-like hashing
- без отдельного внешнего vector database

#### Preview runtime

- Docker Compose
- отдельный per-workspace runtime port
- health-based startup detection

#### Background jobs

- реальной очереди нет
- runs выполняются через threads внутри backend process
- `workers/*` пока placeholder для будущей queue integration

#### Infra / dev tools

- Docker / Docker Compose
- git
- pytest
- npm / Vite build checks для frontend-type workspaces

### 4.2. Структура проекта / репозитория

Сейчас полезно описывать репозиторий так:

```text
contracts/
  grounded-spec.v1.json
  app-ir.v1.json

platform/
  backend/
    app/
      ai/
      api/
      core/
      models/
      repositories/
      services/
      validators/
      workers/
    tests/
    pyproject.toml
  frontend/
    src/
      App.tsx
      lib/api.ts
      main.tsx
      styles/app.css
    package.json

runtime/
  platform-docs/
    telegram/
    max/
  templates/
    base-miniapp/
      docs/
      docker/
      miniapp/
        app/
          routes/
          static/
          generated/

data/
  platform-state.json
  workspaces/
  exports/
```

#### Где что находится

- platform frontend: `platform/frontend`
- platform backend: `platform/backend`
- canonical template: `runtime/templates/base-miniapp`
- contracts: `contracts`
- platform constraints docs: `runtime/platform-docs`
- workspace state: `data/workspaces`
- global platform state: `data/platform-state.json`
- exports: `data/exports`

### 4.3. Какие backend-сервисы реально реализованы

Реально существующие сервисы:

- `WorkspaceService`
  - создание workspace
  - clone/reset template
  - draft lifecycle
  - git revisions
  - file tree / file content / manual save
  - diff / rollback / patch apply

- `RunService`
  - run lifecycle
  - run creation
  - apply / discard / stop / rollback
  - artifact packaging

- `GenerationService`
  - основной generate pipeline
  - retrieval orchestration
  - GroundedSpec synthesis
  - role contract
  - planning/page graph
  - code edits
  - repair loop
  - report generation

- `FixOrchestrator`
  - bounded fix workflow для failed runs
  - triage
  - exact checks
  - scope expansion
  - repair patching

- `DocumentIntelligenceService`
  - document save/index
  - chunking
  - doc retrieval
  - required corpora checks

- `CodeIndexService`
  - workspace indexing
  - code chunk retrieval
  - lightweight ranking

- `ContextPackBuilder`
  - context assembly for LLM edit steps

- `PatchService`
  - thin wrapper around patch apply

- `CheckRunner`
  - build/connectivity/static/preview checks

- `PreviewService`
  - preview lifecycle
  - start / ensure / rebuild / reset
  - logs and role URLs

- `PreviewRuntimeManager`
  - actual Docker Compose runtime orchestration

- `ExportService`
  - export zip
  - export git patch

- `WorkspaceLogService`
  - per-workspace logs

Сервисы, которые пока есть только как заготовки:

- `generation_worker.py`
- `preview_worker.py`
- `export_worker.py`

### 4.4. Какие endpoints или основные операции есть

#### Workspace

- `POST /workspaces`
- `GET /workspaces`
- `GET /workspaces/{workspace_id}`
- `POST /workspaces/{workspace_id}/clone-template`
- `POST /workspaces/{workspace_id}/reset`
- `POST /workspaces/{workspace_id}/rollback`
- `POST /workspaces/{workspace_id}/index`
- `GET /workspaces/{workspace_id}/index/status`
- `DELETE /workspaces/{workspace_id}`

#### Documents

- `POST /workspaces/{workspace_id}/documents`
- `GET /workspaces/{workspace_id}/documents`
- `POST /documents/{document_id}/index`
- `GET /documents/{document_id}/chunks`

#### Chat

- `POST /workspaces/{workspace_id}/chat/turns`
- `GET /workspaces/{workspace_id}/chat/turns`
- `GET /turns/{turn_id}/summary`

#### Generation / runs

- `POST /workspaces/{workspace_id}/generate`
- `POST /workspaces/{workspace_id}/runs`
- `GET /workspaces/{workspace_id}/runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/artifacts`
- `GET /runs/{run_id}/iterations`
- `GET /runs/{run_id}/checks`
- `GET /runs/{run_id}/patch`
- `POST /runs/{run_id}/apply`
- `POST /runs/{run_id}/approve`
- `POST /runs/{run_id}/discard`
- `POST /runs/{run_id}/stop`
- `POST /runs/{run_id}/rollback`

#### Validation / artifacts

- `GET /workspaces/{workspace_id}/spec/current`
- `GET /workspaces/{workspace_id}/ir/current`
- `GET /workspaces/{workspace_id}/validation/current`
- `GET /workspaces/{workspace_id}/assumptions/current`
- `GET /workspaces/{workspace_id}/traceability/current`
- `POST /workspaces/{workspace_id}/validation/run`

#### Files / diff

- `GET /workspaces/{workspace_id}/files/tree`
- `GET /workspaces/{workspace_id}/files/content`
- `POST /workspaces/{workspace_id}/files/save`
- `GET /workspaces/{workspace_id}/diff`

#### Preview / logs

- `POST /workspaces/{workspace_id}/preview/start`
- `POST /workspaces/{workspace_id}/preview/ensure`
- `POST /workspaces/{workspace_id}/preview/rebuild`
- `POST /workspaces/{workspace_id}/preview/reset`
- `GET /workspaces/{workspace_id}/preview/url`
- `GET /workspaces/{workspace_id}/preview/logs`
- `GET /workspaces/{workspace_id}/logs`

#### Export

- `POST /workspaces/{workspace_id}/export/zip`
- `POST /workspaces/{workspace_id}/export/git-patch`
- `GET /exports/{export_id}/download`

### 4.5. Как устроен frontend

Platform frontend фактически является single-page orchestrator UI.

Основные панели/зоны:

- workspace drawer
  - список workspace
  - создание нового
  - удаление

- run composer
  - prompt input
  - режим `generate` / `fix`
  - generation mode

- run timeline
  - список runs
  - статус
  - progress
  - rollback / stop / fix handoff

- run details
  - prompt
  - checks summary
  - failure analysis
  - touched files
  - iterations

- artifacts / diff
  - generated diff viewer
  - code change plan
  - spec/trace/check-related payloads

- file tree
  - tree текущего source или draft
  - file content reading/editing

- preview surface
  - три role phone-like preview panes
  - per-role refresh/back/close
  - preview status/errors

- logs
  - workspace logs
  - preview logs
  - mini-app container logs

### 4.6. Как устроен canonical template

Canonical template сейчас intentionally minimal.

#### Что уже зашито

- 3 fixed roles:
  - client
  - specialist
  - manager

- baseline backend:
  - FastAPI app
  - health endpoint
  - profile routes
  - SQLite persistence for role profiles

- baseline frontend:
  - plain static HTML/CSS/JS
  - separate files per role
  - shared preview bridge

- baseline preview:
  - `docker/docker-compose.yml`
  - один lightweight service `preview-app`

#### Что именно мутируется генерацией

В рамках current architecture генерация обычно меняет:

- `miniapp/app/db.py`
- `miniapp/app/main.py`
- `miniapp/app/schemas.py`
- `miniapp/app/routes/*`
- `miniapp/app/static/client/*`
- `miniapp/app/static/specialist/*`
- `miniapp/app/static/manager/*`
- `miniapp/app/static/preview-bridge.js`
- `artifacts/*`
- иногда `docs/README.md`

#### Что нежелательно менять

Для thesis полезно формулировать это как template invariants:

- не уходить в параллельную frontend architecture;
- не создавать альтернативный backend tree вне canonical roots;
- не ломать 3-role preview model;
- не выносить runtime в слишком сложную multi-service infra;
- не разрушать git/revision-aware lifecycle.

### 4.7. Как устроен preview

Preview сейчас docker-based.

#### Как запускается

1. `PreviewService.ensure_started()` или `rebuild_async()`
2. `PreviewRuntimeManager` выбирает порт
3. рендерится временный compose file
4. запускается `docker compose up -d --build`
5. идёт health polling по `/health`
6. при успехе сохраняются:
   - `url`
   - `backend_url`
   - `role_urls`
   - `latency_breakdown`

#### Какие статусы есть

`PreviewRecord` хранит:

- `status`: `stopped`, `starting`, `running`, `error`
- `stage`: `idle`, `starting`, `rebuilding`, `health_check`, `running`, `error`
- `progress_percent`
- `last_error`

#### Как собираются boot logs

Собираются:

- preview service logs;
- docker compose output;
- health probe logs;
- container logs per service.

#### Как пользователь видит preview

Во frontend это три параллельных embedded panes для:

- client
- specialist
- manager

Это не mock screenshot, а живой runtime.

### 4.8. Что пользователь реально может делать

На текущем прототипе пользователь может:

- создать workspace;
- клонировать template;
- загрузить документы;
- проиндексировать workspace/documents;
- ввести prompt;
- запустить `generate`;
- запустить `fix`;
- смотреть spec / artifacts / iterations / checks / logs;
- смотреть diff;
- смотреть file tree;
- вручную редактировать файл;
- стартовать / перестраивать preview;
- подтвердить draft вручную;
- автоматически применять успешный draft;
- discard draft;
- rollback применённый run;
- export workspace в zip или git patch.

## Concrete Examples

### 4.9. Реальные prompt examples

#### Example A: Flower shop

Исходный prompt:

```text
Create a simple mini app for a real flower shop with three roles: manager, specialist, and client...
```

Что система из него построила:

- manager-side catalog management;
- specialist-side product/order operational workspace;
- client storefront + cart + order flow;
- SQLite persistence;
- fixed categories seeded on startup;
- empty catalog on first boot.

#### Example B: Consultation booking

Исходный prompt:

```text
Build a Telegram mini-app for consultation booking with three connected roles...
```

Что система из него построила в раннем AppIR-based path:

- `GroundedSpec` с общей сущностью `Booking`;
- `AppIR` со screens, actions, transitions и route groups;
- compiled runtime manifest;
- patch plan, применённый к canonical template.

### 4.10. Пример GroundedSpec

Это компактный фрагмент реального flower-shop `grounded_spec.json`:

```json
{
  "product_goal": "Telegram mini app for a real flower shop with role-based operations...",
  "actors": [
    {
      "actor_id": "actor_manager",
      "role": "manager",
      "permissions_hint": [
        "add product",
        "edit product",
        "remove product",
        "set availability",
        "select from predefined categories",
        "upload product photo"
      ]
    },
    {
      "actor_id": "actor_specialist",
      "role": "specialist",
      "permissions_hint": [
        "view product list",
        "edit existing product",
        "view incoming orders"
      ]
    },
    {
      "actor_id": "actor_client",
      "role": "client",
      "permissions_hint": [
        "browse catalog",
        "open product details",
        "add to cart",
        "place order"
      ]
    }
  ],
  "domain_entities": [
    {
      "entity_id": "entity_product",
      "name": "Product"
    }
  ]
}
```

### 4.11. Пример AppIR

Ниже компактный фрагмент реального consultation-booking `app_ir.json` из раннего AppIR-based workspace:

```json
{
  "app_id": "app_unknown",
  "platform": "telegram_mini_app",
  "entry_screen_id": "client_home",
  "variables": [
    {
      "variable_id": "var_client_name",
      "source": "user_input",
      "trust_level": "untrusted",
      "scope": "screen",
      "pii": true
    },
    {
      "variable_id": "var_current_role",
      "source": "validated_init_data",
      "trust_level": "trusted",
      "scope": "session",
      "pii": false
    }
  ],
  "entities": [
    {
      "entity_id": "entity_booking",
      "name": "Booking"
    }
  ]
}
```

Для текста thesis это удобно как пример того, что `AppIR` является именно typed execution representation, а не просто словесным описанием UI.

### 4.12. Пример patch / diff

Фрагмент реального flower-shop diff:

```diff
+class CategoryRecord(Base):
+    __tablename__ = "categories"
+
+class ProductRecord(Base):
+    __tablename__ = "products"
+
+class OrderRecord(Base):
+    __tablename__ = "orders"
+
+FIXED_CATEGORIES = [
+    {"category_id": "bouquets", "name": "Bouquets", "active": True},
+    {"category_id": "roses", "name": "Roses", "active": True},
+    {"category_id": "tulips", "name": "Tulips", "active": True}
+]
```

И фрагмент расширения runtime wiring:

```diff
+from app.routes.auth import router as auth_router
+from app.routes.categories import router as categories_router
+from app.routes.orders import router as orders_router
+from app.routes.products import router as products_router
+
+app.include_router(auth_router)
+app.include_router(categories_router)
+app.include_router(products_router)
+app.include_router(orders_router)
+ensure_fixed_categories()
```

Это хорошо иллюстрирует, что patch касается и data layer, и route layer, и role UI surfaces.

### 4.13. Один реальный run

Ниже данные по живому run `run_f28ce4ab47914200b13e14d42fd50f3e` из flower-shop workspace.

#### Prompt

- Flower shop prompt с ролями manager / specialist / client, empty catalog, fixed categories, storefront/cart/order flow.

#### Retrieved / grounded context

- `doc_refs`: 8
- sources:
  - template docs
  - platform docs
  - workspace code index
  - prompt itself

#### Generated artifacts

- `artifacts/generated_app_graph.json`
- `artifacts/grounded_spec.json`
- role-specific static pages
- backend routes for `auth`, `categories`, `products`, `orders`
- DB models for category/product/order

#### Validation / checks result

Generic checks:

- `schema_validators`: passed
- `connectivity_validators`: passed
- `changed_files_static`: passed
- `preview_boot_smoke`: passed
- `preview_connectivity_smoke`: passed

Scenario-specific artifact checks stored in run artifacts:

- `fixed_categories_seeded`: passed
- `catalog_starts_empty`: passed
- `role_permissions`: passed
- `order_flow`: passed
- `preview_runtime`: passed

#### Preview result

- preview URL: `http://localhost:16307`
- role URLs:
  - `http://localhost:16307/client`
  - `http://localhost:16307/specialist`
  - `http://localhost:16307/manager`
- runtime mode: `docker`
- first healthy boot: health probe passed on attempt 20

#### Final decision

- patch applied successfully
- `base_revision_id`: `rev_c6aef3fd01324879802be968f0e6a27a`
- new revision: `rev_2c7420735d5642b3889734221486b861`
- changed files: 23
- final git commit message: `Apply flower shop mini app`

## Evaluation Preparation

### 5.1. Что уже сейчас можно измерять

Из текущих `RunRecord`, `JobRecord`, `PreviewRecord` и reports уже можно измерять:

- generation time
  - `retrieval_ms`
  - `context_pack_ms`
  - `patch_apply_ms`
  - `checks_ms`
  - `total_ms`

- preview boot success
  - `preview.status`
  - `preview.last_error`
  - `preview latency_breakdown`

- validator pass rate
  - `schema_validators`
  - `connectivity_validators`
  - static compile/build
  - preview smoke

- patch apply success
  - `ApplyPatchResult.status`
  - `conflict_reason`

- repair burden
  - number of repair iterations
  - fix attempts
  - scope expansions

- rollback frequency
  - `rolled_back`
  - `rolled_back_at`

- amount of change
  - number of touched files
  - changed files per patch

### 5.2. Набор тестовых сценариев, который логично собрать

Сейчас в repo нет формализованного benchmark corpus на 10-20 сценариев, но архитектура уже позволяет его собрать. Практически полезный набор:

1. Consultation booking
2. Flower shop storefront
3. Beauty salon appointment booking
4. Lead capture mini-app
5. Service request intake
6. Multi-role approval flow
7. Delivery order status flow
8. Staff queue processing
9. Catalog + cart + checkout
10. Admin dashboard + operator workspace + client entry flow
11. Medical intake with constrained fields
12. Event registration flow

### 5.3. Категории сценариев

- booking
- lead capture
- service workflow
- catalog + checkout
- multi-role coordination
- approval / reassignment flow
- dashboard + operations

### 5.4. Ожидаемые свойства результата

Для каждого сценария имеет смысл фиксировать:

- required roles
- required entities
- required transitions
- required screens
- required permissions
- required persistence
- required platform/security constraints

### 5.5. Baseline’ы

Честная формулировка по текущему состоянию:

- baseline “без retrieval” сейчас не вынесен как отдельный переключаемый режим;
- baseline “без validation” тоже не вынесен в UI/API как режим;
- baseline “full rewrite vs patch-based” частично сравним концептуально, но не оформлен как экспериментальный harness;
- зато в репозитории уже есть два архитектурных среза, которые можно использовать как qualitative baseline:
  - более ранний `GroundedSpec -> AppIR -> artifact plan` path;
  - текущий `GroundedSpec -> role contract -> page graph -> targeted edits` path.

Для главы 5 корректнее писать, что полноценный evaluation harness ещё нужно формализовать, но instrumentation для измерений уже встроен.

## Short Thesis-Ready Summary

Если нужен совсем краткий исследовательский вывод для вставки в текст, его можно формулировать так:

> The prototype implements a grounded, workspace-centric mini-app generation platform that combines LLM-based synthesis with deterministic validation, draft-first patch application, revision-aware workspace management, and runtime preview. Although the repository still contains a formal AppIR layer and an earlier AppIR-to-template compilation path, the current operational architecture has evolved toward a more pragmatic planning-and-editing pipeline built around GroundedSpec, role contracts, page graphs, targeted file edits, and post-generation repair loops. This makes the prototype especially suitable for studying controlled generation, traceability, and iterative refinement under platform and template constraints.
