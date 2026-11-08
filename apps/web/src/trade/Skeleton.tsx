/* The /trade skeleton (#1415), generated once from the pre-#1415 page (dashboard-gridmind.html after
 * gm-desk.js's structural mounts) and edited by hand from here on. It renders once and never again:
 * nothing here has state, so GridMind and gm-desk.js may write inside these nodes. Island placeholders
 * keep their legacy children as opaque markup; the island replaces them when it mounts (Islands.tsx).
 * Every text node is a literal so whitespace between inline elements stays exactly as it was. */

function TopBar() {
  return (
    <header className="top">
      {"\n    "}
      <div className="brand">
        <span>
          {"GRID"}
          <em>
            {"MIND"}
          </em>
        </span>
        <small>
          {"production grid console"}
        </small>
      </div>
      <nav className="desk-nav" aria-label="页面">
        <a href="/trade" aria-current="page">
          {"交易"}
        </a>
        <a href="/desk#news">
          {"日报"}
        </a>
        <a href="/desk#system">
          {"系统"}
        </a>
      </nav>
      <div className="desk-assets" id="deskAssets" role="group" aria-label="品种" />
      {"\n    "}
      <div className="gm-status" data-island="gm-header" data-props={"{}"} id="gmStatus" dangerouslySetInnerHTML={{__html: "<span class=\"pill live\" id=\"envBadge\">PAPER</span>\n    <span class=\"pill run-state\" id=\"marketBadge\"><span class=\"run-dot\" aria-hidden=\"true\"></span><span>状态读取中</span></span>\n    <span class=\"pill ok\" id=\"sourceBadge\">来源读取中</span>\n    <div class=\"pair-block\"><b id=\"headerPair\">XAU / USDT</b><strong id=\"headerPrice\">--</strong><span class=\"header-change\" id=\"headerChange\" title=\"交易所当日开盘价至今\">--</span></div>\n    <span class=\"pill\" id=\"cycleBadge\">周期 --</span>\n    <span class=\"pill\" id=\"accessBadge\">本地控制</span>\n    <span class=\"top-status\" id=\"runBadge\">状态读取中</span>"}} />
      {"\n  "}
      <a className="desk-pass" href="/passcode">
        {"改口令"}
      </a>
    </header>
  );
}

function TodayCard() {
  return (
    <section className="card desk-judge" id="deskToday" aria-labelledby="deskJudgeTitle">
      {"\n      "}
      <div className="card-title">
        <span id="deskJudgeTitle">
          {"今天"}
        </span>
        <span className="desk-meta" style={{"padding": "0"}} id="deskJudgeDate">
          {"11月8日"}
        </span>
      </div>
      {"\n      "}
      <div className="card-body">
        {"\n        "}
        <div className="desk-today" data-island="today" data-props={"{}"} dangerouslySetInnerHTML={{__html: ""}} />
        {"\n        "}
        <ol className="desk-steps desk-steps-exec">
          {"\n        "}
          <li className="desk-step" data-step="3">
            <span className="desk-n" aria-hidden="true">
              {"3"}
            </span>
            <div className="desk-step-body">
              {"\n          "}
              <h3>
                {"你亲手启动"}
              </h3>
              {"\n          "}
              <div className="desk-plan" id="deskExecBox" />
              {"\n          "}
              <div id="deskStop" />
              {"\n        "}
            </div>
          </li>
          {"\n        "}
        </ol>
        {"\n      "}
      </div>
    </section>
  );
}

function ChartCard() {
  return (
    <section className="card chart-card">
      <div className="chart-title">
        <h2>
          {"行情 · K 线 / 生产网格"}
        </h2>
        <div className="chart-tools">
          <div className="indicators">
            <label className="indicator-toggle">
              <input id="showEma" type="checkbox" defaultChecked />
              {"EMA"}
            </label>
            <label className="ema-period">
              {"快"}
              <input id="emaFast" type="number" min="2" max="200" defaultValue="20" />
            </label>
            <label className="ema-period">
              {"慢"}
              <input id="emaSlow" type="number" min="3" max="300" defaultValue="50" />
            </label>
            <label className="indicator-toggle">
              <input id="showMacd" type="checkbox" />
              {"MACD"}
            </label>
          </div>
          <button className="grid-adjust-toggle" id="gridAdjustToggle" type="button" aria-pressed="false">
            {"调整网格"}
          </button>
          <div className="timeframes" id="timeframeButtons" />
        </div>
      </div>
      <div className="chart-key" id="chartMeta">
        {"可信 datafeed 读取中"}
      </div>
      <div className="chart-notice" id="chartHistoryNotice">
        {"向左滚动或向右拖动图表，可加载更早 K 线。"}
      </div>
      <div className="production-strategy-summary" id="productionStrategySummary">
        {"当前生产策略读取中"}
      </div>
      <div className="grid-summary" id="gridSummary" data-island="gm-grid-summary" data-props={"{}"} dangerouslySetInnerHTML={{__html: "网格读取中"}} />
      <div className="chart-gate hidden" id="gate" />
      <div className="chart-wrap" id="chartFrame" data-island="chart-frame" dangerouslySetInnerHTML={{__html: "<div class=\"chart\" id=\"productionChart\" data-standard-kline-host=\"\"></div>"}} />
    </section>
  );
}

function DataCard() {
  return (
    <section className="card data-card">
      <div className="tabs" id="tabs">
        <button data-tab="positions" type="button">
          {"持仓 "}
          <span id="positionsCount">
            {"(0)"}
          </span>
        </button>
        <button data-tab="orders" type="button">
          {"委托 "}
          <span id="ordersCount">
            {"(0)"}
          </span>
        </button>
        <button className="on" data-tab="fills" type="button">
          {"成交 "}
          <span id="tradesCount">
            {"(0)"}
          </span>
        </button>
        <button data-tab="supervisor" type="button" className="desk-tab-quiet">
          {"运行监督 "}
        </button>
        <button data-tab="review" type="button">
          {"交易回看 "}
        </button>
        <button data-tab="shadows" type="button" className="desk-tab-quiet">
          {"影子策略 "}
        </button>
        <button data-tab="history" type="button">
          {"收益曲线 "}
        </button>
        <button data-tab="bench" type="button">
          {"回测"}
        </button>
      </div>
      <div className="tab" data-panel="positions">
        <div className="table-wrap">
          <table id="positions" data-island="exec-tables" data-props={"{\"table\":\"positions\"}"} dangerouslySetInnerHTML={{__html: ""}} />
        </div>
      </div>
      <div className="tab" data-panel="orders">
        <div className="table-wrap">
          <table id="orders" data-island="exec-tables" data-props={"{\"table\":\"orders\"}"} dangerouslySetInnerHTML={{__html: ""}} />
        </div>
      </div>
      <div className="tab on" data-panel="fills">
        <div className="table-wrap">
          <table id="fills" data-island="exec-tables" data-props={"{\"table\":\"fills\"}"} dangerouslySetInnerHTML={{__html: ""}} />
        </div>
      </div>
      <div className="tab card-body" data-panel="supervisor" id="supervisor" />
      <div className="tab card-body" data-panel="review-legacy" id="review" />
      <div className="tab card-body desk-review" data-panel="review" id="deskReview" data-island="review" data-props={"{}"} dangerouslySetInnerHTML={{__html: ""}} />
      <div className="tab" data-panel="shadows">
        <div className="shadow-grid" id="shadows" />
      </div>
      <div className="tab card-body" data-panel="history" id="history" />
      <div className="tab card-body" data-panel="bench" data-island="workbench" data-props={"{}"} dangerouslySetInnerHTML={{__html: ""}} />
    </section>
  );
}

function ControlCard() {
  return (
    <section className="card dashboard-control-card" id="dashboardControlCard">
      <div className="card-title">
        {"Trading BOT · Venue → Asset → Strategy"}
      </div>
      <div className="card-body">
        <div className="gm-pick-steps" id="dashboardPicker" data-island="control-picker" dangerouslySetInnerHTML={{__html: "<div class=\"gm-pick-step\"><span class=\"label\">1 · Venue Profile</span><select class=\"select\" id=\"dashboardVenueSelect\" data-gm-intent=\"control.venue\" aria-label=\"选择 Venue Profile\"><option value=\"\">读取中…</option></select></div><div class=\"gm-pick-step\"><span class=\"label\">2 · Instrument</span><select class=\"select\" id=\"dashboardInstrumentSelect\" data-gm-intent=\"control.instrument\" aria-label=\"选择交易资产\" disabled=\"\"><option value=\"\">先选择 Venue</option></select></div><div class=\"gm-pick-step\"><span class=\"label\">3 · Strategy Family</span><div class=\"choice-row\" id=\"dashboardStrategyChoices\"><button class=\"choice\" data-dashboard-strategy=\"grid\" data-gm-intent=\"control.strategy\" type=\"button\" disabled=\"\">Grid<small>旧版网格</small></button><button class=\"choice\" data-dashboard-strategy=\"dca\" data-gm-intent=\"control.strategy\" type=\"button\" disabled=\"\">DCA<small>旧版 DCA</small></button></div></div>"}} />
        <div className="gm-control-actions" id="dashboardActions" data-island="control-actions" dangerouslySetInnerHTML={{__html: "<button class=\"button subtle\" id=\"dashboardPreviewButton\" data-gm-intent=\"control.preview\" type=\"button\" disabled=\"\">生成 Strategy Preview</button><button class=\"button\" id=\"dashboardConfirmButton\" data-gm-intent=\"control.confirm\" type=\"button\" disabled=\"\">Confirm &amp; Run</button>"}} />
        <p className="desk-rail-note">
          {"日常从「今天」面板开始：选方向 → 批准计划 → 亲手按执行。这里是手动细调，按钮的作用和以前一样。"}
        </p>
        <div className="dashboard-control-preview" id="dashboardPreviewSummary" aria-live="polite" />
        <div className="dashboard-control-account" id="dashboardAccountSummary" aria-live="polite" />
        <div className="dashboard-control-runtime" id="dashboardRuntimeStatus" aria-live="polite" />
        <p className="dashboard-control-status" id="dashboardControlStatus" aria-live="polite">
          {"正在读取可用 Venue Profile…"}
        </p>
      </div>
    </section>
  );
}

function AiChatCard() {
  return (
    <section className="card park-ai-chat-card top-ai-card rail-ai-card" id="parkAiChatCard">
      <div className="card-title">
        {"Park AI 策略助手 · Paper"}
      </div>
      <div className="card-body">
        <div className="park-ai-chat-messages" id="parkAiChatMessages" aria-live="polite">
          <div className="park-ai-chat-message system">
            {"用自然语言描述策略。我会先解析并展示 snapshot，只有你点击“确认并执行”才会提交到 Paper 计划。"}
          </div>
        </div>
        <div className="park-ai-snapshot" id="parkAiSnapshot" />
        <textarea className="park-ai-input" id="parkAiChatInput" maxLength={8000} placeholder="例如：做空 DCA，区间 4444~4200，最大 10 倍杠杆，止损 4444，止盈 4100" />
        <div className="park-ai-actions">
          <button className="primary" id="parkAiSend" type="button">
            {"解析策略"}
          </button>
          <button id="parkAiConfirm" type="button" disabled>
            {"确认并执行"}
          </button>
          <button className="danger" id="parkAiReject" type="button" disabled>
            {"拒绝 Draft"}
          </button>
        </div>
        <p className="park-ai-status" id="parkAiChatStatus" aria-live="polite" />
      </div>
    </section>
  );
}

function MarketCard() {
  return (
    <section className="card market-card">
      <div className="card-title">
        {"AI 决策引擎 · 市场与趋势"}
      </div>
      <div className="card-body">
        <div className="split">
          <label>
            <span className="label">
              {"Broker 来源"}
            </span>
            <input className="input" id="brokerSource" readOnly defaultValue="读取中" />
          </label>
          <label>
            <span className="label">
              {"交易对"}
            </span>
            <select className="select" id="symbol">
              <option value="GOLD">
                {"GOLD"}
              </option>
            </select>
          </label>
        </div>
        <div className="market-actions">
          <label>
            <span className="label">
              {"K 线观察周期"}
            </span>
            <select className="select" id="timeframe">
              <option value="1m">
                {"1m"}
              </option>
              <option value="5m">
                {"5m"}
              </option>
              <option value="15m">
                {"15m"}
              </option>
              <option value="30m">
                {"30m"}
              </option>
              <option value="1h">
                {"1h"}
              </option>
              <option value="4h">
                {"4h"}
              </option>
            </select>
          </label>
          <div>
            <span className="label">
              {"重新评估"}
            </span>
            <button className="button subtle" id="refreshTrend" type="button">
              {"刷新趋势"}
            </button>
          </div>
        </div>
        <div className="trend-box" id="trendBox">
          <span className="label">
            {"当前趋势识别"}
          </span>
          <h3>
            {"读取可信行情中"}
          </h3>
          <p>
            {"只使用真实 broker 行情；行情陈旧、缺失或 synthetic 时停止新开仓。"}
          </p>
          <button className="recommendation" id="adoptRecommendation" type="button">
            {"采用推荐策略 + 自动区间"}
          </button>
        </div>
      </div>
    </section>
  );
}

function StrategyCard() {
  return (
    <section className="card strategy-card" id="strategyCard" data-strategy-type="grid">
      <div className="card-title">
        {"策略配置 · 启动前预览"}
      </div>
      <div className="card-body">
        <div className="choice-label">
          {"策略类型"}
        </div>
        <div className="choice-row two strategy-type-row" id="strategyTypeChoices" data-gm-intent="strategy.type">
          <button className="choice on" data-strategy-type="grid" type="button" aria-pressed="true" aria-label="Grid 网格，当前选择">
            {"Grid 网格"}
            <small>
              {"逐格止盈"}
            </small>
          </button>
          <button className="choice" data-strategy-type="dca" type="button" aria-pressed="false" aria-label="DCA 趋势加仓，未选择">
            {"DCA 趋势加仓"}
            <small>
              {"整轮止盈"}
            </small>
          </button>
        </div>
        <div className="choice-label">
          {"策略方向"}
        </div>
        <div className="choice-row" id="directionChoices">
          <button className="choice" data-direction="neutral" type="button">
            {"中性"}
            <small>
              {"双向震荡"}
            </small>
          </button>
          <button className="choice" data-direction="long" type="button">
            {"做多"}
            <small>
              {"低位加仓"}
            </small>
          </button>
          <button className="choice" data-direction="short" type="button">
            {"做空"}
            <small>
              {"高位加仓"}
            </small>
          </button>
        </div>
        <div className="grid-only" id="gridStyleFields">
          <div className="choice-label">
            {"风格"}
          </div>
          <div className="choice-row two" id="styleChoices">
            <button className="choice style on" data-style="steady" type="button">
              {"稳健"}
              <small>
                {"更宽区间"}
              </small>
            </button>
            <button className="choice style" data-style="aggressive" type="button">
              {"激进"}
              <small>
                {"更密网格"}
              </small>
            </button>
          </div>
          <div className="choice-label">
            {"网格模式"}
          </div>
          <div className="choice-row two" id="gridModeChoices">
            <button className="choice on" data-grid-mode="arithmetic" type="button">
              {"等价差"}
              <small>
                {"相同价差"}
              </small>
            </button>
            <button className="choice" data-grid-mode="geometric" type="button">
              {"等比例"}
              <small>
                {"相同涨跌幅"}
              </small>
            </button>
          </div>
        </div>
        <div className="strategy-mode-note dca-only hidden" id="dcaModeNote">
          {"每次成交都会累积同一方向仓位；系统只保留一张整轮止盈单，并在新增成交后把数量更新为当前总持仓。V1 达到目标或止损后停止，不自动开始下一轮。"}
        </div>
        <button className="button fill" id="smartFill" type="button">
          {"◎ 根据当前行情智能填充 Range"}
        </button>
        <div className="params">
          <label>
            <span className="param-head">
              <span className="label">
                {"加仓 / 网格下边界"}
              </span>
              <button className="param-lock" data-param-lock="range" type="button">
                {"AUTO"}
              </button>
            </span>
            <input className="input" id="rangeLow" inputMode="decimal" />
          </label>
          <label>
            <span className="param-head">
              <span className="label">
                {"加仓 / 网格上边界"}
              </span>
              <button className="param-lock" data-param-lock="range" type="button">
                {"AUTO"}
              </button>
            </span>
            <input className="input" id="rangeHigh" inputMode="decimal" />
          </label>
          <label>
            <span className="param-head">
              <span className="label" id="countLabel">
                {"网格数量"}
              </span>
              <button className="param-lock" data-param-lock="grid_count" type="button">
                {"AUTO"}
              </button>
            </span>
            <input className="input" id="gridCount" inputMode="numeric" />
          </label>
          <label className="param-field grid-only" id="gridProfitField">
            <span className="param-head">
              <span className="label">
                {"每格净利目标（USD）"}
              </span>
              <button className="param-lock" data-param-lock="profit_target" type="button">
                {"AUTO"}
              </button>
            </span>
            <input className="input" id="gridProfitTarget" inputMode="decimal" />
          </label>
          <label className="param-field dca-only hidden" id="dcaTargetField">
            <span className="param-head">
              <span className="label">
                {"整轮止盈价"}
              </span>
            </span>
            <input className="input" id="dcaTarget" inputMode="decimal" />
          </label>
          <label className="param-field dca-only hidden" id="dcaStopField">
            <span className="param-head">
              <span className="label">
                {"止损价"}
              </span>
            </span>
            <input className="input" id="dcaStop" inputMode="decimal" />
          </label>
          <label>
            <span className="param-head">
              <span className="label" id="notionalLabel">
                {"每格名义（USD）"}
              </span>
              <button className="param-lock" data-param-lock="notional_per_grid" type="button">
                {"AUTO"}
              </button>
            </span>
            <input className="input" id="gridNotional" inputMode="decimal" />
          </label>
          <label>
            <span className="param-head">
              <span className="label">
                {"杠杆（x）"}
              </span>
              <button className="param-lock" data-param-lock="leverage" type="button">
                {"AUTO"}
              </button>
            </span>
            <input className="input" id="leverage" inputMode="decimal" />
          </label>
        </div>
        <p className="hint" id="paramHint">
          {"修改任一参数会锁定该值；其余参数自动求解。30–70 格、每格净利 10 USD、10x 杠杆是建议值，不是 Paper 预览硬阻断。"}
        </p>
        <p className="action-status" id="actionStatus" aria-live="polite" />
        <div className="preview-summary hidden" id="previewSummary" data-island="gm-preview" data-props={"{}"} dangerouslySetInnerHTML={{__html: ""}} />
        <div className="robot-actions" id="robotActions" data-island="robot-actions" dangerouslySetInnerHTML={{__html: "<button class=\"robot start\" id=\"startRobot\" data-gm-intent=\"robot.start\" type=\"button\">启动机器人</button><button class=\"robot stop\" id=\"stopRobot\" data-gm-intent=\"robot.stop\" type=\"button\">停止 + 撤单 + 平仓</button>"}} />
        <p className="desk-rail-note">
          {"日常从「今天」面板开始：选方向 → 批准计划 → 亲手按执行。这里是手动细调，按钮的作用和以前一样。"}
        </p>
      </div>
    </section>
  );
}

function SystemDrawer() {
  return (
    <details className="card desk-sys" id="deskSystem">
      <summary>
        <span className="desk-dot" id="deskHealthDot" />
        <span id="deskHealth">
          {"读取中"}
        </span>
        <span className="desk-sys-link">
          {"系统详情：运行状态、策略明细、内部记录"}
        </span>
      </summary>
      <div className="desk-sys-body">
        <div className="desk-records" id="deskRecords" />
        <section className="card current-strategy-card idle" id="currentStrategyCard">
          <div className="current-strategy-head">
            <div>
              <span className="current-strategy-eyebrow">
                {"当前运行策略"}
              </span>
              <h2 id="currentStrategyTitle">
                {"读取中"}
              </h2>
            </div>
            <span className="current-strategy-status" id="currentStrategyStatus">
              {"状态读取中"}
            </span>
          </div>
          <div className="current-strategy-facts" id="currentStrategyFacts">
            <div className="current-strategy-fact">
              <span>
                {"执行身份"}
              </span>
              <b>
                {"等待权威读模型"}
              </b>
            </div>
          </div>
          <div className="current-strategy-evidence" id="currentStrategyEvidence" />
        </section>
        <section className="card live-card rail-status">
          <div className="card-title">
            {"生产运行状态"}
          </div>
          <div className="card-body" id="live">
            {"读取中"}
          </div>
        </section>
      </div>
    </details>
  );
}

function YesterdayPnlCard() {
  return (
    <section className="card yesterday-pnl-card partial" id="yesterdayPnlCard">
      <div className="yesterday-pnl-head">
        <div>
          <span className="current-strategy-eyebrow">
            {"Yesterday PnL · Beijing"}
          </span>
          <h2 id="yesterdayPnlTitle">
            {"读取中"}
          </h2>
        </div>
        <span className="current-strategy-status" id="yesterdayPnlStatus">
          {"证据读取中"}
        </span>
      </div>
      <div className="yesterday-pnl-facts" id="yesterdayPnlFacts" />
      <div className="yesterday-pnl-evidence" id="yesterdayPnlEvidence" />
    </section>
  );
}

function ExternalDcaCard() {
  return (
    <section className="card external-dca-card hidden" id="externalDcaCard">
      <div className="external-dca-head">
        <div>
          <span className="current-strategy-eyebrow">
            {"Hyperliquid Testnet · 外部 DCA"}
          </span>
          <h2 id="externalDcaTitle">
            {"读取中"}
          </h2>
        </div>
        <span className="current-strategy-status" id="externalDcaStatus">
          {"状态读取中"}
        </span>
      </div>
      <div className="external-dca-facts" id="externalDcaFacts" />
      <div className="external-dca-evidence" id="externalDcaEvidence" />
    </section>
  );
}

function AiReceiptDialog() {
  return (
    <dialog className="ai-dialog" id="aiReceiptDialog" aria-labelledby="aiReceiptTitle">
      {"\n    "}
      <div className="receipt-head">
        <h2 id="aiReceiptTitle">
          {"AI 评估收据"}
        </h2>
        <select className="select receipt-history" id="aiReceiptHistory" aria-label="选择历史 AI 评估" />
        <button className="receipt-close" id="closeAiReceipt" type="button" aria-label="关闭">
          {"×"}
        </button>
      </div>
      {"\n    "}
      <div className="receipt-body" id="aiReceiptContent" />
      {"\n  "}
    </dialog>
  );
}

function GridRangeReviewDialog() {
  return (
    <dialog className="ai-dialog grid-review-dialog" id="gridRangeReviewDialog" aria-labelledby="gridRangeReviewTitle">
      {"\n    "}
      <div className="receipt-head">
        <h2 id="gridRangeReviewTitle">
          {"新网格规格 · 最终核对前预览"}
        </h2>
        <button className="receipt-close" id="closeGridRangeReview" type="button" aria-label="关闭">
          {"×"}
        </button>
      </div>
      {"\n    "}
      <div className="receipt-body">
        <p className="grid-review-note">
          {"点击最终按钮前仍是只读草稿；执行时会再次核对计划版本、行情、风险确认、挂单和持仓。人工确认只适用于 Paper。"}
        </p>
        <div className="grid-compare" id="gridRangeComparison" />
        <div className="grid-risk-acks" id="gridRiskAcknowledgements" />
        <div className="grid-review-gate" id="gridRangeGate">
          {"正在核对利润与容量。"}
        </div>
        <div className="grid-review-actions">
          <button className="button subtle" id="cancelGridRangeReview" type="button">
            {"取消草稿"}
          </button>
          <button className="button subtle" id="keepGridRangeDraft" type="button">
            {"返回继续微调"}
          </button>
          <button className="button replace-grid" id="executeGridRangeReplacement" type="button">
            {"停止+平仓+撤单+交易新网格"}
          </button>
        </div>
      </div>
      {"\n  "}
    </dialog>
  );
}

function StartRiskDialog() {
  return (
    <dialog className="ai-dialog grid-review-dialog" id="startRiskDialog" aria-labelledby="startRiskTitle">
      {"\n    "}
      <div className="receipt-head">
        <h2 id="startRiskTitle">
          {"新网格参数 · 风险确认"}
        </h2>
        <button className="receipt-close" id="closeStartRisk" data-gm-intent="startRisk.close" type="button" aria-label="关闭">
          {"×"}
        </button>
      </div>
      {"\n    "}
      <div className="receipt-body">
        <p className="grid-review-note">
          {"这些是 Paper 策略偏好与资金风险，不是静默改参。逐项确认后，后台仍会再次校验行情、账户、执行身份与精度。"}
        </p>
        <div className="start-risk-review" id="startRiskReview" data-island="start-risk" dangerouslySetInnerHTML={{__html: "<div class=\"grid-compare\" id=\"startRiskComparison\"></div><div class=\"grid-risk-acks\" id=\"startRiskAcknowledgements\"></div><div class=\"start-blocker-list\" id=\"startRiskBlockers\"></div><div class=\"grid-review-gate\" id=\"startRiskGate\"></div><div class=\"grid-review-actions\"><button class=\"button subtle\" id=\"cancelStartRisk\" data-gm-intent=\"startRisk.close\" type=\"button\">返回修改</button><button class=\"button replace-grid\" id=\"confirmStartRisk\" data-gm-intent=\"startRisk.confirm\" type=\"button\" disabled=\"\">确认风险并启动机器人</button></div>"}} />
      </div>
      {"\n  "}
    </dialog>
  );
}

function StartFailureDialog() {
  return (
    <dialog className="ai-dialog start-failure-dialog" id="startFailureDialog" aria-labelledby="startFailureTitle" aria-describedby="startFailureReason">
      {"\n    "}
      <div className="receipt-head">
        <h2 id="startFailureTitle">
          {"启动未完成"}
        </h2>
        <button className="receipt-close" id="closeStartFailure" type="button" aria-label="关闭">
          {"×"}
        </button>
      </div>
      {"\n    "}
      <div className="receipt-body start-failure-body">
        <p className="start-failure-reason" id="startFailureReason" role="alert">
          {"后台拒绝了启动请求。"}
        </p>
        <p className="start-failure-detail">
          {"没有创建新网格，生产状态没有改变。你可以关闭后修改参数，再重新启动。"}
        </p>
        <div className="start-failure-actions">
          <button className="button subtle" id="dismissStartFailure" type="button">
            {"返回修改"}
          </button>
        </div>
      </div>
      {"\n  "}
    </dialog>
  );
}

export function Skeleton() {
  return (
    <>
      {"\n  "}
      <TopBar />
      {"\n  "}
      <div className="trade-toast-stack" id="tradeToasts" aria-live="polite" aria-atomic="false" />
      {"\n  "}
      <div className="desk-layout">
        <section className="desk-status wait" id="deskStatus" aria-live="polite" />
        <div className="desk-retired hidden" id="deskRetired" role="status" />
        <section className="card desk-watch" aria-labelledby="deskWatchTitle" data-island="watch" dangerouslySetInnerHTML={{__html: ""}} />
        <aside className="card desk-news" aria-labelledby="deskNewsTitle" data-island="news" data-props={"{}"} dangerouslySetInnerHTML={{__html: ""}} />
        <div className="console-wrap">
          {"\n    \n    "}
          <YesterdayPnlCard />
          {"\n    "}
          <ExternalDcaCard />
          {"\n    "}
          <section className="metrics" aria-label="账户指标">
            <div className="account" id="account" data-island="gm-cards" data-props={"{}"} dangerouslySetInnerHTML={{__html: ""}} />
          </section>
          {"\n    "}
          <main className="workspace">
            {"\n      "}
            <section className="market-column">
              {"\n        "}
              <ChartCard />
              {"\n        "}
              <DataCard />
              {"\n      "}
            </section>
            {"\n      "}
            <aside className="control-rail">
              <TodayCard />
              <details className="card desk-more" id="deskManual">
                <summary>
                  {"手动配置与 AI 助手"}
                  <small>
                    {"Trading BOT · AI 助手 · 趋势评估 · 策略配置与停止"}
                  </small>
                </summary>
                <div className="desk-more-body">
                  <ControlCard />
                  <AiChatCard />
                  <MarketCard />
                  <StrategyCard />
                </div>
              </details>
              {"\n        \n        \n        \n        \n        \n      "}
            </aside>
            {"\n    "}
          </main>
          {"\n    "}
          <div className="hidden" aria-hidden="true">
            <table id="baselineFills" data-island="exec-tables" data-props={"{\"table\":\"baseline\"}"} dangerouslySetInnerHTML={{__html: ""}} />
          </div>
          {"\n  "}
          <SystemDrawer />
        </div>
      </div>
      {"\n  "}
      <AiReceiptDialog />
      {"\n  "}
      <GridRangeReviewDialog />
      {"\n  "}
      <StartRiskDialog />
      {"\n  "}
      <StartFailureDialog />
    </>
  );
}
