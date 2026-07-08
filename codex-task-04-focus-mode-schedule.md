# Codex 任务 04(v2):聚焦模式收缩 + 调度重装

> **本文件取代旧的 codex-task-04-schedule-reinstall.md(已删除)。若你已开始按旧规格工作,停止并从本文件重新开始。**
> 背景:所有者 2026-07-08 明确收缩范围——只保留人机双轨(人:手动定义 range 手动交易;机:dualtrack 条件网格),约 23 个舰队策略(MACD/chan/bollinger/vwap/breakout 等)全部**暂时关闭**(disable,不删除)。同时收尾 task-02 遗留的调度漂移问题。
> 你(Codex)严格按本规格实现。规格未覆盖的决策,选最保守(fail-closed)的做法并在 PR 里列出。
> **Phase 5 会修改本机 live launchd。先 dry-run 核对,失败按规则回滚,全程打印回执。**

## 关键事实(动手前先自行验证,Phase 0)

1. 双轨机器轨(`services/dualtrack_machine.py` + `dualtrack_grid_core`)**独立于**策略舰队,由 `dualtrack-cycle` launchd 任务驱动——关舰队不影响双轨。`configs/strategy.yaml` 里的 `gold_1m_grid` 是舰队策略,不是双轨机器轨,一起关。
2. `configs/strategy.yaml` 现有 23 个 `enabled: true` 策略(另有 2 个已 disabled);`StrategyRegistry.enabled()` 与 `multi_strategy_runner` 尊重该 flag。
3. `configs/pipeline.yaml` 的 `demo_trading.enabled: true` + `active_strategy_id: gold_1m_macd` 是往 Binance demo 发真实网络订单的绑定——**它是两次裸头寸事故的源头**,必须关闭。
4. 调度现状:`schedule_status --json` = `stale_installed`,9/9 loaded 但 0/9 匹配 generated。上次重装失败于 `launchctl bootstrap` dashboard → exit 5 EIO(常驻服务拆除竞态);且生成器的 deadman URL 注入依赖生成时 shell 环境(`schedule_manager._base_job` ~180 行),不确定性缺陷。
5. 已安装 runner plist 里有 `TRADING_ORCHESTRATOR_DEADMAN_URL`,当前 generated 没有——先修 Phase 2 再重装,否则会把 deadman URL 剥掉。

## Phase 0 — 现状基线(只读)

1. 复现上面 5 条(schedule_status / strategy.yaml 计数 / demo_trading / install 回执 / plist diff)。
2. 记录变更前基线:`completion_audit`、`live_activation --json`、`curl /api/system-state` 的完整输出存档进 PR——Phase 4 要用它对比"哪些检查纯粹因聚焦模式而翻红"。
3. **bias-ledger 摄入路径核查(重要)**:`MarketViewIntake.record()`(DT-INV-2 的闸门)现在由哪些入口调用?(grep pipelines/、dashboard_server 的 POST 路由、trading-plan 流程)。如果 `trading-plan` job 是唯一自动摄入口,停放它会断掉人轨方向分记账——**这不可接受**,你必须保留一条摄入路径(优先:确认 dashboard_server 的 market-view POST API 可用即可,人工/Obsidian 同步驱动;写明结论)。

## Phase 1 — 聚焦模式配置(纯代码+配置,TDD)

1. `configs/strategy.yaml`:23 个 enabled 策略全部 `enabled: false`。不删除任何条目。
2. `configs/pipeline.yaml`:`demo_trading.enabled: false`(保留其余字段原样,可逆)。
3. 验证 `multi_strategy_runner` 在 0 个 enabled 策略时优雅 no-op(有测试锁定;若现状不优雅,修为优雅空转并测试)。
4. 全仓搜索因"具体策略必须 enabled"而写死的既有测试断言,按意图更新(PR 里逐个列出改了哪些、为什么)。

## Phase 2 — 生成器:profile 化 + 确定性(纯代码,TDD)

1. `configs/pipeline.yaml` 新增 `schedule.profile`,取值 `dualtrack_focus | full`,**默认 dualtrack_focus**。
2. `schedule_manager.build()` 按 profile 生成:
   - `dualtrack_focus`(4 个):`dualtrack-cycle`、`dualtrack-live-tick`、`dashboard`、`deadman-ping`
   - `full`(9 个):现状全集(保留全部 builder 方法,一行配置可回退——这就是"暂时关闭"的可逆性保证)
3. 确定性修复(自旧 task-04 继承):生成前 `apply_live_env()`(语义照 `services/live_env.py` 既有行为),deadman 两个 env key 从 `configs/live.env` 稳定注入;若本机 live.env 缺该值,从已安装 plist 本地迁移补入(**值不得出现在任何提交物里**)。
4. 测试:两个 profile 的 job 集合断言;同一 live.env+代码连续两次 build 输出逐字节一致;live.env 有/无 URL 时 env key 的出现/缺席。

## Phase 3 — 安装器:孤儿移除 + 竞态修复(纯代码,TDD)

1. **孤儿移除**:安装时,已安装但不在 generated 集合里的本项目 label(`com.wendy.trading-orchestrator.*`)= 孤儿 → 备份 plist → bootout → 删除文件。回执记录每个孤儿的处理;rollback 必须能恢复孤儿(从备份还原 + bootstrap)。**只动本项目前缀的 label,其他一律不碰。**
2. **teardown-wait**:每个 bootout 后轮询 `launchctl print gui/<uid>/<label>` 直到服务真正消失(0.5s 间隔,10s 超时;超时=该 job 失败,走回滚)。
3. **EIO 重试**:bootstrap rc=5 → 等 2s 重试,最多 3 次。
4. **顺序**:dashboard 最后装;bootstrap 后 kickstart 并轮询 `http://127.0.0.1:8765/dashboard-v4.html`(15s 超时)确认恢复。
5. **rollback 同样使用 teardown-wait**(上次 strategies 掉线就是回滚裸奔 bootstrap 所致)。
6. 测试(fake `command_runner`):孤儿备份+移除+回滚恢复;teardown-wait 等待序列;EIO 重试成功/三次失败触发回滚;超时处理。

## Phase 4 — 审计与状态源的聚焦重划(纯代码,TDD;范围收敛,别扩散)

原则:**只重划那些纯粹因聚焦模式而翻红的检查**(用 Phase 0 基线对比确认),其余一律不碰。已知必改:

1. `completion_audit._schedule_artifacts`:required label 清单按 `schedule.profile` 取(focus=4 个)。
2. `completion_audit` 中依赖舰队产物、停放后必然过期的检查(如 `_daily_review_run` 等,以你的基线对比为准):profile=focus 时返回明确的 `parked_by_focus_mode` 状态(用仓库既有的 warn/skip 语义,**不许静默 pass**),evidence 里写明如何恢复(`schedule.profile: full`)。
3. `services/system_state.py`:focus profile 下 `daily_review` 检查从清单移除(双轨闭环已由 `dualtrack_heartbeat` 覆盖),profile 读取同一配置源;`not_scheduled`/字段语义不变。
4. 测试:focus/full 两种 profile 下 audit 与 system-state 的检查集合与状态断言;"parked 不等于 pass"有显式断言。

## Phase 5 — live 重装(ops,谨慎)

1. 重新生成(此时 profile=dualtrack_focus,4 个 plist,且 runner 已不在集合——deadman URL 验证转移到 deadman-ping job 的 plist 上,grep key 存在,不打印值)。
2. `schedule_install --dry-run --json`:核对 plan = 安装/更新 4 个 + 移除 5 个孤儿,全部是本项目前缀;贴回执。
3. 用 dry-run 给出的 acknowledgement + package-id 真实安装,逐条打印 launchctl 回执。
4. 验收(全部满足):
   - `schedule_status --json`:健康态,`matching_generated_count == loaded_count == required_count == 4`,`mismatched_jobs == []`,孤儿不再出现
   - `launchctl list | grep com.wendy.trading-orchestrator` 恰好 4 个 label
   - `curl http://127.0.0.1:8765/dashboard-v4.html` → 200;`python3 -m pipelines.deadman_ping --json` 成功
   - dualtrack 心跳保持 `fresh`(`completion_audit` 的 `dualtrack_cycle_liveness` pass)
   - `curl /api/system-state`:不再有 `schedule` 的 DEGRADED;`daily_review` 检查已按 focus 语义处理
   - **停机验证**:确认 `strategies`/`runner` 进程不再被调度拉起;当天 `outputs/**/demo_order_requests/` 无新增条目(裸头寸源头已死)
5. 任何原本 loaded 且属于保留集的 job 安装后掉线 → 立即 rollback(含孤儿恢复),核对回到起点状态,停下报告。宁可维持 stale_installed 也不留下更差的状态。

## 硬约束

- 不碰订单/执行/broker/风控代码(Phase 1 的两个 config 开关除外——它们正是本任务目的);R4 锁定文件零 diff。
- 一切都可逆:不删代码、不删策略条目、不删 builder;恢复路径 = `schedule.profile: full` + 重新 enable + 重装,写进 decision-log。
- 秘密值不得出现在任何提交物;测试用假 URL。
- UTC-aware;显式错误处理;类型注解;frozen dataclass;新文件 <400 行。
- TDD 先红后绿;全仓 `python3 -m pytest -q` 全绿。
- decision-log.md 记录:聚焦模式决策(所有者 2026-07-08 口述,理由:只跑双轨、舰队负 EV、demo 链两次裸头寸)、"生成结果不得依赖生成时 shell 环境"规则、恢复路径。

## DOD

- [ ] Phase 1-4 测试全绿 + 全仓全绿
- [ ] Phase 5 验收 6 条全过,回执贴进 PR
- [ ] Phase 0 基线 vs 变更后对比表:每个翻红检查的处置(rescoped / parked / 遗留并说明)
- [ ] bias-ledger 摄入路径结论写进 PR(它必须活着)
- [ ] PR 列出所有自行决定的点
