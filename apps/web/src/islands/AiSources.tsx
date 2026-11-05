/* 依据 under today's AI call (#1318). The legacy page re-renders the AI box every minute, which
 * remounts this island; whether the reader had it open is kept per judgment so it does not fold. */
import {useState} from "react";
import {advice, type Judgment} from "../sources";

const openByJudgment = new Map<string, boolean>();

export function AiSources({judgment}: {judgment: Judgment}) {
  const key = String(judgment?.id ?? "");
  const [open, setOpen] = useState(() => openByJudgment.get(key) ?? false);
  const v = advice(judgment);
  if ("empty" in v) return <p className="desk-ai-src muted">{v.empty}</p>;
  const warn = [...(v.unverified.length ? [`输入里没有：${v.unverified.join("、")}`] : []), ...v.flags];
  return (
    <details
      className="desk-ai-src"
      open={open}
      onToggle={event => {
        const next = (event.currentTarget as HTMLDetailsElement).open;
        openByJudgment.set(key, next);
        setOpen(next);
      }}
    >
      <summary>
        依据：{v.summary}
        {warn.length ? <> <span className="warn">{warn.join(" · ")}</span></> : null}
      </summary>
      <ul>
        {v.items.map(item => (
          <li key={item.label} className={item.cited ? "cited" : ""}>
            <span className="k">{item.label}</span>
            {item.url ? <a href={item.url} target="_blank" rel="noopener noreferrer">{item.text}</a> : item.text}
            {item.cited ? <span className="cite">已引用</span> : null}
          </li>
        ))}
      </ul>
      {v.model ? <p className="muted">{v.model}</p> : null}
    </details>
  );
}

/** Test hook: forget remembered open states. */
export function resetAiSourcesMemory() {
  openByJudgment.clear();
}
