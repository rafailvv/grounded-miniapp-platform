# Grounded Mini-App Platform: максимально подробное описание устройства приложения

## 1. Что это за приложение в одном абзаце

Этот репозиторий реализует не просто "чат, который пишет код", а управляемую платформу генерации мини-приложений. Пользователь работает с workspace, добавляет документы, задаёт prompt, после чего система строит промежуточные артефакты (`GroundedSpec`, `AppIR`, отчёты, патчи, draft-версию кода), валидирует их, применяет изменения к каноническому шаблону mini-app, поднимает runtime preview и хранит историю ревизий. Ключевая идея архитектуры: генерация должна быть grounded, то есть опираться не только на prompt, но и на документацию, ограничения платформы и жёсткие валидаторы.

## 2. Главная архитектурная идея

Архитектура здесь строится вокруг контролируемого конвейера:

`workspace -> documents/code index -> retrieval -> GroundedSpec -> AppIR/plan -> patch/draft -> checks -> preview -> approve/apply/rollback/export`

Это значит:

- пользователь не пишет код напрямую в пустоту;
- LLM не является единственным источником истины;
- между prompt и кодом есть промежуточные формальные слои;
- перед применением есть блокирующие проверки;
- изменения живут как ревизии workspace и могут быть откатены;
- preview отражает не абстрактный макет, а реальный runtime шаблона.

## 3. Верхнеуровневая структура репозитория

### 3.1. Корневые зоны

- `contracts/`
  Здесь лежат версии контрактов для формальных промежуточных представлений, например `grounded-spec.v1.json` и `app-ir.v1.json`.

- `platform/backend/`
  Сервер платформы на FastAPI. Это главный orchestrator: API, сервисы, генерация, preview, state store, валидаторы, работа с workspace.

- `platform/frontend/`
  UI самой платформы на React/Vite. Это интерфейс, в котором пользователь создаёт workspace, запускает generate/fix run, смотрит артефакты, preview и логи.

- `runtime/templates/base-miniapp/`
  Канонический шаблон генерируемого mini-app. Именно в него компилируются/применяются изменения.

- `runtime/platform-docs/`
  Bundled-документация по платформам, например ограничения Telegram и MAX.

- `docker/`
  Инфраструктура верхнего уровня репозитория.

- `data/`
  Рабочие артефакты платформы: состояния, workspaces, экспорты и прочие runtime-данные.

### 3.2. Фактический смысл разделения

Разделение нужно, чтобы не смешивать:

- код самой платформы;
- код генерируемого runtime-приложения;
- формальные контракты;
- документационные источники;
- runtime-данные конкретных workspace.

Если бы всё это было смешано, система быстро потеряла бы traceability и контроль над тем, что является исходным шаблоном, а что является сгенерированным пользовательским результатом.

## 4. Основные архитектурные слои

### 4.1. Слой platform frontend

Основной файл: [platform/frontend/src/App.tsx](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/frontend/src/App.tsx)

Этот слой:

- показывает список workspace;
- позволяет создать workspace;
- запускает runs;
- переключает режимы `generate` и `fix`;
- показывает preview;
- показывает три роли одновременно: `client`, `specialist`, `manager`;
- получает артефакты run;
- показывает статусы, прогресс, ошибки, логи, диффы и проверки;
- умеет инициировать `approve`, `discard`, `rollback`, `stop`, `rebuild preview`.

То есть frontend платформы не является frontend генерируемого mini-app. Это отдельная административно-исследовательская оболочка.

### 4.2. API-слой platform backend

Точка входа: [platform/backend/app/main.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/main.py)

Backend создаёт FastAPI app, собирает service container и регистрирует роуты:

- auth;
- workspaces;
- documents;
- chat;
- generation;
- runs;
- validation;
- files;
- preview;
- export.

Это публичный orchestration API для всего жизненного цикла workspace.

### 4.3. Service layer

Сборка сервисов: [platform/backend/app/services/container.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/container.py)

Service layer отвечает за:

- хранение state;
- управление workspace;
- индексацию кода и документов;
- retrieval;
- генерацию;
- фиксы;
- preview runtime;
- проверки;
- экспорт;
- логи workspace.

Это главный бизнес-слой платформы.

### 4.4. Domain/model layer

Основные файлы:

- [platform/backend/app/models/common.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/models/common.py)
- [platform/backend/app/models/domain.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/models/domain.py)
- [platform/backend/app/models/grounded_spec.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/models/grounded_spec.py)
- [platform/backend/app/models/app_ir.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/models/app_ir.py)
- [platform/backend/app/models/artifacts.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/models/artifacts.py)

Этот слой задаёт строгие Pydantic-модели, через которые проходят состояния системы.

### 4.5. Validator layer

Файлы:

- [platform/backend/app/validators/suite.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/validators/suite.py)
- [platform/backend/app/validators/grounded_spec_validator.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/validators/grounded_spec_validator.py)
- [platform/backend/app/validators/app_ir_validator.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/validators/app_ir_validator.py)
- [platform/backend/app/validators/platform_validator.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/validators/platform_validator.py)
- [platform/backend/app/validators/build_validator.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/validators/build_validator.py)

Валидаторы нужны, чтобы LLM не мог напрямую продавить невалидную структуру в код.

### 4.6. Runtime template layer

Шаблон: `runtime/templates/base-miniapp`

Это каноническая целевая структура мини-приложения:

- FastAPI miniapp backend;
- статические страницы для трёх ролей;
- SQLite persistence для role profiles;
- docker-compose для preview;
- общая структура файлов, в которую безопасно встраиваются изменения.

Важно: эта архитектура template тоже фактически "создаётся" платформой на старте workspace lifecycle.

Пользователь не начинает с пустой директории. После `clone_template` в workspace появляется baseline application architecture, и дальнейшая генерация уже не строит app с нуля, а доращивает и перестраивает этот baseline.

Из чего состоит template architecture:

- `miniapp/app/main.py`
  FastAPI entrypoint, static mounting, root redirects, wiring базовых routers.
- `miniapp/app/routes/*`
  базовые backend routes и route aggregation.
- `miniapp/app/db.py`
  baseline persistence layer для miniapp runtime.
- `miniapp/app/schemas.py`
  shared schema/types layer для backend API.
- `miniapp/app/static/client/*`
  baseline client role surface.
- `miniapp/app/static/specialist/*`
  baseline specialist role surface.
- `miniapp/app/static/manager/*`
  baseline manager role surface.
- `miniapp/app/static/preview-bridge.js`
  bridge между generated UI и preview/runtime environment.
- `miniapp/requirements.txt`
  Python runtime dependencies miniapp.
- `docker/docker-compose.yml`
  preview/runtime orchestration baseline.
- `artifacts/`
  место для generated spec, graph, traceability и validation outputs.

То есть template здесь не просто "пример проекта", а заранее заданный execution envelope, внутри которого generation должен оставаться валидным.

## 5. Конфигурация и базовые окружения

Файл: [platform/backend/app/core/config.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/core/config.py)

Настройки определяют:

- `repo_root`
- `data_dir`
- `workspaces_dir`
- `exports_dir`
- `runtime_dir`
- `template_dir`
- `contracts_dir`
- `preview_base_url`
- `preview_runtime_mode`
- `preview_port_base`
- `preview_start_timeout_sec`
- OpenRouter/OpenAI-метаданные

Практический смысл:

- код платформы знает, где лежит шаблон;
- каждый workspace живёт на диске отдельно;
- preview runtime стартует на выделенном порту;
- export кладётся в отдельную директорию;
- backend использует одну и ту же корневую конфигурацию для всех сервисов.

## 6. Что такое workspace

Ключевая сущность: `WorkspaceRecord`

Workspace содержит:

- идентификатор;
- имя и описание;
- target platform;
- preview profile;
- путь на диске;
- факт clone template или нет;
- текущую ревизию;
- список ревизий;
- timestamps.

### 6.1. Что физически есть у workspace

При создании workspace появляется директория в `data/workspaces/<workspace_id>`.

Внутри него дальше обычно существуют:

- `source/` — основная рабочая копия шаблона, куда в итоге попадают применённые изменения;
- draft-копии для runs;
- сопутствующие файлы и артефакты, которые использует платформа.

### 6.2. Почему workspace важен

Workspace является границей:

- состояния генерации;
- документации;
- индекса;
- preview;
- ревизий;
- истории запусков;
- логов;
- экспортов.

Это значит, что платформа ориентирована не на единичный prompt, а на долговременную рабочую область.

## 7. Ревизии и жизненный цикл кода

Основной сервис: [platform/backend/app/services/workspace_service.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/workspace_service.py)

Workspace использует реальные git commits внутри `source/`.

Поддерживаемые источники ревизий:

- `template_clone`
- `manual_edit`
- `ai_patch`
- `reset`
- `rollback`

### 7.1. Что это даёт

- шаблон можно заново клонировать;
- ручные правки становятся first-class revision;
- AI-изменения можно применить после approve;
- можно откатить последнюю ревизию;
- можно revert-нуть конкретную применённую run-ревизию.

### 7.2. Что происходит при clone/reset/rollback

- `clone_template`
  Копируется канонический шаблон из `runtime/templates/base-miniapp` в `workspace/source`, инициализируется git, создаётся commit.

- `reset_workspace`
  Workspace заново копирует шаблон и фактически возвращается к baseline.

- `rollback_last_revision`
  Восстанавливается дерево из предыдущего commit, затем создаётся новая rollback/reset-ревизия.

- `revert_revision`
  Делается `git revert --no-edit` для последней применённой ревизии конкретного run.

## 8. Draft-механика

Это одна из ключевых вещей во всей архитектуре.

Во время run изменения обычно сначала идут не в основной `source`, а в draft-копию.

### 8.1. Что это значит

- платформа готовит draft-каталог для `run_id`;
- AI и патчи применяются туда;
- проверки выполняются по draft;
- preview может быть перестроен на draft;
- только после approve draft переносится в `source`.

### 8.2. Зачем это нужно

Это разделяет:

- candidate changes;
- approved changes.

Именно поэтому система поддерживает `manual_approve`-подход, а не обязана безусловно применять всё сразу.

## 9. Хранилище состояния

Файл: [platform/backend/app/repositories/state_store.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/repositories/state_store.py)

Сервисы используют file-backed state store, который хранит:

- workspaces;
- previews;
- documents;
- chat_turns;
- jobs;
- runs;
- reports;
- exports.

Это не полноценная внешняя БД платформы, а файловое хранилище состояния платформы.

## 10. Документы и grounded retrieval

Сервис: [platform/backend/app/services/document_intelligence.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/document_intelligence.py)

### 10.1. Какие бывают источники документов

`DocumentRecord.source_type` может быть:

- `project_doc`
- `openapi`
- `codebase`
- `platform_doc`
- `user_prompt`
- `assumption`

### 10.2. Что делает document service

- сохраняет документы;
- режет их на chunks;
- индексирует;
- умеет получать chunks;
- собирает релевантные ссылки (`DocRef`) под prompt;
- проверяет, что required corpora вообще существуют.

### 10.3. Откуда retrieval берёт контекст

Контекст может прийти из:

- пользовательских документов workspace;
- code index workspace;
- bundled docs шаблона;
- bundled platform docs;
- самого user prompt.

То есть prompt не живёт сам по себе, а добавляется как один из источников.

### 10.4. Как работает retrieval сейчас

Текущая реализация относительно простая:

- токенизация текста;
- счёт lexical overlap;
- сбор совпавших chunks;
- сортировка по relevance.

Это не самая сложная retrieval-система, но архитектурно она уже реализует принцип grounded generation.

## 11. Индексация кода и context pack

Связанные сервисы:

- `CodeIndexService`
- `ContextPackBuilder`

Их роль:

- проиндексировать актуальный codebase workspace;
- вытащить релевантные code snippets;
- сформировать context для LLM;
- стабилизировать prompt prefix и cache key.

Практический смысл:

- модель получает не только общий prompt, но и часть текущего кода;
- изменения учитывают уже существующие ручные модификации;
- система может повторно использовать контекст более последовательно.

## 12. LLM-слой

Файлы:

- [platform/backend/app/ai/openrouter_client.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/ai/openrouter_client.py)
- [platform/backend/app/ai/model_registry.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/ai/model_registry.py)
- prompts в `platform/backend/app/ai/prompts/`

### 12.1. Для чего LLM используется

LLM участвует не как "свободный кодогенератор", а как компонент нескольких фаз:

- анализ запроса;
- построение grounded spec;
- построение IR/role contract/planning;
- подготовка patch plan;
- repair/fix циклы;
- summary/trace-related артефакты.

### 12.2. Что важно архитектурно

- модель изолирована отдельным клиентом;
- orchestration-логика не размазана по API;
- есть разные model profiles и task profiles;
- есть structured output;
- есть явные prompt templates.

### 12.3. Что произойдёт, если LLM не настроен

В текущей логике generation pipeline может быть заблокирован как `llm_required`.

Это важный сценарный branch:

- если user запускает generation без нужной LLM-конфигурации;
- run не переходит к полноценной генерации;
- job помечается как blocked/failed с объяснением;
- пользователь должен сначала настроить API key.

## 13. Основные режимы работы системы

### 13.1. Режим `generate`

Система строит новое или обновлённое приложение на основании prompt и контекста.

### 13.2. Режим `fix`

Система пытается исправить неудачную предыдущую генерацию или build/runtime проблему.

### 13.3. Generation modes

Определены режимы:

- `fast`
- `balanced`
- `quality`
- `basic`

Каждый режим влияет на fidelity, глубину пайплайна и стратегию генерации.

## 14. Публичные API-возможности платформы

По коду backend доступны группы маршрутов:

### 14.1. Workspaces

- создать workspace;
- получить список;
- получить один workspace;
- клонировать шаблон;
- reset workspace;
- rollback workspace;
- проиндексировать workspace;
- узнать статус индекса;
- удалить workspace.

### 14.2. Documents

- загрузить документ;
- перечислить документы;
- проиндексировать документ;
- получить chunks.

### 14.3. Generation/jobs

- стартовать generate-job;
- получить job;
- получить события job;
- retry.

### 14.4. Runs

- создать run;
- перечислить runs;
- получить run;
- получить артефакты run;
- получить итерации;
- получить checks;
- получить patch;
- approve/apply;
- discard;
- stop;
- rollback.

### 14.5. Files

- получить дерево файлов;
- получить содержимое файла;
- сохранить файл;
- получить diff.

### 14.6. Preview

- start preview;
- ensure preview;
- rebuild preview;
- reset preview;
- получить preview url;
- получить preview logs;
- получить расширенные workspace logs;
- получить fallback HTML preview.

### 14.7. Validation

- получить текущий spec;
- получить текущий IR;
- получить validation snapshot;
- assumptions;
- traceability;
- вручную запустить validation.

### 14.8. Export

- zip export;
- git patch export;
- download export.

## 15. Основной pipeline генерации по шагам

Ключевой сервис: [platform/backend/app/services/generation_service.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/generation_service.py)

Ниже описан типовой жизненный цикл.

### 15.1. Шаг 1. Принятие запроса

При старте generate:

- создаётся `JobRecord`;
- сохраняются mode, generation_mode, target platform, preview profile;
- вычисляется effective prompt;
- определяется role scope;
- инициализируются trace и события.

### 15.2. Шаг 2. Индексация workspace

Перед генерацией система обновляет индекс codebase.

Зачем:

- чтобы retrieval и planning работали по актуальному состоянию кода;
- чтобы учесть ручные edits;
- чтобы fix mode видел текущее содержимое.

### 15.3. Шаг 3. Проверка preflight

Система проверяет:

- есть ли обязательные corpora;
- клонирован ли template;
- включён ли LLM.

Если любой из этих пунктов не выполняется, generation блокируется до следующего шага.

### 15.4. Шаг 4. Retrieval

Собираются релевантные фрагменты:

- docs workspace;
- codebase;
- template docs;
- platform docs;
- prompt.

### 15.5. Шаг 5. Построение GroundedSpec

Система пытается получить структурированное описание:

- product goal;
- actors;
- flows;
- API requirements;
- UI requirements;
- persistence;
- integrations;
- assumptions;
- contradictions;
- unknowns;
- evidence links.

### 15.6. Шаг 6. Валидация GroundedSpec

Если в spec есть проблемы, дальше возможны ветвления:

- spec валиден и pipeline идёт дальше;
- spec частично сомнителен, но неблокирующ;
- spec содержит blocking contradictions/unknowns и генерация останавливается.

### 15.7. Шаг 7. Подготовка draft

Под run создаётся draft workspace.

### 15.8. Шаг 8. Role contract / planning / AppIR

Система переводит grounded intent в более прикладную форму:

- маршруты;
- роли;
- экраны;
- компоненты;
- actions;
- data bindings;
- integrations;
- security policy;
- transitions;
- variables;
- telemetry hooks;
- validator rules;
- traceability links.

### 15.9. Шаг 9. Валидация AppIR и platform constraints

Проверяется:

- логическая целостность IR;
- соответствие platform constraints;
- допустимость источников данных;
- допустимость auth/session flows;
- ссылки на существующие сущности.

### 15.10. Шаг 10. Patch planning и editing

Система решает:

- какие файлы менять;
- менять минимальным patch или whole-file build;
- как распределить изменения по ролям и backend;
- как вписаться в канонические директории.

### 15.11. Шаг 11. Применение в draft

Изменения попадают в draft workspace.

### 15.12. Шаг 12. Checks

После изменения запускаются:

- build validators;
- connectivity validators;
- static compile/build checks;
- preview boot smoke;
- preview connectivity smoke.

### 15.13. Шаг 13. Preview

Если checks допустимы, draft или source preview может быть перестроен и пользователь увидит результат.

### 15.14. Шаг 14. Approval/apply

Если стратегия применения ручная:

- run переходит в `awaiting_approval`;
- пользователь смотрит diff/preview/artifacts;
- затем approve или discard.

Если применяется автоматически:

- draft может быть сразу утверждён;
- ревизия записывается в source.

### 15.15. Шаг 15. Артефакты и отчёты

Сохраняются:

- trace;
- validation;
- assumptions;
- traceability;
- candidate diff;
- check results;
- iterations;
- fix runtime artifacts;
- patch payload;
- run artifacts.

## 16. Что такое "ветки" поведения системы

Ниже под "ветками" понимаются не git branches, а сценарные branch-пути внутри продукта.

## 17. Ветка 1. Пользователь только создал workspace

### 17.1. Что уже можно

- хранить метаданные workspace;
- видеть его в списке;
- прикреплять документы.

### 17.2. Что ещё нельзя полноценно

- генерировать, если template не клонирован;
- поднимать полноценный preview без рабочей копии шаблона;
- рассчитывать на code retrieval, если source ещё не готов.

### 17.3. Что будет при generate слишком рано

Generation likely упрётся в preflight:

- template not cloned;
- missing corpora;
- дальше job будет blocked/failed.

## 18. Ветка 2. Workspace создан и шаблон клонирован

После clone-template:

- в workspace появляется baseline код;
- в workspace появляется baseline template architecture;
- создаётся git revision;
- запускается индексация;
- preview может быть поднят асинхронно.

Это базовое "исходное рабочее" состояние.

Под baseline template architecture здесь понимается не один стартовый файл, а целостный skeleton miniapp:

- backend entrypoint и router wiring;
- role-based static surfaces;
- базовые schemas и persistence;
- preview/runtime orchestration;
- артефактные директории для generated outputs.

Поэтому дальнейшая генерация обычно делает одно из двух:

- локально достраивает существующие части template;
- расширяет template новыми role/domain pages и backend routes, не выходя за canonical roots.

## 19. Ветка 3. Пользователь добавил документы

### 19.1. Если документы релевантны и индексированы

Система сможет строить grounded spec, более уверенно выбирать flows и данные.

### 19.2. Если документы загружены, но не индексированы

Система частично всё равно может использовать документ, но индексная составляющая будет хуже. Обычно ожидается индексирование.

### 19.3. Если документы противоречат prompt

Тогда возможны две ветки:

- противоречие попадёт в `contradictions`, валидатор заблокирует дальнейшую генерацию;
- противоречие будет отмечено как assumption/unknown и потребует уточнения или осторожной генерации.

## 20. Ветка 4. Prompt очень простой

Примеры по смыслу:

- "сделай экран профиля";
- "добавь форму заявки";
- "измени подписи на странице manager".

Что обычно происходит:

- retrieval относительно лёгкий;
- role scope может быть узким;
- план изменений малый;
- patch скорее всего точечный;
- checks проходят быстрее;
- шанс auto-fix ниже, потому что изменений меньше.

## 21. Ветка 5. Prompt большой и продуктовый

Примеры по смыслу:

- "сделай полноценный маркетплейс";
- "добавь каталог, корзину, заказы, роли и админку";
- "реализуй workflow бронирования с несколькими состояниями".

Что меняется:

- retrieval становится шире;
- требуется больше экранов, маршрутов, сущностей;
- план затрагивает больше файлов;
- вероятность platform/build конфликтов растёт;
- возрастает шанс, что система будет вынуждена расширять scope и делать repair iterations;
- существенно важнее корректность spec и IR.

## 22. Ветка 6. Prompt касается одной роли

Например:

- только `client`;
- только `manager`;
- только `specialist`.

Тогда возможен узкий `target_role_scope`.

Что это даёт:

- меньше изменений;
- меньше фронтовых страниц для патча;
- меньше риск сломать соседние роли;
- preview всё равно остаётся трёхрольным, но основная изменяемая область уже.

## 23. Ветка 7. Prompt затрагивает все роли

Примеры:

- "client создаёт заявку, specialist обрабатывает, manager подтверждает";
- "перестрой весь workflow работы по ролям".

Тогда:

- меняется несколько экранов;
- появляются межролевые переходы;
- выше требования к consistency;
- AppIR становится центральным элементом, потому что он связывает роли, экраны, данные и действия.

## 24. Ветка 8. Prompt просит только UI-изменение

Например:

- поменять текст;
- поменять layout;
- добавить блок на страницу;
- обновить стили роли.

Ожидаемое поведение:

- меньше backend-изменений;
- patch идёт в статические файлы `miniapp/app/static/...`;
- build-validator главным образом контролирует допустимость структуры;
- runtime чаще остаётся живым;
- preview быстро отражает результат.

Риски:

- если prompt фактически требует данных или API, но сформулирован как UI-only, система может ошибочно недооценить backend-изменения.

## 25. Ветка 9. Prompt просит данные, бизнес-логику и backend

Например:

- новая сущность;
- новые API;
- новые поля формы;
- сохранение состояния;
- workflow обработки.

Ожидаемые последствия:

- затрагиваются backend-файлы шаблона;
- меняются schemas/routes/db;
- нужно согласовать frontend и backend;
- checks становятся критичнее;
- preview может падать из-за runtime/backend ошибок.

## 26. Ветка 10. Prompt противоречит шаблону

Например:

- пользователь просит отдельный React frontend внутри runtime, а шаблон диктует static-role pages;
- просит другую архитектуру директорий;
- просит убрать три роли и заменить полностью другой моделью;
- просит слой, который противоречит canonical roots.

Как это должно ветвиться:

- система старается встроить идею в существующий template;
- если запрос ломает каноническую структуру, platform/build validators могут заблокировать или ограничить план;
- generation service ориентирована на canonical file roots и знает legacy architecture markers, которых не надо плодить.

Это очень важная архитектурная развилка: система не обязана исполнять любой prompt буквально, если это разрушает канонический runtime.

## 27. Ветка 11. Prompt неполный

Например:

- "сделай CRM";
- "добавь удобный workflow";
- "надо улучшить experience".

Возможны варианты:

- система формирует assumptions;
- часть требований переходит в unknowns;
- если unknowns не критичны, generation идёт дальше;
- если unknowns high-impact, валидатор может блокировать.

То есть неполный prompt не всегда фатален, но может стать blocking, если неполнота касается основы продукта.

## 28. Ветка 12. Prompt детальный и согласованный

Это лучший сценарий.

Если в prompt:

- есть роли;
- есть данные;
- есть целевые экраны;
- есть flow;
- есть платформа;
- есть ограничения;

то pipeline с высокой вероятностью:

- строит качественный GroundedSpec;
- формирует валидный AppIR;
- создаёт более точный patch plan;
- проходит checks с меньшим числом repair cycles.

## 29. Ветка 13. Документы есть, но они устаревшие

В этом случае возможна опасная ветка:

- retrieval честно подтянет старые требования;
- prompt может говорить одно, docs другое;
- contradictions возрастут;
- LLM может выдать spec со следами обоих источников;
- валидатор либо остановит, либо вынудит систему описать assumptions.

Именно поэтому документ-основанная генерация не упрощает задачу, а делает конфликт явным.

## 30. Ветка 14. Документов нет, но prompt хороший

Система всё равно хочет обязательные corpora:

- template docs;
- platform docs;
- prompt source.

Если только пользовательские docs отсутствуют, generation всё ещё может быть возможна.

Если отсутствуют обязательные bundled docs или template docs, pipeline должен блокироваться.

## 31. Ветка 15. Пользователь делает manual edits до генерации

Это нормальный сценарий.

Что происходит:

- manual edit создаёт отдельную git revision;
- code index потом увидит эти изменения;
- генерация будет происходить поверх нового состояния;
- retrieval и patch planning смогут опираться на ручные правки.

Это одно из главных достоинств текущей архитектуры.

## 32. Ветка 16. Пользователь делает manual edits после AI-генерации

Возможные сценарии:

- пользователь исправляет что-то в source;
- создаётся ревизия `manual_edit`;
- следующая AI-генерация должна учитывать новый baseline;
- diff строится относительно актуального source;
- ручные изменения не должны стираться слепым full rewrite.

## 33. Ветка 17. Run идёт с auto apply

Если стратегия применения автоматическая:

- draft после успешных checks может быть сразу применён;
- создаётся `ai_patch` revision;
- source обновляется автоматически;
- preview перестраивается уже для source.

Плюсы:

- быстро.

Минусы:

- меньше человеческого контроля;
- выше риск применить нежелательные изменения.

## 34. Ветка 18. Run идёт с manual approve

Это более исследовательски и продуктово безопасный сценарий.

Последовательность:

- готовится draft;
- строится preview;
- показывается diff;
- run переходит в `awaiting_approval`;
- пользователь либо approve, либо discard.

### 34.1. Если approve

- draft переносится в source;
- создаётся `ai_patch` revision;
- run становится `completed/applied`.

### 34.2. Если discard

- draft удаляется;
- run маркируется как discarded/failed apply;
- source не меняется.

## 35. Ветка 19. Checks успешно прошли

Тогда:

- validator issues нет или они неблокирующие;
- connectivity issues нет или они неблокирующие;
- static checks passed;
- preview boot smoke не выявил критики;
- preview connectivity smoke не выявил критики;
- run может быть завершён или отправлен на approve.

## 36. Ветка 20. Валидаторы не прошли

Если `GroundedSpec` или `AppIR` невалидны:

- pipeline не должен бездумно идти в build;
- job получает validation-related failure;
- в reports записываются issues;
- пользователь видит, где именно заблокировалось.

Это "хороший отказ", потому что система останавливается раньше, чем сломает runtime.

## 37. Ветка 21. Build validators прошли, но static build упал

Например:

- синтаксическая ошибка;
- frontend build failure;
- backend compile failure;
- tooling/runtime misconfiguration.

Тогда:

- run checks содержат failed result;
- failure классифицируется;
- возможен auto-fix или explicit fix flow.

## 38. Ветка 22. Preview не поднялся

Возможные причины:

- docker compose недоступен;
- контейнеры не стартовали;
- health endpoint не отвечает;
- runtime build прошёл, но app не стал healthy;
- порт занят;
- в контейнере отсутствуют зависимости;
- backend упал на startup.

В этом случае preview service переведёт preview в `error` или `starting/health_check`, сохранит `last_error` и логи.

## 39. Ветка 23. Preview раньше работал, потом стал stale

PreviewService умеет:

- reconciling runtime state;
- обнаруживать stale starting preview;
- перезапускать ensure flow;
- скрывать публичную готовность, если HTTP-пробы `/health` и `/client` не проходят.

Это означает, что "URL есть" не равно "preview действительно ready".

## 40. Ветка 24. Run запрошен на остановку

RunService хранит `run_stop_request`.

Если пользователь нажал stop:

- run переводится в `stopping`;
- генерация периодически проверяет should_stop;
- при корректной обработке пайплайн останавливается безопасно.

## 41. Ветка 25. Generate run провалился, и запускается fix

Это одна из самых интересных сценарных веток.

Если generate-run неудачен:

- есть `failure_reason`;
- есть `error_context`;
- может быть `handoff_from_failed_generate`;
- запускается `fix` run;
- fix orchestrator использует ошибку как исходный материал для исправления.

## 42. Ветка 26. Auto-fix после failed generate

По коду RunService умеет автоматически переключиться в fix mode, если generate-run упал на build/runtime проблеме.

Сценарий:

- generation завершился с ошибкой;
- система классифицировала failure;
- понимает, что это repairable build/runtime issue;
- автоматически запускает fix orchestrator;
- пытается починить без ручного старта отдельного fix run.

## 43. Ветка 27. Resume from checkpoint

Система поддерживает resume:

- можно повторно использовать сохранённые planning artifacts;
- можно переиспользовать grounded spec, role contract и plan result;
- можно клонировать draft от предыдущего run и продолжать не с нуля.

Это полезно, когда:

- generation был прерван;
- пользователю нужен повтор с тем же планом;
- уже был построен дорогой planning stage, и нет смысла заново делать retrieval/spec/planning.

## 44. Ветка 28. Fix запущен на основе build-ошибки

Тогда fix-flow обычно:

- получает raw error;
- определяет source ошибки: build, preview, miniapp, frontend, runtime;
- выбирает failing target;
- сужает или расширяет область фикса;
- вносит repair iteration;
- повторяет checks.

## 45. Ветка 29. Fix запущен на основе preview/runtime-ошибки

Например:

- контейнеры поднялись, но app unhealthy;
- backend импорт не найден;
- startup traceback;
- проблема в runtime конфигурации.

Тогда fix-flow отличается от чисто фронтового build fix:

- сильнее важны container logs;
- важнее backend/runtime файлы;
- preview boot smoke становится только liveness-индикатором;
- preview connectivity smoke и connectivity validators становятся ключевыми индикаторами того, что generated routes действительно живы и связаны.

## 46. Ветка 30. Система расширяет scope фикса

Иногда ошибка проявляется в одном месте, а причина глубже.

Тогда возможна ветка:

- начальный fix был узким;
- checks снова упали;
- система делает `scope_expanded`;
- начинает править не только один файл, а связанный набор файлов.

Это типичная архитектурная реальность: многие ошибки межслойные.

## 47. Ветка 31. Система обнаруживает повторяющуюся сигнатуру ошибки

По событиям видно, что repair flow умеет распознавать repeated signature и abort.

Зачем:

- чтобы не войти в бесконечный цикл "починил, снова сломал так же";
- чтобы остановиться и зафиксировать, что автоматический repair не продвигается.

Это важная защитная ветка.

## 48. Ветка 32. Пользователь откатывает успешный run

Если applied run оказался нежелательным:

- можно вызвать rollback run;
- система делает revert соответствующей ревизии;
- run помечается как rolled_back;
- preview перестраивается.

Это не удаление истории, а аккуратное обратное изменение.

## 49. Ветка 33. Пользователь откатывает workspace целиком

Отдельно от rollback конкретного run есть workspace-level rollback/reset.

Это более широкие сценарии:

- вернуться к предыдущему revision workspace;
- полностью reset-нуться к каноническому template.

## 50. Ветка 34. Пользователь хочет только просмотреть результат, не применять

С manual approve это естественно:

- generate run создаёт draft;
- пользователь получает diff и preview;
- решение о применении можно отложить;
- итоговый source остаётся нетронутым.

## 51. Ветка 35. Пользователь использует platform UI как IDE

Через file APIs можно:

- читать дерево файлов;
- смотреть contents;
- сохранять файл;
- получать diff.

Тогда платформа становится не только генератором, но и редактором/оболочкой над workspace.

## 52. Ветка 36. Prompt просит архитектурный рефакторинг

Например:

- вынести общие части;
- изменить routing;
- перестроить слои miniapp;
- переделать persistence.

Это один из самых рискованных типов запросов.

Почему:

- рефакторинг менее "локален", чем новая форма;
- можно разрушить канонические допущения template;
- validation, build и даже boot-level preview могут пройти, но реальные page-to-API связи останутся неполными;
- manual approval тут особенно важен.

## 53. Ветка 37. Prompt просит то, чего template пока прямо не поддерживает

Например:

- новую роль вне `client/specialist/manager`;
- нестандартную auth-модель;
- параллельный frontend framework;
- совершенно иную runtime-топологию.

Возможные исходы:

- система частично адаптирует запрос в рамках текущего шаблона;
- пометит часть требований как assumptions/unknowns;
- заблокирует генерацию как несовместимую с template/platform constraints.

## 54. Ветка 38. Prompt ориентирован на Telegram

Для Telegram важны:

- platform docs из `runtime/platform-docs/telegram`;
- preview profile `telegram_mock`;
- platform validator для соответствующих ограничений;
- корректные auth/session assumptions.

Если IR нарушает Telegram-ограничения, platform validator должен это поймать.

## 55. Ветка 39. Prompt ориентирован на MAX

Аналогично Telegram, но с corpora и ограничениями MAX.

То есть target platform меняет не только label, но и набор обязательного контекста и validator rules.

## 56. Ветка 40. Пользователь просит только preview

Через preview API можно:

- `start`;
- `ensure`;
- `rebuild`;
- `reset`.

Смысл развилок:

- `start/ensure`
  Если preview ещё нет или он stale, система поднимает или восстанавливает runtime.

- `rebuild`
  Когда код уже изменился и нужен новый контейнерный runtime.

- `reset`
  Когда нужно полностью остановить preview с очисткой сессии runtime.

## 57. Ветка 41. Preview строится по source

Это базовый режим:

- пользователь смотрит применённое состояние;
- URL стабилен для рабочего workspace.

## 58. Ветка 42. Preview строится по draft

Это более продвинутый исследовательский сценарий:

- пользователь видит ещё не применённые изменения;
- можно сравнить candidate state до approve;
- `draft_run_id` становится частью preview state.

## 59. Ветка 43. Export результатов

Пользователь может не только смотреть preview, но и экспортировать:

- zip;
- git patch.

Это полезно, если:

- нужно забрать итог наружу;
- нужно отправить diff;
- нужно отдельно анализировать результаты.

## 60. Ветка 44. Пользователь получает логический success, но UX-результат слабый

Это важная неошибочная ветка.

То есть:

- checks passed;
- preview работает;
- код валиден;
- но продуктовая интерпретация prompt слабая.

Тогда формально pipeline успешен, но по сути нужен следующий run или более точный prompt. Архитектура тут помогает тем, что у пользователя есть:

- traceability;
- assumptions;
- diff;
- preview;
- возможность сделать следующий итеративный run.

## 61. Ветка 45. Пользователь формулирует противоречивый запрос

Например:

- "сделай минимально и очень функционально";
- "ничего не меняй в архитектуре, но полностью перестрой архитектуру";
- "добавь backend без изменения backend".

Система в таких случаях должна:

- зафиксировать contradictions;
- выделить assumptions;
- по возможности выбрать безопасную интерпретацию;
- заблокировать дальше, если противоречие критично.

## 62. Ветка 46. Prompt слишком широкий для одного шага

Например:

- "с нуля сделай полноценную экосистему сервиса, CRM, аналитику, интеграции, платежи, роли и админку".

Практически это ведёт к:

- большому IR;
- большому объёму patch plan;
- высокому риску частичной неконсистентности;
- вероятности, что нужен итеративный подход через несколько runs.

Архитектурно платформа это выдерживает лучше, чем "LLM сразу пишет весь код", но цена всё равно растёт.

## 63. Ветка 47. Prompt очень узкий и безопасный

Например:

- поменять copy;
- исправить label;
- добавить поле в profile page.

Это почти идеальный candidate для controlled patching:

- маленький diff;
- быстрая проверка;
- низкий риск;
- хороший preview feedback.

## 64. Ветка 48. Пользователь сначала генерирует, потом вручную правит, потом снова генерирует

Это один из ключевых поддерживаемых циклов:

1. baseline template;
2. AI generation;
3. manual edit;
4. re-index;
5. next generation on top.

Архитектура workspace + revisions + code index специально для этого и создана.

## 65. Ветка 49. Пользователь использует chat turns как контекст

Есть отдельные chat routes и `ChatTurnRecord`.

Это значит, что платформа может хранить диалоговый контекст workspace:

- user turns;
- assistant turns;
- summaries;
- привязку к job/run.

Это не основной pipeline-код, но это важный контекстовый слой.

## 66. Ветка 50. Система не может безопасно определить правильный путь

Это один из самых зрелых сценариев для такой архитектуры.

Лучшее поведение системы в этом случае не "угадать любой ценой", а:

- сформировать assumptions;
- вывести unknowns;
- заблокировать критический шаг;
- оставить след в артефактах;
- дать пользователю наблюдаемую причину остановки.

## 67. Runtime шаблон miniapp

Основные файлы:

- [runtime/templates/base-miniapp/miniapp/app/main.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/main.py)
- [runtime/templates/base-miniapp/miniapp/app/db.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/db.py)
- [runtime/templates/base-miniapp/miniapp/app/schemas.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/schemas.py)
- [runtime/templates/base-miniapp/miniapp/app/routes/profiles.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/runtime/templates/base-miniapp/miniapp/app/routes/profiles.py)

### 67.1. Как устроен runtime

Miniapp runtime:

- поднимается на FastAPI;
- создаёт БД на startup;
- обслуживает API профилей;
- раздаёт статические страницы для ролей;
- редиректит `/` в `/client`.

### 67.2. Какие роли зашиты сейчас

- `client`
- `specialist`
- `manager`

### 67.3. Почему это важно

Это не абстракция. Вся platform preview UI, planning логика и canonical roots уже опираются на эту трёхрольную структуру.

## 68. Как устроен frontend runtime miniapp

Статика лежит в:

- `miniapp/app/static/client/*`
- `miniapp/app/static/specialist/*`
- `miniapp/app/static/manager/*`
- `miniapp/app/static/preview-bridge.js`

Это означает:

- нет отдельного независимого frontend build subproject внутри шаблона по умолчанию;
- каждая роль представлена своими HTML/CSS/JS страницами;
- generated/apply pipeline работает по реальным файлам шаблона.

## 69. Как устроен preview

Связанные файлы:

- [platform/backend/app/services/preview_service.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/preview_service.py)
- [platform/backend/app/services/runtime_manager.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/runtime_manager.py)

### 69.1. Preview service отвечает за состояние preview

Он хранит:

- status;
- stage;
- progress;
- url;
- runtime_mode;
- draft_run_id;
- logs;
- last_error;
- latency.

### 69.2. Runtime manager отвечает за фактический запуск

Он:

- выбирает режим `docker` или `inline`;
- выделяет порт;
- рендерит compose file под host path;
- делает `docker compose up -d --build`;
- ждёт health;
- собирает logs;
- умеет `rebuild` и `reset`.

### 69.3. Какие сценарии статусов preview есть

- `stopped`
- `starting`
- `running`
- `error`

Стадии:

- `idle`
- `starting`
- `rebuilding`
- `health_check`
- `running`
- `error`

## 70. Что именно валидируется

ValidationSuite объединяет:

- grounded spec validation;
- app ir validation;
- platform validation;
- build validation;
- connectivity validation.

### 70.1. GroundedSpec validation

Смотрит на:

- полноту spec;
- contradictions;
- unknowns;
- корректность структуры.

### 70.2. AppIR validation

Смотрит на:

- целостность ссылок;
- существование routes/screens/entities;
- допустимость data flow;
- корректность transitions.

### 70.3. Platform validation

Смотрит на:

- допустимость platform-specific сценариев;
- auth/session rules;
- ограничения trusted/user-supplied data;
- другие платформенные запреты.

### 70.4. Build validation

Смотрит на:

- допустимость результирующего workspace path structure;
- согласованность build/runtime layout;
- архитектурные инварианты шаблона.

Build validation больше не считается достаточным доказательством, что сгенерированный mini app "подключен".

Он подтверждает structural correctness, но не гарантирует:

- что page-level `data_dependencies` реально доведены до backend routes;
- что UI не ограничился статическим placeholder text;
- что loading/error states, задуманные на этапе planning, реально присутствуют в сгенерированном page surface.

### 70.5. Connectivity validation

Connectivity validation добавлен как отдельный blocking layer между structural validation и preview acceptance.

Его задача: подтвердить, что generation pipeline не только создал корректный workspace, но и собрал внутренне связанное mini app behavior.

Source of truth для connectivity contract:

- `grounded_spec.api_requirements`;
- `page_graph.roles[].pages[].data_dependencies`;
- planned/selected target files;
- реально сгенерированные file operations.

На этой основе система строит implicit contract вида:

- какая страница заявляет динамическую зависимость;
- какой backend route/module должен существовать для этой зависимости;
- какие UI signals должны присутствовать на странице;
- есть ли в draft реальные wiring markers, а не только copy.

Connectivity validator блокирует draft, если обнаруживает:

- `connectivity.missing_backend_route`
- `connectivity.unwired_page_dependency`
- `connectivity.missing_ui_loading_state`
- `connectivity.missing_ui_error_state`
- `connectivity.placeholder_dynamic_page`
- `connectivity.preview_route_unreachable`

## 71. Проверки после редактирования

Файл: [platform/backend/app/services/check_runner.py](/Users/rafailvv/PycharmProjects/grounded-miniapp-platform/platform/backend/app/services/check_runner.py)

CheckRunner запускает пять классов проверок:

- `schema_validators`
- `connectivity_validators`
- `changed_files_static`
- `preview_boot_smoke`
- `preview_connectivity_smoke`

### 71.1. `schema_validators`

Исполняет build validators по draft workspace.

### 71.2. `connectivity_validators`

Исполняет отдельный connectivity validator по draft workspace.

Это уже не structural check, а content-and-wiring check.

Validator смотрит:

- имеет ли страница с `data_dependencies` соответствующий backend route/module;
- есть ли на page surface реальный request/API/submit wiring;
- не потерялись ли planned loading/error states;
- не осталась ли dynamic page фактически статическим placeholder;
- соответствуют ли frontend `/api/...` references реально существующим `miniapp/app/routes/*.py`;
- подтвержден ли planner-expanded contract gap после composition, а не только добавлен в target list.

### 71.3. `changed_files_static`

Пытается:

- собрать frontend, если есть `frontend/package.json`;
- проверить backend compile/структуру, если есть `miniapp/app`.

### 71.4. `preview_boot_smoke`

Не всегда реально стартует новый runtime прямо в checks, но фиксирует состояние текущего preview и решает, можно ли считать preview smoke пройденным или пропущенным.

Это liveness-level smoke, а не proof of functional connectivity.

### 71.5. `preview_connectivity_smoke`

Это лёгкий route-level smoke поверх уже running preview.

Он не пытается исполнять полный business flow, но проверяет, что generated root role routes:

- реально открываются через текущий preview session;
- не возвращают 404/empty/unusable content;
- дают минимально пригодный HTML surface вместо "preview жив, но route broken".

Таким образом платформа теперь различает:

- runtime is alive;
- generated routes are reachable;
- page/API wiring structurally and semantically connected.

## 72. Почему здесь важны assumptions, traceability и reports

Платформа хранит не только код и status, но и объясняющие артефакты:

- assumptions;
- traceability;
- validation snapshot;
- candidate diff;
- iterations;
- fix attempts;
- scope expansions;
- event trace.

Это нужно, чтобы пользователь мог ответить на вопросы:

- почему система так интерпретировала prompt;
- откуда взялась конкретная функциональность;
- почему generation заблокировался;
- почему preview не стартовал;
- почему fix полез в дополнительные файлы;
- что именно изменил AI.

## 73. Что используется по технологиям

### 73.1. Backend platform

- Python
- FastAPI
- Pydantic
- file-backed state store
- subprocess/git/docker orchestration

### 73.2. Frontend platform

- React
- TypeScript
- Vite

### 73.3. Runtime miniapp

- FastAPI
- SQLAlchemy/SQLite-подобная persistence схема для профилей
- статические HTML/CSS/JS страницы
- Docker Compose preview

### 73.4. AI/generation

- OpenRouter/OpenAI routed structured generation
- prompt templates
- typed intermediate artifacts

## 74. Что в системе является каноничным, а что переменным

### 74.1. Каноничное

- template structure;
- три роли;
- workspace model;
- typed contracts;
- validator gates;
- draft/apply workflow;
- preview через runtime.

### 74.2. Переменное

- prompt;
- docs;
- platform target;
- model profile;
- generation mode;
- scope изменений;
- итоговый patch;
- наличие approve или auto-apply.

## 75. Что является сильной стороной архитектуры

- generation grounded, а не полностью галлюцинаторная;
- есть промежуточные формальные слои;
- manual edits не теряются;
- есть git-based revision history;
- есть draft before apply;
- есть real preview runtime;
- есть fix/retry/resume ветки;
- есть богатые артефакты для анализа.

## 76. Что является ограничением текущей архитектуры

- шаблон пока жёстко ориентирован на 3-role baseline;
- retrieval пока в основном lexical/simple;
- template architecture ограничивает типы допустимых запросов;
- большие product prompts могут быть слишком широкими для одного прогона;
- preview сильно зависит от docker/runtime окружения;
- фактическая успешность сильно опирается на корректность prompt и на совместимость с canonical template.

## 77. Практическая карта "если вот так, то что будет"

### 77.1. Если prompt маленький и конкретный

С высокой вероятностью будет маленький patch, быстрый preview и понятный diff.

### 77.2. Если prompt большой и туманный

Будет больше assumptions, выше риск blocking unknowns и больше repair cycles.

### 77.3. Если документы хорошие

Spec и planning становятся устойчивее.

### 77.4. Если документы конфликтуют

Появятся contradictions и возможный блок.

### 77.5. Если шаблон не клонирован

Generation блокируется на preflight.

### 77.6. Если LLM не настроен

Generation блокируется как `llm_required`.

### 77.7. Если build сломался

Запустится failure path и, возможно, fix flow.

### 77.8. Если preview сломался

Preview уйдёт в `error` или stuck `health_check`, а пользователь увидит логи и сможет rebuild/reset/fix.

### 77.9. Если пользователь хочет контроль

Используется manual approve через draft.

### 77.10. Если пользователь хочет скорость

Подходит auto-apply path, но риск выше.

### 77.11. Если AI сгенерировал неудачно, но не критично

Можно сделать следующий run поверх текущего состояния без уничтожения manual edits.

### 77.12. Если AI сгенерировал нежелательное, но уже применённое

Можно rollback/revert revision.

## 78. Реальное текущее положение дел по "вариантам" в этом репозитории

На основании просмотренного кода можно уверенно сказать:

- система проектировалась как pipeline-driven generator;
- главные ветвления уже зашиты в модели `JobRecord`, `RunRecord`, `PreviewRecord`, `RevisionRecord`;
- архитектура уже поддерживает не только happy path, но и stop/fix/resume/discard/rollback/manual-approve/stale-preview/error-runtime сценарии;
- центральный инвариант всей системы: любые изменения должны уважать канонический miniapp template и быть наблюдаемыми через артефакты и ревизии.

## 79. Итоговое резюме

Если описать приложение совсем коротко, то это workspace-centric генератор mini-app, который:

- принимает prompt и документы;
- извлекает grounded context;
- строит typed intermediate artifacts;
- валидирует их;
- генерирует контролируемые patch-изменения в шаблон;
- поднимает runtime preview;
- хранит ревизии и артефакты;
- умеет чинить, останавливать, откатывать и переиспользовать промежуточные результаты.

Если описать ещё точнее, то самое важное в нём не "что он генерирует код", а "как именно он контролирует ветвления между идеей пользователя, ограничениями платформы, каноническим runtime-шаблоном и безопасным применением изменений".
