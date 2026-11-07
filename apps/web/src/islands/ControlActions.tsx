/* The buttons that act and the risk review before a start (#1397, ADR 0008). GridMind decides what can
 * be pressed and publishes it; this draws it. A press reaches GridMind through the document-level
 * intent router, so there are no handlers here and no requests. Nothing here computes a gate. */
import {StartBlockers, type Blocker} from "./ControlSummaries";

interface ActionsProps {previewDisabled?: boolean; confirmDisabled?: boolean}

export function ControlActions({previewDisabled = true, confirmDisabled = true}: ActionsProps) {
  return (
    <>
      <button className="button subtle" id="dashboardPreviewButton" data-gm-intent="control.preview" type="button" disabled={previewDisabled}>生成 Strategy Preview</button>
      <button className="button" id="dashboardConfirmButton" data-gm-intent="control.confirm" type="button" disabled={confirmDisabled}>{"Confirm & Run"}</button>
    </>
  );
}

interface RobotButton {className: string; disabled: boolean; text: string; title?: string}
interface RobotProps {start?: RobotButton; stop?: RobotButton}

export function RobotActions({
  start = {className: "robot start", disabled: false, text: "启动机器人"},
  stop = {className: "robot stop", disabled: false, text: "停止 + 撤单 + 平仓"},
}: RobotProps) {
  return (
    <>
      <button className={start.className} id="startRobot" data-gm-intent="robot.start" type="button" disabled={start.disabled} title={start.title || undefined}>{start.text}</button>
      <button className={stop.className} id="stopRobot" data-gm-intent="robot.stop" type="button" disabled={stop.disabled}>{stop.text}</button>
    </>
  );
}

interface Ack {code: string; title?: string; summary?: string; critical?: boolean; checked?: boolean}
interface RiskProps {
  rows?: [label: string, before: string, after: string][];
  acks?: Ack[];
  blockers?: Blocker[];
  gate?: {ok: boolean; text: string};
  confirmText?: string;
  confirmDisabled?: boolean;
}

// A controlled checkbox needs an onChange. The state lives in GridMind, which hears the click.
const published = () => {};

export function StartRisk({rows = [], acks = [], blockers, gate, confirmText = "确认风险并启动机器人", confirmDisabled = true}: RiskProps) {
  return (
    <>
      <div className="grid-compare" id="startRiskComparison">
        {rows.map(([label, before, after]) => (
          <div className="grid-compare-row" key={label}><span>{label}</span><b>{before}</b><i>→</i><b>{after}</b></div>
        ))}
      </div>
      <div className="grid-risk-acks" id="startRiskAcknowledgements">
        {acks.map(row => (
          <label className={row.critical ? "grid-risk-ack critical" : "grid-risk-ack"} key={row.code}>
            <input type="checkbox" data-gm-intent="startRisk.ack" data-start-risk-ack={row.code} checked={row.checked === true} onChange={published} />
            <span><b>{row.title}</b><small>{row.summary}</small></span>
          </label>
        ))}
      </div>
      <div className="start-blocker-list" id="startRiskBlockers"><StartBlockers rows={blockers} /></div>
      <div className={gate?.ok ? "grid-review-gate ok" : "grid-review-gate"} id="startRiskGate">{gate?.text}</div>
      <div className="grid-review-actions">
        <button className="button subtle" id="cancelStartRisk" data-gm-intent="startRisk.close" type="button">返回修改</button>
        <button className="button replace-grid" id="confirmStartRisk" data-gm-intent="startRisk.confirm" type="button" disabled={confirmDisabled}>{confirmText}</button>
      </div>
    </>
  );
}
