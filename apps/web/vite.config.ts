/* The islands build into the desk's static folder as one IIFE script, committed with the source.
 * CI rebuilds it and fails when the committed file differs (make test-web). */
import {defineConfig} from "vitest/config";
import react from "@vitejs/plugin-react";

// Relative to apps/web (Vite's root); the npm scripts always run from there.
const OUT = "../trading-desk/src/trading_desk/static/web";

export default defineConfig(({command}) => ({
  plugins: [react()],
  // Library mode leaves process.env.NODE_ENV alone; the browser has no `process`. Tests keep React's dev build (act needs it).
  define: command === "build" ? {"process.env.NODE_ENV": JSON.stringify("production")} : {},
  build: {
    outDir: OUT,
    emptyOutDir: true,
    sourcemap: false,
    lib: {entry: "src/main.tsx", formats: ["iife"], name: "DeskWeb", fileName: () => "web.js"},
  },
  test: {environment: "jsdom", globals: true, include: ["src/**/*.test.{ts,tsx}"]},
}));
