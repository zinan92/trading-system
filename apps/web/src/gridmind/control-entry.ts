/* Builds to assets/gridmind/control.js (vite.control.config.ts), a plain script loaded before gridmind.js.
 * gridmind.js is not a module, so the control layer reaches it as window.gmControl. It sends nothing itself:
 * gridmind.js's control() sends every request (ADR 0008). */
import * as control from "./control";

declare global {
  interface Window { gmControl: typeof control }
}

window.gmControl = control;
