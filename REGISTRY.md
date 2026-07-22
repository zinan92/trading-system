# trading-system

## 要去哪里
多市场自动化交易系统:网格策略为主力,先 paper 盘验证、达标后进真钱;风控闸独立于策略永不妥协;每一笔行为可审计。(路线图基线:docs/trading-roadmap-2026-07-20.md,70% 评估)

## 现在在哪里(2026-07-22)
- 架构:19 节 ports-and-adapters 重构已落地;DualTrack / Nautilus Paper 是权威验证场;live/真钱路径仍关闭。
- Goldbot V5 已部署代码提交 `main@8f80f37`;当前 Paper 机器人 stopped、0 活跃挂单、0 活跃持仓,Dashboard read-model、账本核对与 Cloudflare Access 健康。
- 网格生命周期、循环重挂、图表 Range 草稿确认、手动风险确认和自适应参数预览均已进入 main。
- Dashboard 已修复市价单 `NaN` 导致的整页读取失败;AI 决策与策略配置完整展开,生产运行状态独立滚动,旧“运行中调整”卡片已下线。
- Nautilus 当前周期与生产历史账本已使用同一权威引擎身份规则;已清除旧 flatten 身份造成的误报启动门禁,真实歧义仍保持 fail-closed。
- Paper 启动若被行情过期或跨越网格线安全拒绝,Dashboard 会从控制审计恢复真实原因;只允许无副作用的 `prepare_start` 重试一次,创建订单的 `start` 永不自动重试。
- 单边网格的 Range 外启动已按订单可成交性区分:做多位于上边界之上、做空位于下边界之下可等待回归;反向暴露侧和中性网格仍 fail-closed。停机前也可在图上调整待启动 Range,启动失败原因改为屏幕中央展示。
- 新周期尚未建立 StrategyPlan 时也可根据可信行情智能填充 Range;该步骤保持只读,不会写计划、启动机器人或创建订单。

## 下一步
- 由 Park 选择并启动下一轮 Paper 网格,连续积累开仓→止盈→原价重挂与 P&L reconciliation 实绩。
- 用 12 小时复盘与 Strategy Shadows 比较网格变体,只在足够交易样本和可持续原因成立后升级主策略。
- 实绩达标后再定义 live 准入标准;任何真钱动作仍需 Park 本人 `park-approved`。
