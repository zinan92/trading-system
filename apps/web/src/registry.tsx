/* Every island the desk pages can mount, by data-island name. */
import {ChartFrame} from "./islands/ChartFrame";
import {ControlActions, RobotActions, StartRisk} from "./islands/ControlActions";
import {ControlPicker} from "./islands/ControlPicker";
import {AccountCards} from "./islands/AccountCards";
import {AiSources} from "./islands/AiSources";
import {BacktestBox} from "./islands/BacktestBox";
import {ExecTables} from "./islands/ExecTables";
import {GmHeader} from "./islands/GmHeader";
import {GridSummary, PreviewSummary} from "./islands/ControlSummaries";
import {ReviewPanel} from "./islands/ReviewPanel";
import {TodayPanel} from "./islands/TodayPanel";
import {WhyPanel} from "./islands/WhyPanel";
import {Workbench} from "./islands/Workbench";


export const islands = {"backtest": BacktestBox, "ai-sources": AiSources, "review": ReviewPanel, "workbench": Workbench, "today": TodayPanel, "exec-tables": ExecTables, "gm-cards": AccountCards, "gm-header": GmHeader, "control-picker": ControlPicker, "control-actions": ControlActions, "robot-actions": RobotActions, "start-risk": StartRisk, "chart-frame": ChartFrame, "gm-grid-summary": GridSummary, "gm-preview": PreviewSummary, "why": WhyPanel};
