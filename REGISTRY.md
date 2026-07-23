# trading-system

## 要去哪里
多市场自动化交易系统:网格策略为主力,先 paper 盘验证、达标后进真钱;风控闸独立于策略永不妥协;每一笔行为可审计。(路线图基线:docs/trading-roadmap-2026-07-20.md,70% 评估)

## 现在在哪里(2026-07-23)
- 架构:19 节 ports-and-adapters 重构已落地;DualTrack / Nautilus Paper 是权威验证场;live/真钱路径仍关闭。
- Goldbot V5 已部署代码提交 `main@890aa89`(DCA 基线 `1b5fcd9`);部署与浏览器验收期间保留原 Paper Grid 运行态,15 张已接受挂单、0 活跃持仓,未执行启动、停止、撤单或平仓。
- 网格生命周期、循环重挂、图表 Range 草稿确认、手动风险确认和自适应参数预览均已进入 main。
- Dashboard 已修复市价单 `NaN` 导致的整页读取失败;AI 决策与策略配置完整展开,生产运行状态独立滚动,旧“运行中调整”卡片已下线。
- Nautilus 当前周期与生产历史账本已使用同一权威引擎身份规则;已清除旧 flatten 身份造成的误报启动门禁,真实歧义仍保持 fail-closed。
- Paper 启动若被行情过期或跨越网格线安全拒绝,Dashboard 会从控制审计恢复真实原因;只允许无副作用的 `prepare_start` 重试一次,创建订单的 `start` 永不自动重试。
- 单边网格的 Range 外启动已按订单可成交性区分:做多位于上边界之上、做空位于下边界之下可等待回归;反向暴露侧和中性网格仍 fail-closed。停机前也可在图上调整待启动 Range,启动失败原因改为屏幕中央展示。
- 新周期尚未建立 StrategyPlan 时也可根据可信行情智能填充 Range;该步骤保持只读,不会写计划、启动机器人或创建订单。
- GridMind 已区分“启动前风险提醒”和“运行故障”:当前 14.18x Paper Grid 的确认回执、预览 ID 与风险决策 ID 精确匹配且后台已接受,因此顶部正确显示绿色运行中;14.18x 超过 10x 的风险详情仍保留。浏览器复验为实时可信行情、15 张已接受委托、0 控制台错误。
- Paper DCA 已具备做多/做空加仓计划、累计仓位后单张整轮 TP 数量更新、整轮止损、风险确认、控制面与 V5 参数预览;浏览器已验收做多 7 参数联动重算及做空智能填充。首轮真实 DCA 生命周期尚未启动观察,不能把 UI/自动化测试当作成交证据。
- #192/#193 已部署：Paper tick 已连续两次成功，Grid 与 DCA 的最终启动均要求 180 秒内完整 tick。#194 正在把运行中的 tick 失联投影为 Dashboard 明确可见的降级状态。
- #175 正在统一历史 NAV 的展示口径：仅机器生产已实现 P&L 可进入累计和 NAV；recovery replay 与缺失值均不能伪装为生产收益或 0。
- decision-log 已与 main 对账补齐(2026-07-23):#96/#82/#126/#130/#134/#138 的设计决策、失败模式与验证证据已入档。
- DCA 审计后续已合并并部署(#164/#165/#166/#168):DCA×Grid 互斥与 TP 提交失败 fail-closed 均有回归测试;`loop_enabled=true` 在 v1 被显式拒绝,概要恒显示「完成后停止」;静态套件在修正 #154 遗留的 `actionStatus` 断言后恢复全绿。重启 Dashboard 后 Grid 运行态不变(running、15 挂单、0 持仓),页面 0 控制台错误。
- #171/#185: Nautilus Paper 启动/预启动现要求当前周期 `dualtrack-live-tick` 的 180 秒内成功心跳；仅完整完成行情、生命周期与账本刷新后才落盘，避免反复崩溃制造假绿。
- #173: weekly ledger 对 deploy-canary 等非 ISO 历史 cycle/date 记录诊断并跳过，不能再中断 Paper tick；有效日期账本仍按原周归属计算。
- #172: 跨周期 Paper runtime 不再被当前周期静默投影成“0 委托”。上一周期仍在运行或仍有已接受委托时，Dashboard 显示具体周期与数量，任何新 Grid/DCA 启动一律拒绝；rollover 只负责停止、撤单/平仓与封包，下一周期必须由操作者明确启动，绝不从旧几何自动生成计划。
- #189: tick 的生命周期顺序已改为“关闭旧周期 → 收口旧 Paper runtime → 规划当前周期”。历史行情下载超时不能再阻止旧周期的安全收口；若 rollover 本身阻塞，当前周期规划明确跳过。
- #174: 每个 terminal Paper package 现在会在隔离 Nautilus Shadow 中尝试生成 production 基准与 `notional-half` What-if；缺少事件、runtime 或 preflight 会作为明确原因留在 package/页面，Shadow 失败不会重开或阻塞生产周期收口。
- #176: canonical accounting 会保留任何“平仓早于开仓”的历史原始记录并写入明确 reconciliation 诊断；Dashboard 将其隔离为“时间异常”，不再计为正常已完成交易。
- #177: DCA lifecycle 的聚合止盈世代、累计数量和退出状态已从 Paper JSON 接入 read model；页面明确说明整轮 TP 由行情事件触发、不是 entry 挂单。

## 下一步
- 排查 #178 的 daily 24-hour report launchd 失败，并将可恢复诊断落到运行状态与 GitHub。
- 在 tick 健康且不存在旧策略冲突的 Paper 窗口，验收首轮 DCA:两次加仓成交 → 唯一整轮 TP 数量随累计持仓更新 → 整轮 TP 或 SL 退出 → `outputs/dualtrack/dca_lifecycle/` 审计落盘。
- 继续积累 Grid 开仓→止盈→原价重挂与 P&L reconciliation 实绩,DCA 与 Grid 必须保持独立 StrategyPlan 与生命周期账本。
- 用 12 小时复盘与 Strategy Shadows 比较网格变体,只在足够交易样本和可持续原因成立后升级主策略。
- 实绩达标后再定义 live 准入标准;任何真钱动作仍需 Park 本人 `park-approved`。
