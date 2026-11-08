/* #1416: GridMind's read layer builds into assets/gridmind/read.js, a plain script that
 * dashboard-gridmind.html and the desk's /trade both load before gridmind.js. Committed with the
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
