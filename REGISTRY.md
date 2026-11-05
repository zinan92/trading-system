# trading-system

## Operating documents (2026-08-02)

- Stable intent and completion evidence: [`NORTH_STAR.md`](NORTH_STAR.md).
- Current handoff snapshot: this file (`REGISTRY.md`); do not duplicate its
  history in the North Star.
- Material daily deltas: [`daily/`](daily/), using
  [`daily/YYYY-MM-DD.md`](daily/YYYY-MM-DD.md) as the format template.
- Durable rationale and traps: [`decision-log.md`](decision-log.md).

## 现在在哪里(2026-11-05, 交易回看 · #1306)

- `/trade` 底部的「交易回看」换成了新视图：选一轮，看计划线、每笔成交、判断、结束原因、结果，点事件看原始记录。
  只读，只有 GET。旧的 `#review` 面板还在，只是不显示，#1310 再一起清。
- 黄金 Paper 9/8 这轮从 `fills/`、`inputs/`、`park_strategy/` 三份账本拼出来：56 笔，费后 −122.14，未含资金费。
  结束时 10 笔未平，09:19 标记结束，16:21 才全部结算；有 3 笔账本时间是回填的，图上按实际结算时间画，账本时间保留在记录里。
- 行情只用这一轮自己录下的 market events；Testnet 走 Hyperliquid Testnet 历史 K 线。缺记录就写「这段记录缺失」，不补不算。
- 成交没轧平的 Testnet 轮次（如 0e696f60）不显示盈亏，只写「成交记录没有轧平，算不出结果」。

_下一步_：#1307 OKX 历史回填，再做 #1308 回测。

