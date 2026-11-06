/* Entry for static/web/web.js, loaded by the /trade shell after the legacy desk scripts. */
import {createBridge} from "./bridge";
import {AccountCards} from "./islands/AccountCards";
import {AiSources} from "./islands/AiSources";
import {BacktestBox} from "./islands/BacktestBox";
import {ExecTables} from "./islands/ExecTables";
import {GmHeader} from "./islands/GmHeader";
import {GridSummary, PreviewSummary, StartBlockers} from "./islands/ControlSummaries";
import {NewsRail} from "./islands/NewsRail";
import {ReviewPanel} from "./islands/ReviewPanel";
import {TodayPanel} from "./islands/TodayPanel";
import {WatchPanel} from "./islands/WatchPanel";
import {Workbench} from "./islands/Workbench";

export const islands = {"watch": WatchPanel, "backtest": BacktestBox, "ai-sources": AiSources, "review": ReviewPanel, "workbench": Workbench, "today": TodayPanel, "news": NewsRail, "exec-tables": ExecTables, "gm-cards": AccountCards, "gm-header": GmHeader, "gm-grid-summary": GridSummary, "gm-preview": PreviewSummary, "gm-blockers": StartBlockers};

const bridge = createBridge(islands);
const boot = () => bridge.start(document.body);
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, {once: true});
else boot();

export const mounted = bridge.mounted;
