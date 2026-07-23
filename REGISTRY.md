# trading-system

## 要去哪里
多市场自动化交易系统:网格策略为主力,先 paper 盘验证、达标后进真钱;风控闸独立于策略永不妥协;每一笔行为可审计。(路线图基线:docs/trading-roadmap-2026-07-20.md,70% 评估)

## 现在在哪里(2026-07-24)
- 架构:19 节 ports-and-adapters 重构已落地;DualTrack / Nautilus Paper 是权威验证场;live/真钱路径仍关闭。
- 部署:Goldbot V5 Dashboard = `main@b5c91ae`（#221）；Paper stopped、0 委托、0 持仓;`dualtrack-live-tick` 每分钟心跳连续健康,Dashboard 页面与 read-model 均 200。
- Grid 全链路在 main:预览/风险确认/启动/循环重挂/收口;Grid 与 DCA 启动均要求 180 秒内完整 tick 心跳,旧周期未收口一律拒绝新启动;rollover 只停止/撤单/封包,下一周期必须操作者显式启动;运行中 tick 失联显示「运行降级」。
- DCA v1 就绪:做多/做空加仓、单张整轮 TP 世代随累计持仓更新(由行情事件触发,不是 entry 挂单,页面已标注)、独立整轮止损、风险确认、read-model 可见;TP/SL 后停止,v1 显式拒绝 `loop_enabled=true`。**首轮真实生命周期尚未验收,UI/自动化测试不算成交证据。**
- 可观测性:请求未达后端/Cloudflare 530/Dashboard 5xx/Binance 上游/网格回滚五类故障链分立文案各带下一步;硬输入 blocker 结构化解释;历史 NAV 仅计 machine 生产已实现 P&L,缺失即显示不可用;终态周期自动尝试 production+notional-half What-if Shadow,缺失原因显式留档。
- 启动前草稿:手动参数若为空或格式无效，页面会标出具体字段、给出修复动作、清除旧预览并禁用启动；`手动` 可一键回到 `AUTO` 求解。该前端提示不放宽任何后端或 Paper 风控门禁。
- 治理:decision-log 与 main 对账一致(近期功能 PR 完工义务全履行);pre-live 四项历史风险已复验,唯一残留 gate = naked-position 的 mainnet attended canary(docs/audits/);open issue 仅 #38(Portfolio 锚点);AGENTS.md 已仓内化;GitHub 缺号 #52–#128 有 provenance 索引。

## 下一步
- 在 tick 健康且不存在旧策略冲突的 Paper 窗口，验收首轮 DCA:两次加仓成交 → 唯一整轮 TP 数量随累计持仓更新 → 整轮 TP 或 SL 退出 → `outputs/dualtrack/dca_lifecycle/` 审计落盘。
- 继续积累 Grid 开仓→止盈→原价重挂与 P&L reconciliation 实绩,DCA 与 Grid 必须保持独立 StrategyPlan 与生命周期账本。
- 用 12 小时复盘与 Strategy Shadows 比较网格变体,只在足够交易样本和可持续原因成立后升级主策略。
- 实绩达标后再定义 live 准入标准;任何真钱动作仍需 Park 本人 `park-approved`。

## Appendix — 历史记录(只追加,原文搬运,不删除)

### 2026-07-22/23 逐票记录(蒸馏于 2026-07-23,原正文条目原样保留)
- Goldbot V5 已部署代码提交 `main@890aa89`(DCA 基线 `1b5fcd9`);部署与浏览器验收期间保留原 Paper Grid 运行态,15 张已接受挂单、0 活跃持仓,未执行启动、停止、撤单或平仓。
- 网格生命周期、循环重挂、图表 Range 草稿确认、手动风险确认和自适应参数预览均已进入 main。
- Dashboard 已修复市价单 `NaN` 导致的整页读取失败;AI 决策与策略配置完整展开,生产运行状态独立滚动,旧“运行中调整”卡片已下线。
- Nautilus 当前周期与生产历史账本已使用同一权威引擎身份规则;已清除旧 flatten 身份造成的误报启动门禁,真实歧义仍保持 fail-closed。
- Paper 启动若被行情过期或跨越网格线安全拒绝,Dashboard 会从控制审计恢复真实原因;只允许无副作用的 `prepare_start` 重试一次,创建订单的 `start` 永不自动重试。
- 单边网格的 Range 外启动已按订单可成交性区分:做多位于上边界之上、做空位于下边界之下可等待回归;反向暴露侧和中性网格仍 fail-closed。停机前也可在图上调整待启动 Range,启动失败原因改为屏幕中央展示。
- 新周期尚未建立 StrategyPlan 时也可根据可信行情智能填充 Range;该步骤保持只读,不会写计划、启动机器人或创建订单。
- GridMind 已区分“启动前风险提醒”和“运行故障”:当前 14.18x Paper Grid 的确认回执、预览 ID 与风险决策 ID 精确匹配且后台已接受,因此顶部正确显示绿色运行中;14.18x 超过 10x 的风险详情仍保留。浏览器复验为实时可信行情、15 张已接受委托、0 控制台错误。
- Paper DCA 已具备做多/做空加仓计划、累计仓位后单张整轮 TP 数量更新、整轮止损、风险确认、控制面与 V5 参数预览;浏览器已验收做多 7 参数联动重算及做空智能填充。首轮真实 DCA 生命周期尚未启动观察,不能把 UI/自动化测试当作成交证据。
- #192/#193 已部署：Paper tick 已连续两次成功，Grid 与 DCA 的最终启动均要求 180 秒内完整 tick。#194 正在把运行中的 tick 失联投影为 Dashboard 明确可见的降级状态。(注:#194 已于当日完成)
- #175 正在统一历史 NAV 的展示口径：仅机器生产已实现 P&L 可进入累计和 NAV；recovery replay 与缺失值均不能伪装为生产收益或 0。(注:#175 已于当日完成)
- decision-log 已与 main 对账补齐(2026-07-23):#96/#82/#126/#130/#134/#138 的设计决策、失败模式与验证证据已入档。
- DCA 审计后续已合并并部署(#164/#165/#166/#168):DCA×Grid 互斥与 TP 提交失败 fail-closed 均有回归测试;`loop_enabled=true` 在 v1 被显式拒绝,概要恒显示「完成后停止」;静态套件在修正 #154 遗留的 `actionStatus` 断言后恢复全绿。重启 Dashboard 后 Grid 运行态不变(running、15 挂单、0 持仓),页面 0 控制台错误。
- #171/#185: Nautilus Paper 启动/预启动现要求当前周期 `dualtrack-live-tick` 的 180 秒内成功心跳；仅完整完成行情、生命周期与账本刷新后才落盘，避免反复崩溃制造假绿。
- #173: weekly ledger 对 deploy-canary 等非 ISO 历史 cycle/date 记录诊断并跳过，不能再中断 Paper tick；有效日期账本仍按原周归属计算。
- #172: 跨周期 Paper runtime 不再被当前周期静默投影成“0 委托”。上一周期仍在运行或仍有已接受委托时，Dashboard 显示具体周期与数量，任何新 Grid/DCA 启动一律拒绝；rollover 只负责停止、撤单/平仓与封包，下一周期必须由操作者明确启动，绝不从旧几何自动生成计划。
- #189: tick 的生命周期顺序已改为“关闭旧周期 → 收口旧 Paper runtime → 规划当前周期”。历史行情下载超时不能再阻止旧周期的安全收口；若 rollover 本身阻塞，当前周期规划明确跳过。
- #174: 每个 terminal Paper package 现在会在隔离 Nautilus Shadow 中尝试生成 production 基准与 `notional-half` What-if；缺少事件、runtime 或 preflight 会作为明确原因留在 package/页面，Shadow 失败不会重开或阻塞生产周期收口。
- #176: canonical accounting 会保留任何“平仓早于开仓”的历史原始记录并写入明确 reconciliation 诊断；Dashboard 将其隔离为“时间异常”，不再计为正常已完成交易。
- #177: DCA lifecycle 的聚合止盈世代、累计数量和退出状态已从 Paper JSON 接入 read model；页面明确说明整轮 TP 由行情事件触发、不是 entry 挂单。
- #178: 24 小时报表已成为受控 launchd 任务，直接执行本仓 pipeline；不再依赖已移动的外部 wrapper。调度状态会给出受限 stderr 摘要、失败分类、下一步以及报表产物是否存在。
- #179: `/usr/bin/python3`（launchd 实际使用的 Python 3.9）现有独立的 import 兼容性闸和可读回执；Paper 服务重启前必须通过，不能用开发环境 Python 3.13 的测试结果替代。
- #180: GridMind 的 Paper 操作者主路径现在有一条连续 Playwright 验收：趋势刷新、智能填充、启动前调整、启动、停止，以及复盘/Shadows/NAV 入口均验证可见结果和控制请求；详细图表、拖动、历史加载仍由各自的聚焦浏览器测试覆盖。
- #181: `codex/recovery-stash-20260721` 的四个 handoff 指定文件已逐项三方审计；均不应直接恢复，恢复分支仍完整保留为只读证据，结论见 `docs/audits/recovery-stash-20260721.md`。
- #182: GitHub 404 的历史 #52–#128 已有不可伪造的本仓 provenance 索引；只记录可复验 commit/decision-log 证据，绝不伪造原 Issue，见 `docs/audits/missing-issue-provenance-52-128.md`。
- #183: 四项历史 pre-live 风险已重新复验：日损 NaN/时区/陈旧证据 fail-closed、测试网保护单回收、百分比仓位口径一致、Obsidian 可选；mainnet 仍被独立 activation/canary 门禁阻断，见 `docs/audits/pre-live-risk-invariants-2026-07-23.md`。
- #147: 根 `AGENTS.md` 已由失效的外部符号链接替换为仓内可读的 Paper 安全、交付和证据规则；#137 已按已合并的 #138 与浏览器回归证据关闭。
- #146: 自适应求解器的硬输入边界仍不可绕过，但不再将 2–200 格、整数格数或 1–20x 杠杆错误以裸 `ValueError` 交给操作者；控制面返回不可执行的结构化 blocker，Dashboard 显示原因和下一步。候选 Range/策略类型的不可覆盖 blocker 同样有专属说明。
- #145: GridMind 已把「请求未到后端」「Cloudflare 隧道 530/1033」「Dashboard 5xx」「Binance USD-M 行情上游」和「完整网格未被接受后安全回滚」分开说明，每种状态都包含下一步；展示没有放宽任何行情或执行 fail-closed 门禁。
- #214: 策略类型的 Grid / DCA 选择使用高对比选中卡、`✓ 当前选择` 标签和同步的 `aria-pressed` 状态；切换后仅一个类型保持选中，策略计算与下单语义未变。
- #216: 浏览器未收到 Dashboard 响应时不再断言请求未到后端或 Paper 未改变；控制动作一律进入权威 read-model 核对，读取动作明确提示刷新重试。
- #218: AI 市场评估改为位置优先：D1 200 根完整日线先给出低/中/高位与方向倾向，再由 D1/4H 明确震荡、形成中、已形成趋势，最后才推荐 Grid/DCA 与确定性参数职责；只写提案与本地预览，绝不自动启动或下单。
