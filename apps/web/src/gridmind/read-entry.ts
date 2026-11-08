/* Builds to assets/gridmind/read.js (vite.read.config.ts), a plain script loaded before gridmind.js.
 * gridmind.js is not a module, so the read layer reaches it as window.gmRead. */
import * as read from "./read";

declare global {
  interface Window { gmRead: typeof read }
}

window.gmRead = read;
