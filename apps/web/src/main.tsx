/* Entry for static/web/web.js, loaded by the /trade shell after the legacy desk scripts. */
import {createBridge} from "./bridge";
import {islands} from "./registry";
import {startTradeApp} from "./trade/TradeApp";

export {islands};

const bridge = createBridge(islands);
// /trade (#1415): the desk page is one React tree. It draws the skeleton, starts GridMind and gm-desk.js,
// then mounts these islands into the tree; the bridge still mounts placeholders added later (gm-desk.js
// writes some into its step 3). Any other page with islands keeps the bridge alone.
const app = document.getElementById("trade-app");
let tree: ReturnType<typeof startTradeApp> | null = null;
const boot = () => {
  if (app) tree = startTradeApp(app, islands, () => bridge.start(document.body));
  else bridge.start(document.body);
};
if (document.readyState === "loading" && !app) document.addEventListener("DOMContentLoaded", boot, {once: true});
else boot();

export const mounted = () => bridge.mounted() + (tree ? tree.islands.mounted() : 0);
