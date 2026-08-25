import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Vitest picks this up in preference to vite.config.ts, which stays purely a
// build/dev-server concern.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    restoreMocks: true,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
