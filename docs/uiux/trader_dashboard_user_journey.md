# 黄金交易 Dashboard UI/UX 规划

日期：2026-06-28  
范围：桌面端 Trader / PM 视角；暂不考虑移动端；暂不改生产页面。

## 目标

这轮不是继续“删内容”，而是重新做信息分层：

- Trader 需要在 5 秒内知道：现在赚不赚钱、有没有风险、该看哪笔交易。
- PM 需要在 30 秒内知道：哪些策略值得继续看、哪些策略卡住、今天该复盘什么。
- OPS 信息必须能查，但不抢主页面注意力。

最终原则：

```text
主页面展示决策信息
折叠菜单展示复盘证据
OPS 页面展示系统诊断
```

## 用户旅程

### Journey 1：PM 打开驾驶舱

目的：判断组合今天有没有赚钱，谁贡献了收益，是否有风险阻断。

用户路径：

1. 看顶部状态：交易是否允许、数据是否新鲜、是否有未处理安全问题。
2. 看组合区：策略 NAV vs 黄金基准，今日 PnL，已平仓 / 未平仓分开。
3. 看策略排行榜：按已实现 PnL、样本数、风险状态排序。
4. 点击一个策略，进入策略页或 Replay。

首屏必须展示：

- 全组合 NAV / 黄金基准图
- 今日真实成交数
- 今日已实现 PnL、未实现 PnL
- 策略榜 Top / Bottom
- 当前最大 blocker

可以折叠：

- NAV 图读法
- 策略明细解释
- 今日行动队列的完整列表

只给 OPS：

- API payload 状态
- collector runs
- raw artifact path
- internal audit 字段

### Journey 2：Trader 看某个策略

目的：判断这个策略今天为什么赚钱 / 不赚钱，下一步该复盘哪笔。

用户路径：

1. 进入策略页。
2. 先看策略卡：今日交易数、PnL、胜率、状态。
3. 看交易列表：每笔 entry / TP / SL / current / exit。
4. 点一笔交易进入 Replay。

首屏必须展示：

- 策略名、策略类别、交易状态
- 今日成交数、已平仓数、持仓数
- 已实现 PnL、未实现 PnL
- 最近 3-5 笔交易
- “下一笔该复盘什么”

可以折叠：

- 完整策略列表
- 阻断归因细节
- 参数和配置摘要

只给 OPS：

- strategy artifact path
- raw JSON schema
- backend diagnostics

### Journey 3：Trader 做单笔交易复盘

目的：看懂一笔交易的完整病历：为什么进、为什么出、TP/SL 是否合理。

用户路径：

1. 进入交易索引或 Replay。
2. 先看主图和单笔病历。
3. 如果需要上下文，再看 1D / 4H / 15m / 5m。
4. 如果不懂为什么 No-Go，展开观望原因。
5. 如果怀疑系统问题，再去 OPS。

首屏必须展示：

- 主 K 线图
- context K 线图
- 当前 K 线决策：Go / No-Go / Blocked
- Entry / TP / SL / Exit
- 观望或成交原因

可以折叠：

- 信号过滤链路
- 技术指标横截面
- 复盘归因表
- 其他交易

只给 OPS：

- 同步状态
- raw decision snapshot
- replay boundary debug
- artifact health

## 信息分层规则

### Always Visible

这些信息直接影响交易或 PM 决策，应该在主页面展示：

- NAV 曲线
- 黄金基准
- 今日真实成交数
- 已实现 PnL / 未实现 PnL
- 策略状态
- 每笔交易 Entry / TP / SL / Exit
- 当前 blocker
- 当前复盘任务

### Collapsed By Default

这些信息有用，但不是第一眼必须看：

- NAV 图读法
- 策略完整明细
- 单笔交易完整病历
- 过滤链路
- 技术指标横截面
- 复盘归因
- 其他交易
- 参数摘要

### Hidden From Trader, OPS Only

这些信息会帮助排查，但会打断交易员注意力：

- raw artifact path
- API shape
- collector run detail
- dashboard server payload
- schema / audit wording
- debug boundary
- stale source trace

## 页面结构建议

### A. 黄金交易驾驶舱

主任务：PM 看组合。

```text
[Top status: trading allowed / blocker / data freshness]

[Portfolio NAV vs Gold benchmark]

[Today book]
  Realized PnL | Unrealized PnL | Trades | Open Risk | Blocker

[Strategy leaderboard]
  Strategy | Category | Trades | Realized | Unrealized | Edge label | Next action

[Review queue]
  Only top 3 visible, rest collapsed
```

### B. 策略监控台

主任务：Trader / PM 看策略。

```text
[Strategy selector + category filters]

[Selected strategy summary]
  NAV | Today PnL | Trades | Positions | Risk | Status

[Recent trades]
  Entry | TP | SL | Exit | PnL | Compliance | Replay

[Attribution]
  Why made money / lost money / no trade
```

### C. 多周期 Replay

主任务：单笔复盘。

```text
[Replay controls]

[Main chart: selected timeframe]
[Context charts: 2x2]

[Decision strip]
  Go/No-Go | Direction | Entry | TP | SL | Reason

[Trade medical card]
  Collapsed detail: indicators, filters, attribution, raw notes
```

## 需要合并的区域

- 当前 K 线决策 + 交易病历 + 观望原因：合并为一个 `Decision / Trade Card`。
- PM 证据检查 + 复盘材料 + 记录完整性：合并为 `Review Confidence`，默认折叠。
- 其他交易 + 图上标注：合并为 `Trade Tape`，主图旁边保留短入口。
- 市场背景 + 多周期定位：合并为 context chart 的 hover / tooltip，不单独铺满一块。

## 不应该继续做的事

- 不再无目标地把内容继续隐藏。
- 不再按 OPS payload 结构生成前端区块。
- 不在 Trader 首屏展示 raw artifact、schema、backend audit。
- 不为每个 backend 字段都做一个 card。

## Mockup 验收标准

- 桌面端 1728px 宽度下，首屏只有一个视觉焦点。
- 用户不滚动也能回答：今天组合如何、看哪笔交易、哪里有风险。
- 每个页面最多 3 个主区域。
- 折叠区必须是“有用但二层”的信息，不是垃圾桶。
- OPS 信息必须存在入口，但不在 Trader 主路径里出现。

