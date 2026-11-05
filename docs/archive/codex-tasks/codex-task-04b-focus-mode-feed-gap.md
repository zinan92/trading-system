# Codex 任务 04b:补上聚焦模式下的 1m 行情喂养 + 裸头寸检查的新鲜度门

> 背景:task-04(聚焦模式收缩)已落地且安全(4/4/4 调度、23 策略已关、demo 绑定已关、孤儿清理带回滚,1204 测试通过)。但验收时发现一个未被 task-04 自身验收覆盖的真实缺口,本任务专门补上。**不重新讨论 task-04 已完成的部分,只做以下两件事。**
> 你(Codex)严格按本规格实现。规格未覆盖的决策,选最保守(fail-closed)的做法并在 PR 里列出。
> **Phase 2 会再次修改本机 live launchd(新增一个任务)。先 dry-run 核对,失败按规则回滚。**

## 已确诊的问题(Phase 0 先复现验证)

1. **双轨机器轨正在吃陈旧行情**:`dualtrack_market_feed.py` 只从本地 `data/market_data.db` **只读**取 GOLD 1m bar,自己从不写入。全仓库唯一会把新鲜 GOLD 1m K 线写进本地库的地方是 `pipelines/strategies.py::refresh_1m_feed()`(内部调用 `run_binance_usdm_1m_feed_import`),它挂在 `strategies` launchd 任务里,在 task-04 里被当作"纯舰队任务"停放了。结果:`strategies` 停放后,GOLD 1m bar 停止更新——`data_freshness` 检查目前显示 DEGRADED 且持续变陈旧。**这不是舰队专属基础设施,是双轨自己依赖的共享数据管道,聚焦模式把它误分类停掉了。**
2. **裸头寸检查没有新鲜度门,可能被冻结在错误状态**:验收时发现 `gold_1m_macd` 的 `live_reconciliation/current.json` 冻结在一次瞬时 SSL 错误(`BLOCKED_RECONCILIATION_UNKNOWN`)上,时间戳是几小时前——因为聚焦模式后没有任何任务再刷新它。人工手动跑一次只读对账确认交易所侧其实是空仓(`READY`),证明这只是被冻结的旧错误,不是真实裸头寸。但 `services/system_state.py::_check_naked_position` 是五个检查里**唯一没有新鲜度判断**的一个——`schedule`/`dualtrack_heartbeat`/`daily_review`/`data_freshness` 在数据陈旧时都会降级为 `UNKNOWN`,唯独这个会把任意久远的旧状态当作永远有效的真相展示。

## Phase 0 — 复现验证(只读)

1. 确认 `dualtrack_market_feed.py` 里没有任何写入 DB 的代码路径(只有 `_load_bars` 之类的只读查询)。
2. 确认 `pipelines/strategies.py::refresh_1m_feed()` 是全仓库唯一的 GOLD 1m 写入入口(`grep -rn "run_binance_usdm_1m_feed_import"`)。
3. 确认当前 `data/market_data.db` 里 GOLD 1m 最新 bar 时间戳与"当前 UTC 时间"的差距(应该在持续增长)。
4. 确认 `outputs/strategies/gold_1m_macd/live_reconciliation/current.json` 的 `checked_at` 是否早于当前时间较多(视你运行时刻而定,只需确认"这个字段存在且从未被聚焦模式后的任何调度刷新过"这个结构性事实,不要求具体分钟数)。
5. 任何一条与上述不符 → 停下,在 PR 里说明。

## Phase 1 — 新增最小行情心跳任务(纯代码,TDD)

**不要**把整个 `pipelines/strategies.py` 加回调度(它还会跑 leaderboard/sampler/frequency-governance/daily-review 等舰队记账逻辑,聚焦模式的名字应该名副其实——只跑双轨需要的东西)。

1. 新增 `pipelines/gold_1m_feed_heartbeat.py`(<60 行):唯一职责是调用 `run_binance_usdm_1m_feed_import(run_date)`(复用 `services/binance_futures_feed.py` 既有函数,不重新实现),`--date` 参数默认今天,打印结果 JSON。异常必须捕获并以非零退出码或结构化错误返回,不允许静默吞掉(注意这与 `pipelines/strategies.py::refresh_1m_feed` 的"吞错误不阻塞舰队"哲学不同——这里没有下游舰队要保护,喂食失败应该在日志里可见)。
2. `services/schedule_profiles.py`:`FOCUS_SCHEDULE_LABELS` 新增 `com.wendy.trading-orchestrator.gold-1m-feed`。`FULL_SCHEDULE_LABELS` 保持不变(full 模式下 `strategies` 任务本身已经在刷新 1m feed,不需要重复)。
3. `services/schedule_manager.py`:新增 `_gold_1m_feed_job(log_dir)`,`ProgramArguments` 指向 `pipelines.gold_1m_feed_heartbeat`,`StartInterval: 60`(与 `dualtrack-live-tick` 同量级,保证机器轨拿到的 bar 足够新鲜),`RunAtLoad: True`。只在 `build()` 生成的任务清单里,由 profile 决定是否包含(照抄现有 `_dualtrack_cycle_job` 之类的接入方式)。
4. 测试:两个 profile 的 job 集合断言更新(focus 现在是 5 个,full 仍是 9 个);新 pipeline 的成功/失败路径单测(mock `run_binance_usdm_1m_feed_import`)。

## Phase 2 — live 重装,加这一个任务(ops,谨慎)

复用 task-04 已经修好的安装器机制(teardown-wait、EIO 重试、孤儿处理、dashboard 最后装),不要重新发明:

1. 重新生成 5 个 plist(focus profile)。
2. `schedule_install --dry-run --json`:核对 plan 只是"新增 1 个 job(gold-1m-feed),其余 4 个不变、无需重装"。
3. 执行安装。
4. 验收:
   - `schedule_status --json`:`required_count=matching_generated_count=loaded_count=5`
   - `launchctl list | grep com.wendy.trading-orchestrator` 恰好 5 个
   - 等待 ≥90 秒后,`data/market_data.db` 的 GOLD 1m 最新 bar 时间戳比 Phase 0 记录的基线更新(证明喂养确实在跑)
   - `curl /api/system-state`:`data_freshness` 不再是 DEGRADED(假设 Phase 3 的新鲜度阈值内)
   - 原有 4 个任务(dashboard/deadman-ping/dualtrack-cycle/dualtrack-live-tick)全部仍 loaded,dashboard 仍 200
5. 任何原本 loaded 的任务掉线 → 立即回滚,核对回到 4/4/4 起点,停下报告。

## Phase 3 — 裸头寸检查新鲜度门(纯代码,TDD,独立于 Phase 1/2)

`services/system_state.py::_check_naked_position`:

1. 新增一个模块级常量 `NAKED_POSITION_MAX_AGE = timedelta(hours=1)`(与其余检查同风格,不硬编码进函数体)。
2. 逐条读取每个策略的 `live_reconciliation/current.json` 时,额外解析 `checked_at`(照抄 `_check_schedule`/`_check_data_freshness` 里现成的时间戳解析与新鲜度比较写法,别重新发明);缺失/无法解析/超龄 → 该策略的检查结果视为 `UNKNOWN`(reason 说明原因),**不得**因为拿不到新鲜数据就默认 `RUN`,也不得把陈旧的 `BLOCKED` 状态当真实告警继续上报。
3. 若某策略的 `demo_trading` 绑定当前是关闭状态(即该策略不是 `active_strategy_id` 且 `demo_trading.enabled=false` 时全局不存在活跃 demo 策略),其 `live_reconciliation` 陈旧属于预期(没人会再刷新它)——此时报告 `RUN`(带 `skipped`/说明字段,语义参考 `dualtrack_heartbeat` 的 `not_scheduled` 处理方式),而不是每次都报 `UNKNOWN` 制造噪音。**这条策略性判断请在 PR 里明确写出你的实现选择**,如果你认为一律 `UNKNOWN` 更 fail-closed 也可以,但要说明取舍。
4. 测试:新鲜 BLOCKED → 仍 BLOCKED;陈旧 BLOCKED(超过新鲜度门,且该策略不是当前 demo 绑定)→ 按你 3. 的选择处理并有明确断言;陈旧且策略仍是活跃 demo 绑定 → UNKNOWN(不允许当作 RUN);`checked_at` 缺失/损坏 → UNKNOWN。

## 硬约束

- 不碰订单/执行/broker/风控代码;R4 锁定文件零 diff。
- Phase 1/3 是纯代码,可独立验证;Phase 2 是唯一动 live 系统的部分。
- 秘密值不得出现在提交物中。
- UTC-aware;显式错误处理(除 Phase 1 规格里明确要求"不吞错误"的那一处);类型注解;新文件 <100 行(Phase 1 的 pipeline 文件应远小于此)。
- TDD 先红后绿;全仓 `python3 -m pytest -q` 全绿。
- decision-log.md 记录:1m 喂养缺口的根因(共享基础设施被误分类为舰队专属)、新增任务的职责边界、裸头寸新鲜度门的取舍。

## DOD

- [ ] Phase 1/3 测试全绿 + 全仓全绿
- [ ] Phase 2 验收 5 条全过,回执贴进 PR
- [ ] `/api/system-state` 的 `data_freshness` 恢复健康(或说明为何仍未达标)
- [ ] PR 列出所有自行决定的点(尤其 Phase 3 第 3 条的取舍)
