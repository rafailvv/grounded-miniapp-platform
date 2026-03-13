# Unified Architecture Mini App

Шаблон Telegram Mini App на `React + TypeScript + Vite` с единой архитектурой и ролевым разделением интерфейса.

Проект рассчитан на 3 роли:
- `client`
- `specialist`
- `manager`

Сейчас это рабочая база для старта продукта: есть bootstrap, авторизация через Telegram initData, общая UI-архитектура, ролевые роуты, профиль пользователя, интеграция с Telegram WebApp API и инструменты для локальной разработки без Telegram.

## Что уже реализовано

- Единый bootstrap приложения (`src/app/bootstrap/useAppBootstrap.ts`):
  - получает данные Telegram WebApp,
  - при наличии `VITE_AUTH_ENDPOINT` отправляет initData на backend,
  - сохраняет токены,
  - определяет активную роль,
  - переводит приложение в `ready/error` состояние.
- Ролевой роутинг (`src/app/routing/RoleRouter.tsx`):
  - для каждой роли подключается отдельный route-модуль,
  - внутри роли есть `/` (кабинет) и `/profile` (редактирование профиля).
- Единый шаблон профиля для всех ролей (`src/shared/ui/templates/RoleProfileEditorPage.tsx`):
  - одинаковый UI,
  - одинаковая валидация,
  - одинаковое поведение кнопки сохранения (`Сохранение...` -> `Сохранено`).
- Профиль и хранилище (`src/shared/profile/clientProfile.ts`):
  - чтение/запись из `localStorage`,
  - попытка синхронизации с `Telegram.WebApp.DeviceStorage` (если доступно),
  - подмешивание Telegram user данных (имя/фото) как fallback.
- Интеграция с Telegram WebApp API (`src/shared/telegram/*`):
  - `BackButton`,
  - haptic feedback,
  - `ready/expand/requestFullscreen/disableVerticalSwipes`,
  - обработка `themeChanged`,
  - чтение `start_param`,
  - обертки для DeviceStorage.
- Жест "swipe back" на мобильных (`src/shared/gestures/useIOSSwipeBack.ts`) с защитой от ложных срабатываний в горизонтально-скроллимых зонах.
- Темизация (`src/shared/theme/useAppTheme.ts`):
  - приоритет Telegram theme,
  - fallback на системную тему,
  - дизайн-токены в `src/shared/styles/global.css`.
- Двухслойный загрузчик:
  - стартовый (`index.html`),
  - React-loader (`src/shared/ui/Loader.tsx`) с fade-out и heartbeat анимацией.
- Browser mock Telegram API (`src/shared/telegram/mockTelegram.ts`) через `?mockTelegram=1`.

## Технологии

- React 18
- TypeScript 5
- Vite 6
- React Router 6
- ESLint 9

## Быстрый старт

## 1) Установка

```bash
npm install
```

## 2) Настройка окружения

Скопируйте `.env.example` в `.env`:

```bash
cp .env.example .env
```

`.env.example`:

```env
VITE_AUTH_ENDPOINT=
VITE_API_BASE_URL=
VITE_DEFAULT_ROLE=client
```

## 3) Запуск

```bash
npm run dev
```

Откройте `http://localhost:5173`.

## Скрипты

- `npm run dev` - запуск dev-сервера Vite
- `npm run build` - type-check (`tsc -b`) + production build
- `npm run preview` - локальный просмотр production build
- `npm run lint` - ESLint по `src/**/*.{ts,tsx}`

## Переменные окружения

- `VITE_AUTH_ENDPOINT`
  - URL backend endpoint для Telegram auth.
  - Если пустой, auth-запрос не отправляется.
- `VITE_API_BASE_URL`
  - Базовый URL для `httpClient` (`src/shared/http/httpClient.ts`).
  - Пример: `https://api.example.com`.
- `VITE_DEFAULT_ROLE`
  - Роль по умолчанию (`client|specialist|manager`).
  - Используется, если роль не определилась из query/start_param/auth.

## Как определяется роль

Приоритет источников роли (`src/shared/roles/resolveRole.ts`, `src/app/bootstrap/useAppBootstrap.ts`):

1. `?role=` или `?mockRole=` в URL
2. `Telegram.WebApp.initDataUnsafe.start_param`
3. `role` из ответа auth endpoint
4. `VITE_DEFAULT_ROLE`
5. fallback: `client`

Дополнительно:
- `expert` автоматически нормализуется в `specialist` (`src/shared/roles/role.ts`).

Примеры:
- `http://localhost:5173/?role=client`
- `http://localhost:5173/?role=specialist`
- `http://localhost:5173/?role=manager`
- `http://localhost:5173/?mockTelegram=1&role=client`

## Контракт Telegram auth

Файл: `src/shared/auth/authApi.ts`

Запрос (`POST`, `Content-Type: application/json`):

```json
{
  "init_data": "<Telegram initData>",
  "user_id": 123456,
  "init_data_unsafe": { "...": "..." }
}
```

Важно:
- если `VITE_AUTH_ENDPOINT` пустой или `initData` отсутствует, функция возвращает `null` и auth не блокирует запуск.

Ожидаемые поля ответа (нормализация tolerant, можно частично):

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "Bearer",
  "expires_at": "2026-12-31T23:59:59Z",
  "role": "client",
  "user": { "role": "manager" }
}
```

Как интерпретируется ответ:
- роль берется из `role` или `user.role`;
- токены сохраняются, если есть `access_token`.

## Роутинг

Для каждой роли подключаются одинаковые пути:
- `/` - кабинет роли (`RoleCabinetHomePage`)
- `/profile` - редактирование профиля (`RoleProfileEditorPage`)

Модули роутов:
- `src/roles/client/ClientRoutes.tsx`
- `src/roles/specialist/SpecialistRoutes.tsx`
- `src/roles/manager/ManagerRoutes.tsx`

## UI и UX (что увидит пользователь)

## Главная страницы роли (`/`)

Файл: `src/shared/ui/templates/RoleCabinetHomePage.tsx`

- Карточка профиля (`ProfileCabinetCard`) с:
  - аватаром (или инициалами),
  - именем,
  - подписью роли,
  - переходом на `/profile` по клику.
- Блок-заглушка "Функционал для ..." для быстрого расширения бизнес-фич.

## Страница профиля (`/profile`)

Файл: `src/shared/ui/templates/RoleProfileEditorPage.tsx`

Поля:
- Имя (обязательное визуально)
- Фамилия
- Никнейм Telegram (read-only)
- Почта (обязательная валидация)
- Телефон (маска `+7 (___) ___-__-__`, обязательная валидация)
- Фото профиля (через file input)

Поведение:
- `Сохранить` -> `Сохранение...` -> `Сохранено`
- haptic уведомление:
  - `success` при успешном сохранении
  - `error` при ошибке/невалидных данных
- валидация почты регуляркой
- валидация телефона по локальным 10 цифрам внутри маски

## Где и как хранится профиль

Файл: `src/shared/profile/clientProfile.ts`

Ключ хранения для каждой роли:
- `miniapp:<role>:profile`

При загрузке профиля:
- берется данные из `localStorage`,
- если пусто, используются Telegram user fields (`first_name`, `last_name`, `photo_url`).

При сохранении профиля:
- сохраняется в `localStorage` всегда,
- дополнительно вызывается `Telegram.WebApp.DeviceStorage.setItem` (если API доступно).

При открытии редактора профиля:
- после первичной загрузки делается попытка дочитать значения из DeviceStorage и обновить форму.

## Telegram-специфика

## Back navigation

`src/shared/telegram/useTelegramBackButton.ts` + `src/shared/gestures/useIOSSwipeBack.ts`:

- на корневой странице `/` Telegram BackButton отключен;
- на внутренних страницах показывает BackButton и вызывает `navigate(-1)`;
- на мобильных дополнительно работает жест свайпа назад;
- на нажатия кнопок в интерфейсе добавлен haptic impact (`light`).

## Viewport и fullscreen

`src/shared/telegram/useTelegramViewport.ts`:

- вызывает `ready()`, `expand()`, `disableVerticalSwipes()`;
- на мобильных пытается `requestFullscreen()` (fallback на `expand()`);
- устанавливает CSS-переменную `--telegram-top-safe-offset` для safe area.

## Тема

`src/shared/theme/useAppTheme.ts`:
- Telegram `colorScheme` приоритетен;
- если недоступно, берется системная тема;
- слушается событие `themeChanged`.

## Локальная разработка без Telegram

Включите mock Telegram API:

- `http://localhost:5173/?mockTelegram=1&role=client`

Что даёт mock:
- fake `Telegram.WebApp` объект,
- `initDataUnsafe.user` с demo-данными,
- no-op реализации `ready/expand/requestFullscreen/BackButton/HapticFeedback`,
- in-memory `DeviceStorage`.

Ограничения mock:
- это только локальная имитация, не проверяет серверную валидацию `initData`;
- не воспроизводит реальное поведение Telegram-клиента на 100%.

## HTTP-клиент

Файл: `src/shared/http/httpClient.ts`

Поддерживает методы:
- `get`
- `post`
- `put`
- `patch`
- `del`

Особенности:
- автоматически добавляет `Content-Type: application/json`;
- автоматически добавляет `Authorization` из сохраненных токенов (`authStorage`), если есть `accessToken`.

## Структура проекта

```text
src/
  app/
    App.tsx
    bootstrap/
      types.ts
      useAppBootstrap.ts
    layout/
      AppShell.tsx
      AppShell.module.css
    routing/
      RoleRouter.tsx

  roles/
    client/
      ClientRoutes.tsx
      pages/
        ClientHomePage.tsx
        ClientProfile/
          ClientProfilePage.tsx
    specialist/
      SpecialistRoutes.tsx
      pages/
        SpecialistHomePage.tsx
        SpecialistProfile/
          SpecialistProfilePage.tsx
    manager/
      ManagerRoutes.tsx
      pages/
        ManagerHomePage.tsx
        ManagerProfile/
          ManagerProfilePage.tsx

  shared/
    auth/
      authApi.ts
      authStorage.ts
      types.ts
    gestures/
      useIOSSwipeBack.ts
    http/
      httpClient.ts
    profile/
      clientProfile.ts
    roles/
      role.ts
      resolveRole.ts
    styles/
      global.css
    telegram/
      mockTelegram.ts
      useTelegramBackButton.ts
      useTelegramViewport.ts
      webApp.ts
    theme/
      useAppTheme.ts
    ui/
      Loader.tsx
      Loader.module.css
      ProfileCabinetCard/
        ProfileCabinetCard.tsx
        ProfileCabinetCard.module.css
      templates/
        RoleCabinetHomePage.tsx
        RoleCabinetHomePage.module.css
        RoleProfileEditorPage.tsx
        RoleProfileEditorPage.module.css
```

## Что важно при расширении

- Если добавляете новые страницы роли:
  - добавляйте их в соответствующий `*Routes.tsx`;
  - учитывайте BackButton/жесты (они работают через `AppShell`).
- Если меняете профиль:
  - обновляйте единый `RoleProfileEditorPage` и `clientProfile.ts`,
  - не создавайте отдельные дубли под роли без необходимости.
- Если подключаете backend:
  - сначала задайте `VITE_AUTH_ENDPOINT` и `VITE_API_BASE_URL`,
  - затем используйте `httpClient` для типизированных вызовов.

## Проверка перед коммитом

```bash
npm run lint
npm run build
```

## Частые проблемы

- Пустой экран с ошибкой инициализации:
  - проверьте корректность `VITE_AUTH_ENDPOINT`;
  - убедитесь, что backend принимает `init_data` и возвращает `2xx`.
- Роль не та, что ожидается:
  - проверьте query-параметры `?role=`;
  - проверьте `start_param` в Telegram;
  - проверьте, что backend возвращает валидный `role`.
- Токен не уходит в API:
  - убедитесь, что в auth ответе есть `access_token`;
  - проверьте `localStorage` ключ `miniapp:auth:tokens`.

