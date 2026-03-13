/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_AUTH_ENDPOINT?: string;
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_DEFAULT_ROLE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
