# Codex 任务 02：修复调度漂移 + close-cycle 存活心跳（DT-INV-3）

> 设计出自 2026-07-08 系统全景审查（见 [[system-audit-2026-07-08]] 记忆与 decision-log）。
> 你（Codex）严格按本规格实现，不扩展范围。规格未覆盖的决策，选最保守（fail-closed）的做法并在 PR 描述里列出。
> **本任务有一个步骤会修改 Park 本机的 live launchd 状态（Phase 2）。那一步必须先 dry-run、核对范围、再执行，且全程打印 launchctl 回执。其余全部是可单测的代码工作。**

## 已确认的根因（不要重新质疑，直接从这里开始；但要在 Phase 0 用命令复现验证）

1. **调度漂移**：`python3 -m pipelines.schedule_status --json` 当前返回 `status: "stale_installed"`，9 个 launchd job 全部 `matches_generated: false`（`matching_generated_count: 0`）。原因：本周新增了 `dualtrack-cycle` / `dualtrack-live-tick` / `deadman-ping` 三个 job 且既有 job 定义有变，生成的 plist 在 `2026-07-06T03:30:44Z` 重新生成，但从未 bootstrap 进 `~/Library/LaunchAgents`。安装的是旧版 plist。
2. **静默数据腐坏**：`dualtrack-cycle`（`StartInterval: 60`，`--event auto`，内部按 `cycle_hours_utc` 决定何时 `close_cycle`）实际没有在跑，导致 `close_cycle` 自 ~2026-07-06 01:00 UTC 起停摆。证据：`outputs/dualtrack/ledger/daily/` 最新只有 `2026-07-05.json`，`outputs/dualtrack/attribution/` 最新只有 `2026-07-05_NIGHT.json`。周期每 12h 关闭一次（`configs/dualtrack.yaml` 的 `cycle_hours_utc`：day_start=01:00 UTC，night_start=13:00 UTC），因此已漏掉 3–4 个周期未评分。
3. **检测盲区（本任务最有价值的部分）**：`services/completion_audit.py` 的 `_schedule_artifacts`（约 515–545 行）只校验 launchd **label 是否存在于生成的调度里**，从不校验 `close_cycle` 的**产物是否新鲜**。所以上面的 40h+ 停摆对 audit 完全不可见，看板还在显示"实时"。`live_activation` 的 `schedule_active` 检查确实抓到了 `stale_installed`（这是 `dry_run_ready` 当前 fail 的原因），但它抓的是"plist 漂移"，抓不到"job 在跑但产物在腐坏"。

## 动手前必读

- `services/schedule_manager.py` — `build()` 生成 9 个 job 定义；`_dualtrack_cycle_job`（StartInterval 60）、`_dualtrack_live_tick_job`（StartInterval 300）、`_deadman_ping_job`
- `services/schedule_status.py` — 漂移检测逻辑（generated vs installed 的比对、`matches_generated`、`loaded`）
- `services/schedule_installer.py` — 真正调用 `launchctl bootout/bootstrap/kickstart` 的安装器；有 `install()` / `rollback()` / dry-run plan；non-dry-run 需要 `--acknowledgement` + `--package-id`
- `pipelines/schedule_install.py` — 安装 CLI（`--dry-run` / `--acknowledgement` / `--package-id` / `--rollback` / `--receipt`）
- `services/completion_audit.py` — `_schedule_artifacts`（现有 label-only 检查，你要在它旁边加新的产物新鲜度检查段）
- `services/dualtrack_clock.py` + `configs/dualtrack.yaml` 的 `cycle_hours_utc` — 周期边界的唯一真相源（**从这里派生阈值，不要硬编码 12h/01:00/13:00**）
- `pipelines/dualtrack_cycle_runner.py` — `--event auto` / `close_cycle` / `fast-forward` 的语义
- `tests/test_schedule_manager.py`、`tests/test_completion_audit.py`、`tests/test_dualtrack_dt8_cycle_runner.py` — 测试风格

⚠️ 本仓库有 local-vs-UTC 混用导致 fail-open 的历史。所有时间戳一律 UTC-aware。

## 交付物

1. **新增 `services/dualtrack_cycle_heartbeat.py`（<300 行）** — DT-INV-3 的核心：判定 close-cycle 产物是否新鲜。
2. **修改 `services/completion_audit.py`** — 新增 `_dualtrack_cycle_liveness` 审计段，接进现有 checklist（照抄 `_human_bias_ledger` 的接法）。
3. **新增 `tests/test_dualtrack_cycle_heartbeat.py`** — TDD，先红后绿。
4. **Phase 2 的 launchd 重装（ops，见下，产物是 install 回执 + 重装后的 schedule_status 健康输出，贴进 PR 描述）。**
5. **Phase 3 的漏周期诚实处理（见下）。**
6. decision-log.md 追加决策与 gotchas。

---

## Phase 0 — 复现验证（只读，先做）

跑并在 PR 里贴出：`schedule_status --json` 的 status/mismatched_jobs；`outputs/dualtrack/ledger/daily/` 与 `attribution/` 的最新文件日期；据 `cycle_hours_utc` 算出从最后一个产物到现在漏了哪几个周期边界。确认根因三条与上面一致；若有任何一条不符，停下并在 PR 里说明，不要盲目继续。

## Phase 1 — DT-INV-3 存活心跳（纯代码，TDD，最高优先级）

**不变量**："只要 `dualtrack-cycle` 在应装调度集合里（即系统被配置为运行双轨），最近一个已过去的周期边界就必须有对应的 close-cycle 产物；否则 fail（BLOCKED/STALE），绝不 pass。"

`services/dualtrack_cycle_heartbeat.py` 要点：
- 从 `configs/dualtrack.yaml` 的 `cycle_hours_utc` 派生周期边界（day_start/night_start，每 12h 一个边界），**不要硬编码**。
- 计算"当前时刻之前最近一个已关闭的周期边界" `expected_boundary`（UTC）。
- 检查 `outputs/dualtrack/ledger/daily/` 和 `outputs/dualtrack/attribution/` 是否存在覆盖到 `expected_boundary` 的产物。
- 加宽限期常量 `CLOSE_GRACE_MINUTES`（默认 120，做成模块常量、可构造函数覆盖）：边界过去不足宽限期时不算 stale（给 auto job 留出运行时间）。
- 返回结构化结果：`status`（`fresh` / `stale` / `not_scheduled`）、`expected_boundary`、`latest_artifact_at`、`missed_boundaries: [...]`、`reason`。
- **门控防误报**：只有当 `dualtrack-cycle` 在 `Schedule*` 的 required/generated 集合里时才判 stale；若系统没配双轨（job 不在集合里），返回 `not_scheduled` 且不算失败。
- **fail-closed**：产物目录不存在、时间戳无法解析、config 缺 `cycle_hours_utc` → 一律 `stale`/错误态并说明，**绝不**因为"读不到"就当作 fresh。

`completion_audit._dualtrack_cycle_liveness`：只读地调用心跳服务，`fresh`/`not_scheduled` → pass，`stale` → fail 且 evidence 带 `missed_boundaries` 和 `latest_artifact_at`。接进 `run()` 的 checklist（挨着 `_schedule_artifacts`）。

`tests/test_dualtrack_cycle_heartbeat.py` 至少覆盖：
1. 最近边界有产物 → `fresh` → audit pass
2. 最近边界缺产物且已过宽限期 → `stale` → audit fail，`missed_boundaries` 非空
3. 边界刚过、还在宽限期内 → 不算 stale（不误报）
4. `dualtrack-cycle` 不在应装集合 → `not_scheduled` → pass（不误报）
5. 产物目录不存在 / 时间戳损坏 → fail-closed（stale/错误，不是 fresh）
6. 边界派生正确：给定固定 `as_of` 和 `cycle_hours_utc`，`expected_boundary` 与 `missed_boundaries` 计算正确（手算一组固定样例）

## Phase 2 — 重装 launchd（ops，会改动 live 系统，谨慎）

**顺序不可跳：**
1. 先 `python3 -m pipelines.schedule_install --dry-run --json`，贴出 plan。核对 plan 只涉及那 **9 个已知 label**，没有多余/意外的 job。
2. 从 dry-run 结果取得所需的 `--acknowledgement` 短语与 `--package-id`。
3. 执行非 dry-run 安装（`--acknowledgement ... --package-id ...`）。**逐条打印 launchctl bootout/bootstrap/kickstart 的回执**。
4. 重装后 `python3 -m pipelines.schedule_status --json` 必须回到健康：`status` 非 `stale_installed`、`matching_generated_count == required_count == 9`、`loaded_count == 9`、`mismatched_jobs == []`。
5. **回归保护**：确认原本在跑的 8 个 job 重装后仍 `loaded: true`；若重装后任何一个 job 变成未加载或安装数减少，立即 `--rollback` 恢复，并停下报告，不要留下比开始时更差的状态。

> 若你（Codex）判断不应自行改动 Park 本机的 live launchd，可只完成到"给出经 dry-run 验证过的确切安装命令"，把 Phase 2 第 3 步留给 Park 手动执行——在 PR 里明确说明你停在了哪一步。

## Phase 3 — 漏掉的周期：诚实处理，绝不编造

对 Phase 0 查出的漏掉周期：
- **只有当该周期的 intraday 数据完整存在**时，才允许用 `dualtrack_cycle_runner` 的 fast-forward 补算评分。
- **数据不完整/缺失**的周期：按本仓库"缺数据=无效，不是 0"的原则，标记为 `void`（带 `reason=schedule_drift_gap`）写进账本，**不要**用不完整数据补出一个评分。
- 在 PR 里列出每个漏掉周期最终是 `backfilled` 还是 `void` 及依据。

## Phase 4 — 验证（DOD）

- [ ] Phase 1 的 6 类测试全绿；全仓 `python3 -m pytest -q` 全绿
- [ ] 重装后 `schedule_status --json` 健康（9/9 匹配且加载），回执贴进 PR
- [ ] 重装后确认 `dualtrack-cycle` 已加载，并（等到下一个周期边界+宽限期后，或用一次手动 `--event auto`）产出新的 close-cycle 产物，`_dualtrack_cycle_liveness` 转 pass
- [ ] `python3 -m pipelines.live_activation --json` 的 `schedule_active` 由 fail 转 pass（漂移消除后 `dry_run_ready` 应不再卡在这一项）
- [ ] 漏周期已按 Phase 3 诚实处理（backfilled 或 void），无编造评分
- [ ] PR 描述列出：所有规格未覆盖、由你自行决定的点

## 硬约束

- **不碰**订单/执行/broker/风控路径的任何代码；本任务只涉及调度、审计、双轨周期产物。
- 时间戳一律 UTC-aware；阈值/边界从 config 派生，不硬编码。
- 显式错误处理，禁止裸 `except` 吞错；全函数类型注解；dataclass `frozen=True`；PEP 8；新文件 <300 行。
- 心跳检测宁可**误报为 stale**（fail-closed）也不可漏报为 fresh。
