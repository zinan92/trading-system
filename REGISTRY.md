# trading-system

## 要去哪里
多市场自动化交易系统:网格策略为主力,先 paper 盘验证、达标后进真钱;风控闸独立于策略永不妥协;每一笔行为可审计。(权威实施基线:docs/plans/implementation-plan-2026-07-24.md,完整产品 65% 评估)

## 现在在哪里(2026-07-29)
- Cloud Paper 当前部署为 `main@fcb0df3597acc9d989d3923e1025eac91fefa606`（#425），preflight 与 dashboard/access-gateway/dead-man/live-tick boot receipts 均绑定同一 source/tree。4 个常驻服务和 5 个 systemd timers 均 active；最近自然 tick 成功且 180 秒内新鲜，可信 Binance USD-M Futures 行情 fresh。
- 今日新鲜 AI 建议 `ai-eval-4272ba2b41ca4382` 已经完整安全路径启动为中性稳健 Grid：StrategyPlan=`strategy-plan-2026-07-29_DAY-2-52d364d4` v2，38/38 委托创建并接受、38 条 lifecycle armed。当前仍为同一计划，runtime=`running`；已有 1 个当前计划真实 entry fill、1 个带 TP/SL 的 Paper 空头持仓与 37 张剩余委托，execution reconciliation=`ok`、canonical accounting=`pass`。2026-07-28 DAY 的 3 笔成交不计入今日计划证据。
- #416/#417 已修复生产执行到派生日账本的长期断链：`2026-07-28_DAY` 现为 3 trades / 6 fills / `-7.47319148 USD`，来源为验证通过的终态 StrategyCyclePackage；原始 fills/trades/周期包未改写。NAV 与基于账本计数的复盘/推广聚合必须使用重建后的派生账本，既有终态复盘和 Shadow 原始证据保持不可变。
- #406/#418、#407/#419、#420/#421 与 #422/#423 已部署：Cloud dead-man 使用当前 Paper 权威执行快照且未知仍 fail-closed；正式 Dashboard 诊断和策略控制台均返回 200；公网探针正确区分 Cloudflare Access 登录页；Linux Cloud preflight 对 29 个活动运行时文件执行 macOS 路径门禁且零违规。#424/#425 进一步把晚到行情保留为 `late_ignored`，防止重放回写既有 fill；当前 fill 时间仍保持 `2026-07-29T02:41:00Z`。
- Cloud soak 仍未终态通过：第一份可证明完整 Cloud 北京自然日的 `report_date=2026-07-29` 日报/自复盘只能在 2026-07-30 01:10 后验收。当前 cloud-health 因 2026-07-28 自复盘不完整显示 degraded；这不是行情、tick、执行或对账故障，也不得降低完整自然日门槛。完整任务验收见 [`docs/evidence/cloud-paper-recovery-2026-07-29.md`](docs/evidence/cloud-paper-recovery-2026-07-29.md)。
- 默认测试基线已与已合并合同重新对齐（#401）：Completion Audit 的 full/focus 调度标签包含 24 小时报表，Standard K-line 空十字线标签不再占位，Cloud 备份/预检的只读 SQLite seam 被精确登记；手工 Paper 订单仍由服务端风险事实裁决，最大计划损失保持 advisory，杠杆超限等硬闸保持 fail-closed。
- Cloud M6a 已实现：部署 manifest 锁定 trading-system/datafeed 的精确 SHA 且不携带密钥或激活 scheduler；切换前机械要求 Paper stopped、0 已接受委托、0 开放持仓、execution reconciliation 通过、备份验证、Cloud preflight 同 SHA 通过且 Cloud tick 禁用。正向切换只能 `local active -> paused -> cloud active`，失败后双端 tick 保持禁用；rollback 只接受更高 epoch 的 paused 状态再恢复 local owner。
- Cloud M5 已实现：公网链路固定为 `Cloudflare Access -> loopback 8766 allowlist gateway -> loopback 8765 Dashboard`，8100/8765 不公开暴露；gateway 只转发精确页面/API，控制请求必须通过 Access JWT 与操作者邮箱校验。`/api/trading-system/cloud-health` 分开报告行情、live-tick、执行、对账、每日复盘、备份、scheduler owner 与部署 SHA，Dashboard 生产状态卡显示 Cloud 7×24 总结；dead-man 的持久化回执不再包含 URL/token。
- Cloud M4 已实现：`outputs/` 与 datafeed SQLite 可生成自哈希、逐文件校验的离线恢复包；恢复只允许空目标、不会激活 scheduler，并强制后续 reconciliation。Paper scheduler owner 具有单调 epoch，只能 `active -> paused -> active`；一旦 cloud owner 生效，本机默认 `local-mac` 会在构造 live-tick runner 前被拒绝。
- Cloud M3 已实现：每天 01:10（北京）在终态 24 小时报表之后生成不可变 JSON/Markdown 自复盘，分开记录 tick 连续性、执行/对账、StrategyPlan/lifecycle，并明确“做对/做错/明天行动”。缺失或哈希错误证据保持 unknown；所有行动均 `executed=false`，稳定 API 为 `/api/trading-system/daily-self-review`。
- Cloud M2 已实现：systemd 可分别托管 loopback datafeed、Dashboard、one-shot live-tick、24 小时报表和 dead-man；Cloud 模式下 Dashboard/live-tick 只有在 preflight 与当前 clean SHA/tree 精确匹配时才启动。本阶段 installer 默认为 passive，只启动 datafeed，绝不激活 tick scheduler；卸载不触碰 `/var/lib/gridmind` 或 env。
- Cloud M1 已实现：Linux Paper preflight 会在零控制动作下验证 clean SHA、Paper-only 标志、持久化目录、loopback 端口、datafeed/storage、Binance USD-M 最新/历史可信 K 线和独立 Nautilus runtime；任一失败均落明确 blocked receipt。云端 app/datafeed/Nautilus 三套隔离运行环境与非密钥路径合同见 [`deploy/cloud/`](deploy/cloud/)。
- 进度: [实施进度页](docs/plans/implementation-progress-2026-07-24.md) 的 26 个已审核 story 已全部验证完成（**26/26，100%**）。M6-03 将 main SHA、发布前兼容性闸、健康/行情/执行分离、浏览器验收、证据落点及 Paper-only rollback 固化为 [release runbook](docs/runbooks/paper-release-rollback-v1.md)（[PR #365](https://github.com/zinan92/trading-system/pull/365)）。计划完成不等于自动启动或真实交易：后续每次 Paper 发布仍须按 runbook 的实时安全闸与证据步骤执行。
- GitHub provenance: `main` 的 #223–#328 merge commits 仍完整，但对应 Issue/PR 元数据对象会返回 404；这是 GitHub 元数据缺口而非代码丢失。可访问的追踪入口为 [#330](https://github.com/zinan92/trading-system/issues/330)，完整 commit 索引与“先 API 读回再报告链接”规则见 [`docs/audits/github-provenance-222-329.md`](docs/audits/github-provenance-222-329.md)。
- 架构:19 节 ports-and-adapters 重构已落地;DualTrack / Nautilus Paper 是权威验证场;live/真钱路径仍关闭。
- Grid 全链路在 main:预览/风险确认/启动/循环重挂/收口;Grid 与 DCA 启动均要求 180 秒内完整 tick 心跳,旧周期未收口一律拒绝新启动。#413 允许 12 小时边界在新周期已有显式接管 StrategyPlan、tick/行情新鲜且 Nautilus 订单/仓位/生命周期身份逐项不变时只关账不平仓；任一条件缺失仍撤单、平仓、对账并等待新启动。tick 失败现会明确标记为行情/路由、生命周期、账本写入或调度器启动阶段并给出下一步；失败绝不写心跳，运行中失联显示「运行降级」。
- DCA v1 就绪:做多/做空加仓、单张整轮 TP 世代随累计持仓更新(由行情事件触发,不是 entry 挂单,页面已标注)、独立整轮止损、风险确认、read-model 可见;聚合 TP 合同已覆盖 1/2/3 次加仓及提交失败 fail-closed。一个逻辑整轮 TP 在 Nautilus 执行层会按精确 `position_id` 拆成多张 reduce-only 子单，绝不再用共享 round ID 模糊平仓；TP/SL 后停止,v1 显式拒绝 `loop_enabled=true`。此前首次 attended 尝试在两笔加仓后暴露该执行缺陷，已安全撤单平仓，**不计作真实生命周期验收**。
- #226 attended Paper DCA 已自然闭环:做多计划 `strategy-plan-2026-07-24_DAY-5-c3bf366f` 经真实 tick 完成两次加仓；第一笔后 generation-1，第二笔后 generation-2 将聚合 TP 扩至 `0.004 @ 4037.5`。generation-2 于 `2026-07-24T06:20:00Z` 自然成交，lifecycle 为 `target_closed`、0 持仓/0 委托，execution reconciliation=`ok`，canonical accounting=`pass`（两条既有时间异常仍 quarantine）。未注入行情、人工平仓或重启执行器制造证据。
- 可观测性:请求未达后端/Cloudflare 530/Dashboard 5xx/Binance 上游/网格回滚五类故障链分立文案各带下一步;硬输入 blocker 结构化解释;历史 NAV 仅计 machine 生产已实现 P&L,缺失即显示不可用;终态周期自动尝试 production+notional-half What-if Shadow,缺失原因显式留档。
- M1 安全恢复链路完成:浏览器响应丢失后只读取 append-only 控制审计回执、权威 runtime 与活动计划身份来确认结果；`prepare_start` 和 `replace_grid` 不再自动二次请求。运行状态卡显示最近控制回执；证据不足时保留未确认状态，不猜测、也不重放控制动作。
- M2-04 聚合 TP 合同完成（#254 / `main@5d5b63b`）:第二次加仓后，终态成交必须引用最新 generation、排除已撤换 generation，并精确平掉累计数量；有效 DCA 几何的目标/止损结果互斥。该证据是纯回放，不替代真实 Paper 成交。
- #257 历史 DCA 聚合对账已部署（#258 / `main@29227f7`）:仅在 lifecycle、Nautilus 子仓位和精确 child command 三者完整对应时，read-model 才把已完成 DCA 子单重建为逻辑整轮；原始历史文件不改。部署后 reconciliation=`pass`、活动差异=0；两条早于开仓的不可变手工历史继续可见于 quarantine，不被隐藏或当作可交易状态。
- #283 canonical preflight 对账已部署（#284 / `main@6d7fea1`）:Grid 启动前风险与生产历史现在共享同一套、证据门控的 Nautilus DCA 聚合规则。当前 Paper 原始快照会误报 22 条历史 DCA 对账问题；聚合后 accounting=`pass`、engine reconciliation=`ok`、0 持仓/0 入场挂单。缺 lifecycle、子仓或精确 TP child-command 证据时，快照保持原样且仍 fail-closed。
- #280 Grid 生命周期证据包已部署（#287 / `main@d611873`）:每条 Nautilus Paper 网格线只有在 entry/TP/原价重挂、计划版本、订单/成交/交易 ID、生命周期转换和 reconciliation 都一致时才显示为「已证实」；任何缺失都显示「未验证」，不会用 K 线穿越推断成交。
- #289 Grid Paper 实证已完成（#297 / `main@5df6046`）:StrategyPlan v7 的一条 Nautilus Paper 网格线已自然完成 `entry → TP → 原价重挂`，证据包为 `completed_rearmed_count=1`、`unverified_count=0`、reconciliation=`ok`；命令、成交、快照与生命周期文件哈希见 `docs/evidence/issue-289-grid-rearm-2026-07-24.md`。之后 tick 未保持新鲜窗口，按 fail-safe 正常停止并撤掉第二代挂单；当前 Paper=`stopped`、0 已接受委托、0 开放持仓。
- #301 Grid Shadows 已扩展（#302 / `main@e9f3591`）:每个符合条件的终态 Grid 周期会在隔离 Nautilus replay 中生成生产基准、50%/150% 名义、交替偶/奇稀疏网格共 5 个同窗口 What-if；它们共享执行/费用合同与输入哈希规则，永不写生产账本、改 StrategyPlan 或创建真实订单。
- #305 Shadow 推广证据闸已部署（#306 / `main@a05ba05`）:Grid Shadow 候选只有在至少 100 笔有效可比的已平仓交易、至少两个完整周期、同窗口与执行/费用合同一致、收益优于基准且回撤/成本不恶化时才会标记 `proposal_ready`；该状态只供人工审阅，绝不自动升级主策略或下单。
- #309 Shadow 推广提案已部署（#310 / `main@db36902`）:Dashboard 现将已封包闭环周期的 Grid Shadow 证据按跨周期门槛投影为只读建议，展示支持/反证、证据 ID、窗口、执行/费用合同及收益/回撤/成本变化；同周期 What-if 不会伪装成推广结论。即便 `proposal_ready`，仍必须人工审阅并另建计划变更，系统没有自动升级或下单路径。
- #313 安全修复队列已部署（#314 / `main@f90814c`）:服务/缓存/read-model 的恢复只能进入带诊断、前后证据和验证回执的候选队列；本阶段没有执行器。订单、持仓、风险、StrategyPlan、执行引擎、行情源一律为 `requires_human_confirmation`，read-model 仅展示证据且 `command_authority=false`。
- #317 参数草稿状态矩阵已审计（#318 / `main@84a22f8`）:Grid/DCA 的 AUTO/手动、字段非法、风险确认、智能填充、DCA 参数联动及「不发控制请求」边界均经 52 项浏览器/静态矩阵复验。发现的三项失败均是测试期待旧措辞；已对齐当前更具体的账户对账、恢复下一步及正整数说明，不涉及产品、控制面或运行时部署。
- #321 Range 拖动合同已复验（#322 / `main@874ebad`）:启动前和运行中 Grid 的中间/上下边界拖动、草稿保留、取消、只读预览、风险确认与最终替换均由浏览器和控制面合同覆盖。补充真实 wheel 缩放后草稿边界仍按价格轴重绘、零控制请求的 Playwright 证据；本次仅新增回归测试，无运行时部署。
- #325 启停结果合同已复验（#326 / `main@2129b13`）:启动成功、启动拒绝与居中失败弹窗、响应中断后的未知状态，以及停止响应丢失后由权威 runtime 核对成功的路径均有浏览器覆盖。任何缺少回执的控制动作不会重发；只在运行状态、委托和持仓共同证明后才展示成功。本次仅新增回归测试，无运行时部署。
- #332 图表回归审计已完成（#333 / `main@ce67229`）:240 根仅为初始历史页，滚动/手势可在保留当前快照的同时增量加载更早可信 K 线；十字线日期在底部时间轴；策略几何变化会重新计算价格轴并保持可见网格边界对齐。远离当前行情的网格线不会强行压缩 K 线视图。本次仅新增浏览器证据，无运行时部署。
- 启动前草稿:手动参数若为空或格式无效，页面会标出具体字段、给出修复动作、清除旧预览并禁用启动；`手动` 可一键回到 `AUTO` 求解。该前端提示不放宽任何后端或 Paper 风控门禁。
- 执行测试:保护性 sweep 只处理已收盘、可信 K 线；形成中的当前 K 线不会送入执行器。#240 已恢复这一合同的锁内正反向回归覆盖。
- M1-01 运行状态合同已固化为 [`docs/contracts/authoritative-runtime-state-v1.md`](docs/contracts/authoritative-runtime-state-v1.md)：Dashboard 只消费权威 read-model；当前/上一周期、执行快照、tick、对账和不确定计数的字段所有权、降级语义与后续 fixture 矩阵已明确。下一步据此拆实现票，不在设计票中改变执行行为。
- 治理:decision-log 与 main 对账一致(近期功能 PR 完工义务全履行);pre-live 四项历史风险已复验,唯一残留 gate = naked-position 的 mainnet attended canary(docs/audits/);AGENTS.md 已仓内化;GitHub 缺号 #52–#128 有 provenance 索引。

## 下一步
- 只读监测当前 `strategy-plan-2026-07-29_DAY-2-52d364d4` 的 TP/SL/循环生命周期、tick、行情与双层对账；不得重复启动、停止、撤单、平仓或修改 StrategyPlan。现有 1 个真实 fill 已满足 #408 的“当前计划成交证据”，后续 accepted/armed 仍不得冒充新增成交。
- 继续 Cloud soak 至首个完整北京自然日闭环：2026-07-30 01:10 后要求 `report_date=2026-07-29` 的终态日报与完整自复盘，再连同 tick coverage、tick failures、备份、dead-man、服务、行情、owner epoch 3 与 Mac jobs unloaded 做终态验收；任何 unknown 都不算通过，不启用 Mac failback。
- 监测自然 tick 单次耗时和 `late_ignored` 数量；如果再发生 timeout、不可变成交回归或执行/会计对账漂移，按独立缺陷 Issue fail-closed 处理，不得重放控制动作。
- #440 完成 #415 的执行终态返工：`plan active` 不再等于决策完成；每个周期须在 5 分钟内进入 `executed / blocked / adopted_existing`。已有 AI 计划直接走正常 `prepare_start → 风险闸 → start`，不重复跑 AI；成功必须正数 N/N 委托且 runtime=`running/running`，其余情况留下机器码、原因与下一步且同周期不重试。真钱/live 仍需 Park 本人 `park-approved`。
- M1-02:补齐 tick 剩余路由/账本失败阶段的诊断与恢复动作证据；不重做现有 180 秒心跳闸，不放宽任何 Paper 启动保护。
- M3-05:审计当前生产策略摘要与持仓/委托/成交表的字段、计数、对齐和桌面可读性；只补复现的完整性或可理解性缺口。
- M1 安全恢复:继续验证 read-model 在浏览器轮询下的完成率；#276 已隔离并压缩重证据，若再出现超时，按阶段记录原因与回执，在有证据前不自动重试任何控制动作。
- 继续积累 Grid 开仓→止盈→原价重挂与 P&L reconciliation 实绩,DCA 与 Grid 必须保持独立 StrategyPlan 与生命周期账本。
- 用 12 小时复盘与 Strategy Shadows 比较网格变体,只在足够交易样本和可持续原因成立后升级主策略。
- 实绩达标后再定义 live 准入标准;任何真钱动作仍需 Park 本人 `park-approved`。

## 实施规划（已审核）

完整的 expectation、当前 65% 基线、Milestone/Epic/Story 合同和审核顺序见 [`docs/plans/implementation-plan-2026-07-24.md`](docs/plans/implementation-plan-2026-07-24.md)。已批准按文档顺序分阶段执行；attended Paper 已获 Park 授权但每次仍须通过实时安全 preflight，真钱仍需独立人工授权。

## Appendix — 历史记录(只追加,原文搬运,不删除)

### 2026-07-29 逐票记录
- #440: 周期决策的完成态改为“已执行、明确阻塞或接管既有运行策略”；Cloud health 按当前时钟周期检测 active 计划超过 5 分钟仍 stopped 的中间态，canonical read-model 公开终态回执。AI provider 在 live-tick 内限时 30 秒，避免 systemd 先杀进程而来不及留下 blocked receipt。
- #415: 每个 DAY/NIGHT Paper 周期现在必须有且仅有一条不可变决策；完整 live tick 后若无人工决策会生成新 AI 建议并走正常 `prepare_start → start`。人工风险确认不得自动代签；已有持仓与新方向冲突时只按精确 ID 撤掉待成交入场单，保护单身份必须保持不变，不平仓、不反手、不对冲、不创建新单，并等待自然退出。Cloud health 会将缺失或重复决策标为 blocked。
- #413: 12 小时周期边界在且仅在当前周期 active StrategyPlan 显式引用旧计划、执行 tick/行情新鲜、Nautilus 新命名空间对账通过且 accepted order/open position/Grid lifecycle ID 全部不变时执行 Paper handoff；旧周期以 `terminal_mode=handed_off` 封包，realized 留旧账、unrealized 随仓位进新账。任何缺项保留原安全撤单/平仓路径并记录确切原因。
- #433: Dashboard 生产运行状态新增一行 `策略运行占比 24h / 7d`；只按 accepted control event 中可证明的 `actual_state=running` 区间计时，进程在线、rejected 动作与无法证明的窗口前段均不冒充策略运行，证据不足显示 `--`。
- #431: Mac Paper 隔离 receipt 兼容当前 macOS `launchctl print-disabled` 的 `enabled/disabled` 输出及旧式 `true/false`；缺 label 或未知值仍 fail-closed，不会把命令成功冒充成验证成功。
- #428: Cloud owner 切换后，Mac Paper 的五个 focus launchd job 现在同时执行持久 `disable` 与当前会话 `bootout`；重启/重新登录不会自动加载。只有 owner 已明确回到 active `local-mac` 且给出专用确认词时，才会按同一 allowlist 恢复；每个 label 的前后 loaded/disabled 状态均留 receipt。
- #424: Nautilus Paper 对迟到 K 线采用“保留原始事件、追加 `late_ignored` 处置、不得回写既有成交”的执行水位合同；不可变成交检测未放宽。根因与字段级证据见 [`docs/evidence/issue-424-late-market-event-replay.md`](docs/evidence/issue-424-late-market-event-replay.md)。
- #422: 全仓环境硬编码已完成分类审计；Cloud Paper preflight 新增可执行 Linux 运行闭包门禁，生产默认值不再依赖 Homebrew、个人 macOS 主目录或固定系统 Python。launchd/failback 保留为隔离的 Mac adapter，完整清单见 [`docs/audits/environment-hardcoding-2026-07-29.md`](docs/audits/environment-hardcoding-2026-07-29.md)。
- #416: 生产 Paper 日账本改从验证通过的终态周期包投影执行事实；根因、下游影响与重建边界见 [`docs/evidence/issue-416-daily-ledger-root-cause.md`](docs/evidence/issue-416-daily-ledger-root-cause.md)。
- #406: Cloud dead-man 的仓位严重级别改读 cloud-primary 当前周期的 Nautilus 权威执行快照；仅新鲜、身份一致且执行/会计双重对账通过的空仓显示 normal，其余未知仍 fail-closed 为 critical。非 Cloud 与 live/真钱读取路径保持不变。
- #407: 正式 `/api/dashboard` 诊断在独立 datafeed 模式下不再把 Cloud 的兼容 SQLite 环境路径误组装为 legacy 市场源；full/trader/ops/strategy 合同继续使用可信 datafeed，生产 legacy/synthetic 限制未放宽。
- #420: public-access-health 现在区分 Cloudflare Access 登录页与真实 Dashboard HTML；受保护路由显示 `public_access_protected` 且特征状态为未认证不可观测，不再把 Access 页缺少应用标记误报为 `public_deployment_stale`，也未给探针新增任何认证绕过。

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
- #370: launchd 兼容性不再假定所有 Paper job 都使用 `/usr/bin/python3`。Dashboard 与 live-tick 会按各自 plist 的 `ProgramArguments`/`PATH` 解析并逐一探测；Nautilus Python 作为独立依赖验证。任何 discovery/probe 异常都会保留 blocked receipt，但机械接入 restart 路径仍由下一张发布安全票完成。
- #372: Paper 发布闸已机械接入 schedule install/rollback、attended Nautilus cutover/rollback 与旧 `start.command`：receipt 必须 pass、包含兼容性通过、15 分钟内新鲜且 Git SHA 精确匹配当前 checkout，才允许任何 bootout/bootstrap/kickstart 或前台启动；失败路径只写 blocked receipt，不执行修改命令。
- #374: release runbook 已校正 gate 的 JSON/plain 输出、`/dashboard-v5.html` 路由别名与磁盘 `dashboard-gridmind.html` 的区别，并要求 source-bound mutation receipt 才能声称部署 SHA 或 live-tick 未重启；缺证据一律标 `unknown`。26/26 页使用不可变 completion baseline，不再假装等于移动中的 HEAD。
- #376: Paper pre-deploy receipt 进一步绑定 commit tree 与 tracked checkout 清洁状态；Dashboard 和 dualtrack live-tick 在 boot 时必须验签后才能绑定端口或构造 runner。主动重启仍要求 15 分钟新鲜回执，同一已发布版本的 KeepAlive 自愈和周期 tick 不因时间流逝失效；开发改动必须在独立 worktree 完成。
- #378: GOLD 的时间边界生命周期读取从 cache-only 改为 cache-first、零行时回同一 `binance_usdm_futures` execution venue；最新行情仍保持 bypass+strict，fallback/synthetic 仍禁止。任何上游或合同失败继续不写 tick heartbeat，并冻结 Grid/DCA 新启动。
- #380: Paper source-attestation boot 拒绝使用专用退出码 79，并以 service boot receipt 为权威诊断；78 保留为标准 `EX_CONFIG`，不得再把两者混报。
- #382: launchd 若在 Python 前报 LWCR/Invalid argument，只能通过 fresh source-bound receipt 驱动的单标签 rebootstrap 恢复；命令限制为 Paper allowlist，记录前后状态，不得用 broad schedule reinstall 或裸 kickstart 掩盖变更范围。
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
# 2026-07-27 Cloud M6b — passive host provisioning in progress

- Cloud M6a merged as PR #397 at `main@ec8be27`: exact-source deployment
  manifests and fail-disabled single-owner cutover/rollback are available.
- Issue #398 now owns the Alibaba Cloud Singapore passive-host build. The
  purchased host is a Simple Application Server running Ubuntu 24.04 x86_64
  with 2 vCPU, 2 GiB memory, and 40 GiB storage. The provider/login/payment
  boundary is complete; no Paper scheduler, strategy, order, position, exchange
  credential, or live process has been moved.
- Next: merge the source-bound Alibaba adapter, upload exact source archives,
  activate only loopback datafeed and Dashboard, then prove authenticated
  access and temporary restore before the separate M6c scheduler cutover.
