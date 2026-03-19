# Unified Architecture Mini App

Minimal starter frontend for a tri-role mini app built with `React + TypeScript + Vite`.

Roles:
- `client`
- `specialist`
- `manager`

What is included:
- role bootstrap from `?role=` or Telegram `start_param`
- home page for each role
- profile page for each role
- local profile storage with backend sync when `VITE_API_BASE_URL` is set
- Telegram WebApp helpers for viewport, back button, theme, device storage, and haptics

What is intentionally not included:
- authentication
- runtime manifest system
- generated domain screens
- domain-specific business logic

## Environment

```env
VITE_API_BASE_URL=
VITE_DEFAULT_ROLE=client
```

## Run

```bash
npm install
npm run dev
```

Open `http://localhost:5173`.

## Scripts

- `npm run dev`
- `npm run build`
- `npm run preview`
- `npm run lint`

## Role resolution order

1. `?role=` or `?mockRole=` in URL
2. Telegram `start_param`
3. `VITE_DEFAULT_ROLE`
4. fallback: `client`
