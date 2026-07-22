# trading-system

## 要去哪里
多市场自动化交易系统:网格策略为主力,先 paper 盘验证、达标后进真钱;风控闸独立于策略永不妥协;每一笔行为可审计。(路线图基线:docs/trading-roadmap-2026-07-20.md,70% 评估)

## 现在在哪里(2026-07-21)
- 架构:19 节 ports-and-adapters 重构全部落地(PR-0 + A0–A17);DualTrack paper 引擎为权威验证场;live 路径未开。
- 已建成:网格线全生命周期与循环重挂(状态机层,#50);paper start 竞态修复;Dashboard 单调读模型;行情断供时 safe-action 保护(risk_port 纯增审计);测试网络隔离(「全绿」信号可信)。
- 硬数字:全量测试 ~1917 通过;2026-07-21 单日合并 7 个 PR。
- 明确未达成:循环重挂尚未接通 nautilus_paper 权威执行路径(实测 rearms=0)——用户可见结果未闭环。

## 下一步
- #51:权威路径重挂闭环(Park 已批 park-approved,在 Dev Queue)。
- paper 盘连续运行,积累 rearm 实绩与生命周期审计数据。
- 实绩达标后,与 Park 定义 live 准入标准(风险轴:真钱,Park 亲批)。
