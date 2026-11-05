# Codex 任务 08:standard-kline 指标支持(v0.2)+ 作战台图表与作战单构图修复

> 背景:所有者用真实数据挑出五个问题:①主图同价位价格线标签堆叠(4155 三张);②作战单卡构图拥挤+死空档;③K线无 EMA/MACD(standard-kline 包从 v0.1.0 起就没有指标功能;replay-v4 的 EMA/MACD 是该页自己的旧实现,与包无关);④context 时间框写死 15m/1h 不可选;⑤context 图 150px 压扁+header meta 截断。
> 你(Codex)严格按本规格实现。规格未覆盖的决策,选最保守做法并在 PR 里列出。
>
> ## 三条不可违反的硬约束
> 1. **盲答协议**:指标(EMA/MACD)只能由 bars 序列纯计算得出,严禁引用任何机器轨数据;盘中主图仍只喂 `state.human.fills`;context 图仍然零标记零指标标注(可以有 EMA 线,因为它是行情的纯函数,但不得有任何成交/点位标记)。既有盲答回归断言必须全绿。
> 2. **decision-log.md 追加末尾**。
> 3. **design-token 纪律**:`tests/test_design_tokens_static.py` 的颜色白名单必须保持全绿——页面新增的任何颜色只能用既有 token;standard-kline 包内新增指标默认色必须取 token 的字面值(包文件不在白名单测试范围内,但值必须一致,PR 列出所用值)。

## Part A — packages/standard-kline v0.2:EMA + MACD

这是独立 npm 包(所有者的标准化组件产品),API 设计要包级通用,不掺作战台业务概念。

1. **纯计算函数并导出**(可单测):`computeEma(candles, period)`、`computeMacd(candles, {fast=12, slow=26, signal=9})`(返回 `{macd[], signal[], histogram[]}`,时间对齐 candles)。
2. **API**:`setAdaptedData(adapted, overlays)` 的 `overlays` 新增可选 `indicators`:
   ```js
   indicators: {
     ema: [{period:20}, {period:50}],          // 主图叠加线,最多 3 条
     macd: {fast:12, slow:26, signal:9}         // 独立副窗格
   }
   ```
   未传 = 现状不变(向后兼容,零破坏)。
3. **EMA 渲染**:主价格窗格叠加 line series。默认色序(token 字面值):`#d8aa3f`(gold)、`#7aa2ff`、`#b894ff`;允许每条显式传 `color` 覆盖。
4. **MACD 渲染**:副窗格(histogram 用 `rgba(53,208,127,.5)`/`rgba(239,95,95,.5)` 正负,macd/signal 线用 `#9aa3ad`/`#e8eaed`)。**先查 vendored 的 `data/vendor/lightweight-charts.standalone.production.js` 版本**:v5 支持 panes(`addPane`/series 的 paneIndex);若 vendored 版本确实不支持,把方案(升级 vendored 文件 vs 组件内第二画布)连同理由写进 PR,选保守者实施,不许硬凑。
5. **包内测试**(`standard-kline.test.js`,node --test):EMA/MACD 数学对手算 fixture(≥8 根K线,预先手算好期望值写死在测试里);indicators 未传时行为与 v0.1 一致的回归断言。
6. `package.json` 版本 0.1.0→0.2.0;README 增补 indicators 文档与示例。

## Part B — 作战台修复(dashboard-dualtrack-v5.html)

### B1 · 主图同价位语义合并(4155 堆叠的正解)
新增纯函数 `mergeSamePriceLines(lines, thresholdPct=0.0005)`:价位落在同一 band 的多条线**合并为一条**,title 拼接(如 `range low · level · invalid 4,155`),颜色按优先级取最高危:invalid(`--block`)> range(`--human`)> level(muted)。应用于主图 `buildHumanPlanPriceLines(...).concat(buildTrackFillPriceLines(...))` 的结果。
注意与机器卡现有 `dedupePriceLinesByBand`(丢弃式)的区别:这里语义不同的线**信息要保留**(拼 title),不是丢弃。机器卡可保持现状或统一改用 merge,你选,PR 说明。

### B2 · 图表 header meta 清理
主图与 context 的 `panel-h .right` 只显示短徽章:`5m · 派生自1m · fresh` 这个量级;完整技术串(provider/source_mode/derived_timeframe...)移到 `chart-foot` 或元素 `title` 属性。`.panel-h .right` 补 `min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap` 兜底。

### B3 · Context 时间框可选
每个 context 槽位加 tf-seg(选项:`15m / 30m / 1h / 4h`),默认槽1=15m、槽2=1h;选择存 localStorage(键名带槽位),刷新保持。数据端点已支持任意 timeframe(派生),无后端改动。**context 仍然零标记**;可以给 context 加一条 EMA50(用 Part A 的 indicators,颜色 muted 系)——这是"大级别定方向"的正用,但止于 EMA,不加 MACD 不加任何点位。

### B4 · Context 高度与空档
`.context-body` 150px→240px;检查 context 区与下一节("周期结束·对账复盘")之间的垂直空档,收敛到 `--sp-5`(24px)量级,消除大片死黑。

### B5 · 作战单卡构图
1. `.plan-card` padding `12px 16px`→`16px`(--sp-4 全边)。
2. `.plan-grid` gap `8px`→`12px`(--sp-3)。
3. `.plan-metric` 背景修正:`var(--rule)`(半透明白,当初手误)→ `var(--panel2)`,与批准提案的 Before/After 示例一致。
4. **死空档**:`.blind-grid` 两卡不再强制等高拉伸(`align-items:start`),或给短内容卡合理的内容分布;总之 AI 卡下方不许再有大片空底。
5. AI 卡的"注意:fallback"提示行与格子左对齐、间距用 --sp 阶梯。

### B6 · 主图接入指标(作战台)
主图 overlays 传 `indicators:{ema:[{period:20},{period:50}], macd:{...}}`,并在图表工具条加 EMA/MACD 开关(默认 EMA 开、MACD 关;状态存 localStorage)。指标只算自 bars——见硬约束 1。

## 测试(先红后绿)

`packages/standard-kline/standard-kline.test.js`:EMA/MACD 数学 + 向后兼容回归。
`tests/test_dashboard_dualtrack_static.py` 追加:
1. `mergeSamePriceLines` 存在且应用于主图 overlay 构建路径
2. context tf-seg 存在(两个槽位、四个选项)
3. `.context-body` 高度 240
4. `.panel-h .right` 含 ellipsis 兜底样式
5. `.plan-metric` 背景不再是 `var(--rule)`
6. 盲答回归:主图/context 渲染路径仍不含 `state.machine`(既有断言保持)
`tests/test_standard_kline_adapter.py`(node 桥接测试)如有必要同步。
全仓 `python3 -m pytest -q` 全绿;`node --test packages/standard-kline/` 全绿;R4 锁零 diff。

## 验收(设计师级,不只工程师级)

浏览器用**真实数据**逐区挑刺并截图进 PR:
- 主图右侧价格轴:同价只有一张合并标签,title 可读
- EMA 两条线可见、MACD 开关后副窗格出现且主图不被压扁(主图+MACD 总高合理)
- context:选 30m/4h 后真实切换且刷新保持;header 完整可读;240px 高度下 K 线不再压扁;区块下方无大片死黑
- 作战单卡:格子有呼吸感、无死空档、背景为 panel2
- 五页 console error = 0;design-token 白名单测试绿

## 硬约束(重申)

- 不碰后端/订单/执行/broker/风控;不引入 CDN/构建步骤;R4 零 diff。
- standard-kline 包保持 provider-agnostic:不许出现 dualtrack/human/machine 等业务词。
- 规范页 `mockups/design-tokens-proposal.html` 不许改。
- decision-log 记录:包 v0.2 API 设计、MACD 窗格技术选型、merge 与 dedupe 的语义区分、构图修复清单。

## DOD

- [ ] 包测试 + 静态测试 + 全仓全绿
- [ ] 设计师级验收截图逐区附 PR
- [ ] 盲答与 token 白名单回归全绿
- [ ] PR 列出所有自行决定的点(尤其 MACD 窗格选型、机器卡是否统一用 merge)
