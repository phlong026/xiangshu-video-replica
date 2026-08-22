import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
  },
  envPrefix: ["VITE_", "TAURI_ENV_*"],
  build: {
    target:
      process.env.TAURI_ENV_PLATFORM === "windows" ? "chrome105" : "safari13",
    minify: process.env.TAURI_ENV_DEBUG ? false : "oxc",
    sourcemap: Boolean(process.env.TAURI_ENV_DEBUG),
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    restoreMocks: true,
    // Node >= 25 ships the Web Storage API enabled by default, and that
    // native globalThis.localStorage makes the jsdom environment skip
    // installing its own Storage (window keys already present on globalThis
    // are skipped), leaving tests with a method-less localStorage stub.
    // Disable the native implementation in test workers so jsdom's Storage
    // is used on every supported Node version; on Node 24 (CI) the flag is a
    // harmless no-op against an already-off default.
    execArgv: ["--no-experimental-webstorage"],
  },
});
