/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API prefix. Default: /api (same origin; Caddy strips the prefix). */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
