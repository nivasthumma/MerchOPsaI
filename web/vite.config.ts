import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to FastAPI on :8000.
//
// This is deliberate, and it is why the API has no CORS middleware. Adding
// permissive CORS to an API whose entire premise is that authorization lives
// server-side would widen its attack surface to save a proxy rule. Same-origin
// in development, same-origin in deployment (serve `dist/` behind the API or a
// reverse proxy) — the browser never makes a cross-origin request at all.
// One definition, used by both `vite dev` and `vite preview`. They are
// separate config keys, and the browser E2E suite runs against `preview` — a
// proxy configured for only one of them means the built bundle talks to
// nothing, and the API has no CORS to fall back on by design.
const apiProxy = {
  "/api": {
    target: process.env.API_ORIGIN ?? "http://127.0.0.1:8000",
    changeOrigin: true,
    rewrite: (p: string) => p.replace(/^\/api/, ""),
  },
};

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: apiProxy },
  // `vite preview` serves `dist/`, which is what deployment serves. The E2E
  // suite drives this rather than the dev server so it exercises the bundle
  // that ships, not a differently-transformed one.
  preview: { proxy: apiProxy },
  build: { outDir: "dist", sourcemap: true },
});
