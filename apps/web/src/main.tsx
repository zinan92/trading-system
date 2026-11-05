/* Entry for static/web/web.js, loaded by the /trade shell after the legacy desk scripts. */
import {createBridge} from "./bridge";
import {AiSources} from "./islands/AiSources";
import {BacktestBox} from "./islands/BacktestBox";
import {WatchPanel} from "./islands/WatchPanel";

export const islands = {"watch": WatchPanel, "backtest": BacktestBox, "ai-sources": AiSources};

const bridge = createBridge(islands);
const boot = () => bridge.start(document.body);
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, {once: true});
else boot();

export const mounted = bridge.mounted;
