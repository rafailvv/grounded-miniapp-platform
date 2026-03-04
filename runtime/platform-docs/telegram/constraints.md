# Telegram Mini-App Constraints

- Load `telegram-web-app.js` and use `window.Telegram.WebApp` as the SDK entrypoint.
- Treat `initDataUnsafe` as untrusted client data.
- Validate `initData` on the server before treating user identity or session data as trusted.
- Respect host-provided theme parameters, `colorScheme`, and viewport metrics.
- Avoid browser-only assumptions that conflict with Telegram container navigation.

