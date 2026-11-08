/* #1418: GridMind's control layer builds into assets/gridmind/control.js, a plain script that
 * the desk's /trade loads before gridmind.js (8765's dashboard-gridmind.html did too until #1419). Committed with the
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
