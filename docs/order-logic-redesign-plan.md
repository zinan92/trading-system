# 下单逻辑改造 · 整体规划

来源：`park-io/003_park原始输出/交易哲学和逻辑/交易系统qa逻辑-gotchas.md`（14 条 gotcha + 元反思）
状态：**规划，不含任何代码改动**。所有 file:line 引用已对当前代码核对（`feat/autonomy-remaining` 分支）。
更新：2026-07-04

---

## 0. 指导原则（gotcha #15）

每一个闸门、每一个字段，先回答五问，再决定它是否该存在、放在哪一层：

1. 这是**市场判断**，还是**系统健康**？
2. 这是**策略信号**，还是**执行容错**？
3. 这是**真实评分**，还是**接口兼容字段**？
4. 这是**允许提交**，还是**已经成交**？
5. 这条规则会不会让**正确的交易变小、变短、变得没有意义**？

整份计划就是把这五问贯彻到管道每一层。

---

## 1. 目标形态：五层管道

文档 #4 / #15 要求的分层顺序（当前是混层的）：

```
① 系统 / 数据健康  →  ② 策略信号  →  ③ 交易结构  →  ④ 账户风险  →  ⑤ 执行状态
```

当前代码 vs 目标：

| 层 | 相关 gotcha | 当前代码位置 | 当前问题 | 目标 |
|----|------------|-------------|---------|------|
| ① 健康 | #4 #5 #6 #7 #8 #9 | `services/data_quality_gate.py`；`pipelines/daily.py:175-186` | 执行顺序其实已经先跑（见下方要点），但**展示层**把信号排在健康前面；健康失败被写成 `no_signal`（像"信号失败"）；synthetic 容忍 20%；无 provider 硬约束；无数据时效闸门 | 一个布尔 `market_data_health == pass` 最先跑；细节进 health report；失败=**本轮不可判定**，不是信号失败 |
| ② 信号 | #1 #2 #3 | `services/macd_signal_engine.py:30-99` | `fresh_bars=3` 把调度容错混进信号；`strength=70/confidence=60` 固定假分；`watch`+`no_signal` 重复 | 二值信号 + 干净的 `direction/status/reason` 状态模型 |
| ③ 结构 | #10 #12 | `services/risk_engine.py:201-248` | 止损锚 = 交叉那根 K 的 low/high；止盈 = 固定 `entry ± 1.5×risk` | 止损/止盈来自**市场结构**；1.5R 变成**闸门**不是目标 |
| ④ 风险 | #10 #11 #13 | `risk_engine.py:67-70,159-199`；`risk_rules.yaml`；`risk_monitor.py:87-110` | 固定 8% 仓位 + 三重 notional 封顶 → 名义太小；`max_loss 0.5%` / `daily 1.25%` 偏保守且互相打架；杠杆语义不一致 | 名义 ≥ 50% AUM 下限；风险预算反推仓位；`max_loss`/`daily` 一起重设；杠杆统一 |
| ⑤ 执行 | #14 | `risk_engine.py:120`；`paper_executor.py:24-107`；`broker_adapter.py:450-660` | `order_type="limit"`；paper 入场区间 = 最新价到最新价→立即成交；demo LIMIT 可能 `submitted_to_binance` 挂着 | 明确 market vs limit；"允许提交"≠"已成交"，UI/回执必须诚实；paper/demo 对称 |

### 三个必须先内化的事实

1. **#4 主要是"展示顺序"问题，不是"执行顺序"bug。** 实际管道 `pipelines/daily.py:176` 已经先跑数据质量闸门，数据坏时把信号降级成 `direction=watch / regime=data_quality_block`，之后才轮到 `risk_engine` 的信号阈值闸门（`daily.py:260`）。要改的是：决策 trace / 飞书卡片把 `信号闸门` 排成 step 1、`数据质量闸门` 排成 step 2（见 `docs/decision-trace-spec.md`），顺序要倒过来；且健康失败要显示成"本轮不可判定"而非 `no_signal`。

2. **③结构 + ④风险是一个耦合系统，不是四条独立改动。** 对线性品种，止损处亏损 = `名义 N × 止损距离 d`。于是 #10（可变结构止损→d 可变）、#11（名义下限 `N ≥ 0.5E`）、#12（结构止盈 + 1.5R 闸门）、#13（亏损上限 `N·d ≤ 0.02E`）被同一条等式绑死：
   - 下限：`N ≥ 0.5E`
   - 上限：`N·d ≤ 0.02E`
   - ⟹ 只有 `d ≤ 4%` 时可行；把风险预算用满的仓位是 `N = 0.02E / d`。
   任何一个单独改都会让系统内部自相矛盾。必须作为**一个工作流**设计。

3. **杠杆/名义语义现在是裂开的（已从代码坐实，不要沿用记忆里的"5×"）。**
   - `trade_quality.py:40` dataclass 默认 `effective_leverage=5.0`，但 `risk_rules.yaml` 覆盖成 `10` → **实际生效 10×**。
   - 风险闸门算的是 `account_stop_risk = d × leverage × position_fraction = d × 10 × 0.08 = d × 0.8`（`trade_quality.py:113` + `risk_engine.py:178`）。
   - paper 真实建仓 `_quantity` 是 `notional = equity × 8%`，**完全不带杠杆**（`paper_executor.py:802-804`）→ 真实亏损只有 `d × 0.08`。
   - **风险闸门比真实 paper 仓位保守整整 10×**。这就是 [[position-sizing-margin-notional-inconsistency]] 的代码根因。动任何仓位/风险数字前，必须先把"名义到底带不带杠杆、caps 是名义还是保证金"这一套语义定死。

4. **对 1m gold，绑死仓位的是 caps，不是亏损上限。** 1m 结构止损很小（d ≈ 0.1%–0.5%），风险预算反推会**想要很大的名义**，`N·d ≤ 0.02E` 几乎永不触发。真正把名义压到远低于 50% AUM 下限的是三重封顶：`position_size_pct=8%`、demo `max_order_quantity=0.002`（4180 价位≈ $8.36 名义）、live `single_order_max_notional=10`。**所以对本策略，#11（抬 caps）比 #13（抬 max_loss）杠杆更高、更该先做**，尽管 #13 听起来更吓人。

---

## 2. 决策点（owner 必须先拍板的 fork）

这些是风险哲学，计划给建议、owner 定稿。锁定前不进 Phase 2。

| # | 决策 | 选项 | 建议 |
|---|------|------|------|
| **D1** 仓位模型 | #11/#13 | (a) **风险预算反推**：`N = 风险预算/d`，名义下限 + 杠杆/交易所 caps 作为可行性闸门；(b) 维持**固定 8%**，其它都当拒绝闸门 | **(a) 风险预算反推 + 名义下限**。当前固定 8% 在"止损可变 + 亏损硬顶"下逻辑上不自洽。不可行情形（`d>4%` 或 capped-N 低于下限）→ **显式拒绝并在 decision trace 写清原因**，绝不静默缩仓。这条拒绝规则正是文档"正确但无意义的交易"恐惧的胜负手。 |
| **D2** 入场类型 | #14 | (a) **market 入场** + 保护性 stop/TP 挂单；(b) **limit 入场**，接受 pending/submitted 状态 | 倾向 **(a) market**（信号通过即入场，符合动量策略直觉），但**前置**是先补上"入场成交与保护单同路 fail-closed"不变式（见 §4）。若暂不做该不变式，则先留 **(b) limit** 并在 UI 诚实显示"已提交未成交"。 |
| **D3** 信号评分 | #2 | (a) 真实评分（成交量/波动/斜率/交叉幅度/上级周期）；(b) `signal_passes: true/false` + score 标为 compat 字段 | **先 (b) 二值 + compat，后 (a)**。当前 70/60 是假分，先诚实成二值，真实评分作为后续质量提升（Phase 4）。 |
| **D4** 风险数字 | #11/#13 | 单笔 `max_loss` 0.5%→**2%**；日亏 1.25%→**4%**；名义下限 **≥50% AUM**；并发按**相关性**限制而非纯数量 | 采纳文档数字为默认，但 owner 明确签字。**注意**：2% 单笔 + 4% 日亏意味着理论上第一单可占日预算一半——需同时定义并发/相关性规则。 |

---

## 3. 四个改造工作流

### W1 — 健康层（#4 #5 #6 #7 #8 #9）· 低风险，可先做

把所有"数据/系统健康"检查收敛成开单前**最先跑的一个布尔** `market_data_health == pass`，闸门只引用布尔，细节全部进 health report。

- **展示重排（#4）**：decision-trace / 飞书卡片把 `数据质量/健康` 放到 step 1，`信号` 放到 step 2。健康失败的 `outcome`/文案改成"**本轮不可判定**"，不再复用 `no_signal`。改 `services/decision_trace.py` 的 gate 顺序与 `TradeTicketNotifier._build_view`。
- **missing bars 归类（#5）**：`data_quality_gate.py` 的 `missing_ratio` 明确标注为 *pipeline health*，措辞不得暗示"市场信号质量差"。
- **synthetic 收紧（#6）**：对 demo/live，`max_synthetic_ratio` 从 0.2 → **要求 `synthetic_rows == 0`**；`allow_latest_synthetic` 保持 False。synthetic 只允许出现在测试 fixture / 冷启动 / 明确标记的 shadow。
- **provider 硬约束（#7）**：新增——`gold_1m_macd` 的 latest 1m bar `provider` 必须 == `binance_usdm`，否则 `market_data_health=fail`。当前只记录 `latest_provider`，不强制。
- **数据时效闸门（#8）**：新增——`feed_freshness_ok`：latest bar `age_minutes` 超阈值即 fail。当前**没有任何显式数据时效闸门**（两个探查 agent 均确认）。细节（`age_minutes`/`latest_timestamp`/`collector_status`）进 health report。
- **quote/bar 偏差简化（#9）**：若 `gold_1m_macd` 锁定单一 Binance 源，quote/bar 偏差检查可简化甚至移除；保留多源校验时必须声明 quote provider 与 bar provider 是否必须一致。→ 归到 Phase 4。

**共用产物**：健康层与信号层的 `reason` 枚举**统一定义一次**，跨两层：`data_unhealthy_unevaluable`（本轮不可判定）与 `no_macd_cross` / `stale_cross` 并列，而不是又搞一个 `no_signal`。

### W2 — 信号诚实层（#1 #2 #3）· 低/中风险

- **fresh_bars 正名（#1）**：`macd_signal_engine.py:68-70` 的 `fresh_bars=3` 是**调度容错**混进信号逻辑。二选一：
  - 若调度可靠 → 收紧成 `cross_index == len(bars)-1`（或 `-2`），并明确是否用未完成 K；
  - 若保留容错窗口 → 重命名为 `scheduler_lag_tolerance_bars`，避免被理解成"策略需要回看 3 根"。
- **假分诚实化（#2）**：`macd_signal_engine.py:73-85` 固定 `strength=70/confidence=60`，阈值 `60/55`（`risk_rules.yaml:7-8`），新鲜交叉必过。先按 **D3(b)** 输出 `signal_passes: true/false`，把 `strength/confidence` 标为 compatibility field（保持 `approved_candidate` 接口不破）。
- **状态模型清洗（#3）**：`macd_signal_engine.py:91-99` 的 `direction=watch / status=no_signal / regime=no_trade` 高度重复。收敛成：
  - `direction = long | short | none`
  - `status = candidate | blocked | no_setup`
  - `reason = no_macd_cross | stale_cross | data_quality_block(=data_unhealthy_unevaluable)`
  这是 schema 变更（`schemas/signal.py`），需同步 `decision_trace` / 卡片 / 下游读取。

### W3 — 结构 + 仓位 + 风险预算（**耦合系统**：#10 #11 #12 #13）· 高风险，Phase 2

按 §1 要点 2 的等式统一设计。分两块：

**(a) 结构化止损/止盈（#10 #12）**
- 止损：不再是 `anchor.low/high`（`risk_engine.py:215/227`）。做多 `stop = min(low of lookback structure around cross)`，做空 `stop = max(high...)`，`N` 先从 5 或 10 根起步，后续用缠论顶底分型升级（Phase 4，可复用 `gold_1m_chan` 引擎）。
- 止盈：不再固定 `entry ± 1.5×risk`（`risk_engine.py:213-237`）。做多目标 = 结构前高/压力位，做空 = 结构前低/支撑位。
- **1.5R 变闸门不变目标**：`reward_to_risk = 结构目标 R 值`；`reward_to_risk >= 1.5` 作为 gate（复用 `trade_quality.min_reward_to_risk`）。结构目标 <1.5R → **不做**；能到 3R → **不许被 1.5 截断**。

**(b) 仓位与风险预算（#11 #13 + D1 + D4）**
- 先**统一杠杆/名义语义**（§4 前置）：定死一个规范模型——`loss = N × d`，`N` 与 `E` 的关系（仅 `f`，还是 `f×L`）唯一化，`risk_engine._with_position_risk` 与 `paper_executor._quantity` 必须同源，消除 10× 裂缝。
- 采用 **D1(a)**：`N = 风险预算 / d`（风险预算 = `max_loss_pct × E`）。
- 名义下限闸门（#11）：`nominal_value >= account_equity × 0.5`。
- **同时**抬三重 caps（§1 要点 4）：`position_size_pct`、demo `max_order_quantity=0.002`、live `single_order_max_notional=10`（连带 `reference_equity=100`）。**否则名义下限闸门永久失败**——这是本策略最高杠杆的一步。
- 风险数字（#13, D4）：`max_loss` 0.5%→2%、`daily` 1.25%→4%、并发按相关性。**前置**：daily-loss 闸门必须先 fail-closed（§4）。
- 不可行情形（`d>4%` 或 capped-N 低于下限）→ 显式拒绝 + trace 写原因，**不静默缩仓**。

### W4 — 执行真相层（#14）· 高风险，Phase 3

- 按 **D2** 定 market vs limit。
- 若 market：`risk_engine.py:120` 的 `order_type="limit"` 改 market 入场，保护性 stop/TP 作为独立挂单；**前置**"同路 fail-closed"不变式（§4）。
- 若 limit：接受 pending/submitted，UI/回执明确"已提交未成交"。当前 paper 因入场区间=最新价到最新价而**立即成交**（`paper_executor.py:36-37`），demo LIMIT 却可能 `submitted_to_binance` 挂着（`broker_adapter.py:611`）——**paper/demo 不对称必须消除**。
- **QA 通过 = 允许提交，绝不等于已成交**：回执/卡片/trace 的状态语义要与真实 broker 状态一致，`executedQty=0` 时不得显示成 filled。

---

## 4. 前置依赖闸门（危险改动的硬阻塞）

每个危险改动在其前置落地前**禁止上**：

| 危险改动 | 前置（必须先做） | 关联记忆 |
|---------|-----------------|---------|
| `max_loss` 0.5%→2% / `daily` 1.25%→4%（W3b, D4） | **daily-loss 闸门先做成 fail-CLOSED**（NaN / local-vs-UTC run_date / stale artifact 都要拦），否则等于抬高一个"不可靠触发"的上限 | [[daily-loss-guardrail-failopen-class]] |
| limit → market 入场（W4, D2a） | **入场成交 → 保护单同路 fail-closed 不变式**（market 成交更快，裸仓窗口更宽） | [[naked-position-fill-attach-window]] |
| 任何名义提升 / caps 抬升（W3b, #11） | **杠杆/名义语义统一**（先消除 10× 裂缝，定死 caps 是名义还是保证金） | [[position-sizing-margin-notional-inconsistency]] |

---

## 5. 分阶段路线

对齐 [[project-direction]]（优先安全上真金）与 [[backend-maturity-roadmap]]（回测降级、2 笔/天风险帽、盈利策略未证实前不放开）。

**Phase 1 — 前置与低风险（安全、可立即做）**
- daily-loss 闸门 fail-closed（W3 前置）
- 杠杆/名义语义统一 + paper/gate 同源（W3 前置）
- 入场→保护单同路 fail-closed 不变式（W4 前置）
- 展示重排 + reason 枚举统一（W1 展示部分 + W2 状态模型）——纯观测/展示，零执行风险

**Phase 2 — 让交易有意义（owner 锁定 D1/D4 后）**
- 结构化止损/止盈 + 1.5R 闸门化（W3a）
- 风险预算反推仓位 + 名义下限 + 抬三重 caps + 风险数字重设（W3b）
- 健康层硬约束：synthetic=0、provider=binance_usdm、feed freshness（W1）
- **先在 demo/shadow 跑，2 笔/天帽不动**

**Phase 3 — 执行升级（owner 锁定 D2 后）**
- market 入场 + 诚实 submit/fill 状态 + paper/demo 对称（W4）

**Phase 4 — 质量提升（非阻塞，后置）**
- 真实信号评分（D3a, #2）
- 缠论顶底分型升级结构止损（#10 二阶段）
- quote/bar 偏差简化（#9）
- fresh_bars 收紧到 last-closed（#1，若调度证明可靠）

---

## 6. 风险 / mainnet-blocker 交叉

本次改造直接踩到三个已知 mainnet-blocking 类（见 §4 表）。计划的核心安全策略就是：**把这三个的 fail-closed 前置排进 Phase 1，风险数字与执行模式的放开排到 Phase 2+ 且显式 gated**。任何一个前置没落地，对应的危险改动不许进 demo→live。

另外每一条"缩小/缩短交易"的规则（名义下限、RR 闸门、caps）都必须在 decision trace 里留下**显式拒绝原因**，直接回应文档反复强调的"正确但无意义的交易样本"担忧。

---

## 7. 明确不改 / 开放问题

**本阶段明确不改**：多资产/多交易所适配（[[plug-and-play-adapter-architecture]]，小闭环打磨完再说）；回测深挖（已降级）；引入"人工审批"中间态（[[autonomous-decision-taxonomy]]：无 manual-approve 中间态，待人工=死角/bug 不是策略）。

**留给 owner 的开放问题**：
1. D1–D4 四个 fork 的定稿。
2. 名义模型到底是**现货无杠杆**（`N=E·f`，杠杆不进风险闸门）还是**杠杆/保证金**（`N=E·f·L`，paper `_quantity` 需补 ×L）？两者都能自洽，但必须唯一化——这决定 §1 要点 3 那条裂缝往哪个方向合。
3. 并发/相关性规则的具体形式（#13"同时最多 open trades 按相关性"目前无实现）。
4. 数据时效阈值 `age_minutes`、结构 lookback `N`、feed freshness 具体秒数——建议按 1m 实际调度节奏标定并写进 config，不硬编码。
