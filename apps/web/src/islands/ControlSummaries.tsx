/* Text-only parts of GridMind's control panel (#1329): the grid line above the chart, the preview
 * numbers and why a start is blocked. Display only. Everything the operator acts on stays GridMind's:
 * selects, buttons, risk checkboxes and every request. Nothing here is read back as an approval. */
type Item = [kind: "dot" | "b" | "s", cls: string, text: string];

export function GridSummary({items, loaded}: {items?: Item[]; loaded?: boolean}) {
  if (!loaded || !items) return <>网格读取中</>;
  return (
    <>
      {items.map(([kind, cls, text], i) =>
        kind === "dot" ? <span key={i} className={cls ? `grid-dot ${cls}` : "grid-dot"} />
          : kind === "b" ? <b key={i}>{text}</b>
          : <span key={i} className={cls || undefined}>{text}</span>)}
    </>
  );
}

type Row = [label: string, value: string, cls: string];
interface PreviewProps {
  problem?: {title: string; detail: string};
  rows?: Row[];
  risks?: [cls: string, text: string][];
  loaded?: boolean;
}

export function PreviewSummary({problem, rows, risks, loaded}: PreviewProps) {
  if (!loaded) return null;
  if (problem) return <div className="preview-problem" role="status"><b>启动前需补齐：{problem.title}</b><span>{problem.detail}</span></div>;
  return (
    <>
      {(rows || []).map(([label, value, cls]) => <div key={label}><span>{label}</span><b className={cls || undefined}>{value}</b></div>)}
      {(risks || []).map(([cls, text], i) => <div key={i} className={cls}>{text}</div>)}
    </>
  );
}

interface Blocker {code: string; title: string; explanation: string; action: string; lines: string[]}

export function StartBlockers({rows}: {rows?: Blocker[]}) {
  return (
    <>
      {(rows || []).map((row, i) => (
        <section className="start-blocker" key={i}>
          <b>{row.title}</b><p>{row.explanation}</p>
          {row.lines.length ? <ul>{row.lines.map((line, j) => <li key={j}>{line}</li>)}</ul> : null}
          <p>{row.action}</p><code>机器码：{row.code}</code>
        </section>
      ))}
    </>
  );
}
