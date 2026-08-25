/// <reference types="vite/client" />

/** Build-time configuration. See `getToken` in api/client.ts for why the demo
 *  credential is injected rather than committed. */
interface ImportMetaEnv {
  readonly VITE_DEMO_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
