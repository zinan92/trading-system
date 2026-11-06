/* GridMind 顶栏状态 (#1353). GridMind still computes every value (run state, market trust, price
 * direction, who may control) and publishes it. The defaults are its static placeholders: a read-model
 * failure before the first render updates only the run state and leaves these, as before. */
export interface HeaderProps {
  env?: string; marketClass?: string; marketLabel?: string; source?: string; sourceClass?: string;
  pair?: string; price?: string; priceClass?: string; priceTitle?: string;
  change?: string; changeClass?: string; changeTitle?: string;
  cycle?: string; access?: string; accessClass?: string; runClass?: string; runText?: string;
}

export function GmHeader({
  env = "PAPER", marketClass = "pill run-state", marketLabel = "状态读取中", source = "来源读取中", sourceClass = "pill ok",
  pair = "XAU / USDT", price = "--", priceClass, priceTitle,
  change = "--", changeClass = "header-change", changeTitle = "交易所当日开盘价至今",
  cycle = "周期 --", access = "本地控制", accessClass = "pill", runClass = "top-status", runText = "状态读取中",
}: HeaderProps) {
  return (
    <>
      <span className="pill live" id="envBadge">{env}</span>
      <span className={marketClass} id="marketBadge"><span className="run-dot" aria-hidden="true" /><span>{marketLabel}</span></span>
      <span className={sourceClass} id="sourceBadge">{source}</span>
      <div className="pair-block"><b id="headerPair">{pair}</b><strong id="headerPrice" className={priceClass} title={priceTitle}>{price}</strong><span className={changeClass} id="headerChange" title={changeTitle}>{change}</span></div>
      <span className="pill" id="cycleBadge">{cycle}</span>
      <span className={accessClass} id="accessBadge">{access}</span>
      <span className={runClass} id="runBadge">{runText}</span>
    </>
  );
}
