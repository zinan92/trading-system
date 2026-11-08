/* #1418: GridMind's control layer builds into assets/gridmind/control.js, a plain script that
 * dashboard-gridmind.html and the desk's /trade both load before gridmind.js. Committed with the
 * source; CI rebuilds it and fails when the committed file differs (make test-web).
 * Vite's IIFE library build takes one entry, so each GridMind layer has its own config. */
import {defineConfig} from "vite";

export default defineConfig({
  publicDir: false,
  build: {
    outDir: "../../assets/gridmind",
    emptyOutDir: false,
    sourcemap: false,
    minify: false,
    lib: {entry: "src/gridmind/control-entry.ts", formats: ["iife"], name: "GridMindControl", fileName: () => "control.js"},
  },
});
