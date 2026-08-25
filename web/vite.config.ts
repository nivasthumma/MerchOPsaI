import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to FastAPI on :8000.
//
// This is deliberate, and it is why the API has no CORS middleware. Adding
// permissive CORS to an API whose entire premise is that authorization lives
// server-side would widen its attack surface to save a proxy rule. Same-origin
// in development, same-origin in deployment (serve `dist/` behind the API or a
// reverse proxy) — the browser never makes a cross-origin request at all.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
