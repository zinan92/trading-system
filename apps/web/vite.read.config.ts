/* #1416: GridMind's read layer builds into assets/gridmind/read.js, a plain script that
 * the desk's /trade loads before gridmind.js (8765's dashboard-gridmind.html did too until #1419). Committed with the
 * source; CI rebuilds it and fails when the committed file differs (make test-web).
 * emptyOutDir stays false: the folder also holds gridmind.js and the other page files. */
import {defineConfig} from "vite";

export default defineConfig({
  publicDir: false,
  build: {
    outDir: "../../assets/gridmind",
    emptyOutDir: false,
    sourcemap: false,
    minify: false,
    lib: {entry: "src/gridmind/read-entry.ts", formats: ["iife"], name: "GridMindRead", fileName: () => "read.js"},
  },
});
