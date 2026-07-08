# Codex 任务 05:指挥台(DualTrack 聚焦模式版,Phase C)

> 背景:原设计简报 `docs/command-center-brief.md` 是给 21 策略舰队写的(决策漏斗、GateWaterfall、21 行策略表)。所有者已于 2026-07-08 收缩到人机双轨聚焦模式(见 decision-log "DualTrack Focus Mode Schedule"),舰队相关设计**全部作废**。本任务是**重新按聚焦模式设计**的指挥台,不是简报的字面实现——不要去读 command-center-brief.md 的组件清单照抄。
> 你(Codex)严格按本规格实现。规格未覆盖的决策,选最保守做法并在 PR 里列出。
> **明确排除**:不碰订单/执行/broker/风控代码;不重设计现有四个房间的内部布局(那是 Phase D,还没排期);不新建决策漏斗/GateWaterfall/策略表这类舰队概念。

## 定位

一页,回答 5 个问题,10 秒内,不用跳转:
1. 系统现在健康吗?卡在哪?
2. 当前双轨周期是什么状态(盲答/盘中/已揭示),还剩多少时间?
3. 人机比分累计到现在,谁在赢?
4. 我的口述方向分校准得准不准?
5. 下一步安全动作是什么?

这是全站默认落地页(`/` 重定向到它),取代"打开哪个房间"这个选择题本身。

## 复用清单(60%+ 是组合,不是新建——先读这些,不要重新发明)

**已验证存在且返回真实数据的现成 API(逐字段核对,不要猜字段名):**

- `GET /api/system-state`(`services/system_state.py`)—— `overall`(RUN/DEGRADED/BLOCKED/UNKNOWN)+ `checks[]`(每项含 `id/status/reason/room/cta`,部分带 `evidence`)。**这就是裁决带和阻塞栈的完整数据源,不需要新写聚合逻辑。**
- `GET /api/dualtrack/cycle/current`(`pipelines/dashboard_server.py` ~140 行)—— 实测返回:
  ```json
  {"cycle_id": "2026-07-08_DAY", "kind": "DAY", "start": "...", "end": "...",
   "lock_deadline": "...", "start_cst": "...", "end_cst": "...",
   "countdown_seconds": 23018,
   "effective_plan_status": {"has_effective_plan": true, "machine_stands_down": false, "author": "human"}}
  ```
- `GET /api/dualtrack/ledger`(同文件 ~155 行)—— 实测返回 `daily[]`,每项含 `date`、`cycles{cycle_id: {machine, human, delta_machine_minus_human, total}}`、`tracks{machine:{realized_pnl}, human:{realized_pnl}}`、`total_pnl`。**人机比分从这里累加,不要重新计算。**
- `services/bias_ledger.py::BiasLedger` 的 `summary.json`(`{output_root}/bias_ledger/summary.json`)—— `total/adjudicated/correct/wrong/undecidable/hit_rate/mean_brier/last_10`。**当前文件可能不存在(还没有裁决记录)——这是正常空态,不是错误**,页面要能优雅处理"文件不存在"。
- `services/dualtrack_cycle_heartbeat.py::DualTrackCycleHeartbeat.run()` —— 已有 `status/expected_boundary/latest_artifact_at/missed_boundaries`,用它回答"上一个周期收盘评分了没有",**不要新建 review-marker 机制**(旧简报的 POST /api/review-marker 整个作废,这个用现成心跳就够)。
- `assets/shell.js` + `assets/shell.css` —— 全局顶栏(房间导航+状态灯+新鲜度)已经在五个现役页面上跑着,指挥台**必须复用同一套 shell**,不要为指挥台单独做一套导航或状态灯。

## Part 1 — 新增只读组合端点

`GET /api/command-center-state`:纯组合 `/api/system-state` + `/api/dualtrack/cycle/current` + `/api/dualtrack/ledger` + bias-ledger summary(若存在) + 心跳状态,不做任何新的判定逻辑、不写任何文件、不开网络。建议新文件 `services/command_center.py`(<200 行),`build_command_center_state(output_root=None, as_of=None) -> dict`,在 `dashboard_server.py` 里照抄 `/api/system-state` 的注册方式接一个路由。

返回结构(建议,允许你按实际数据调整字段名,但语义要覆盖全部,并在 PR 里注明改动):

```json
{
  "generated_at": "...",
  "system": { "overall": "...", "checks": [...] },
  "cycle": { "cycle_id": "...", "kind": "...", "countdown_seconds": 0, "phase": "blind|intraday|revealed", "effective_plan_status": {...} },
  "scoreboard": { "total_machine_pnl": 0, "total_human_pnl": 0, "delta": 0, "cycles_scored": 0, "last_cycle": {...} },
  "calibration": { "available": true, "total": 0, "hit_rate": null, "mean_brier": null, "last_10": [] },
  "cycle_liveness": { "status": "fresh|stale|not_scheduled", "reason": "..." },
  "next_action": { "kind": "none|blind_answer|fix|review", "label": "...", "target": "..." }
}
```

**`phase` 派生规则**(fail-closed,`system` 抛异常/不可读 → 整个端点仍返回 200,`system.overall` 用 UNKNOWN,其余字段允许为 null,不许 500):
- `now < lock_deadline` → `blind`
- `lock_deadline <= now < end` → `intraday`
- `now >= end`(或心跳显示该周期已有产物)→ `revealed`

**`next_action` 派生规则**(复用系统状态里"没有人工审批中间态"的既有原则,不要发明新分类):
- `system.overall == BLOCKED` → `kind: "fix"`,`label` = 第一个 BLOCKED check 的 reason,`target` = 其 `room`
- 否则,`phase == "blind"` 且 `effective_plan_status.has_effective_plan == false` → `kind: "blind_answer"`,`label`:「提交本周期盲答作战单」,`target: "dualtrack"`
- 否则 → `kind: "none"`,`label`:「系统运行正常,等待下一周期」

## Part 2 — `command-center.html`(单文件,复用 shell,黑底琥珀风格延续 dualtrack-v5)

顶栏:注入既有 `assets/shell.js`/`shell.css`,不重做。

页面骨架(从上到下,一屏,1440 宽):

1. **VerdictBand**(全宽,唯一大号字):四态色点+词(RUN 绿/DEGRADED 琥珀/BLOCKED 红/UNKNOWN 灰),取 `system.overall`;下方一行 `next_action.label` + 若 `kind != "none"` 的一个跳转按钮。
2. **BlockerStack**(左 1/3):遍历 `system.checks`,状态非 RUN 的按 BLOCKED>UNKNOWN>DEGRADED 排序显示,每条一句话+ `room`标签 + 跳转按钮(照抄现有 shell 横幅的"一句话+一个 CTA"惯例,不要照搬旧简报的证据抽屉设计——那是给舰队 gate 写的,双轨用不上)。全部 RUN → 显示"当前无阻塞"空态,不留空白。
3. **CycleCard**(右上):`cycle_id`/`kind`/倒计时(`countdown_seconds` 格式化为 `HH:MM:SS` 且每秒本地递减,不必每秒请求接口)/`phase` 徽章/`effective_plan_status`(谁的计划在生效、机器是否停手)。
4. **Scoreboard**(右中):机器轨 vs 人轨累计已实现盈亏并排对比 + delta(正负颜色区分)+ 已评分周期数。零周期时显示"暂无已收盘周期"空态。
5. **CalibrationCard**(右下):`calibration.available == false` → 显示"尚未有人轨方向分裁决记录"空态,**不是报错、不是隐藏这个卡片**;有数据时显示 `hit_rate`(百分比)+ `mean_brier`(4 位小数)+ 最近 10 条裁决的对错色条(绿/红/灰三色,对应 correct/wrong/undecidable)。
6. **CycleLivenessRow**(底部细条):心跳状态一句话,`stale` 时用 shell 已有的琥珀/红配色而不是新发明一种。

交互:点 BlockerStack 里任意一条 → 跳到其 `room` 对应页面(用 shell.js 现成的房间 URL 映射,不新造);CycleCard 不需要点击交互(v1 只读);其余卡片纯展示。

轮询:与 shell 一致,60 秒;fetch 失败 → 整页降级为"数据不可读"的灰色状态(参照 shell.js 的 `renderUnknown` 思路),**不允许在拿不到数据时显示任何绿色/RUN**。

## Part 3 — Shell 与路由收尾

1. `pipelines/dashboard_server.py`:`/` 重定向到 `command-center.html`;把它加入服务页面白名单(照抄现有条目)。
2. `assets/shell.js`:房间导航列表新增「指挥台」为默认高亮项,放最前;**评估是否把「驾驶舱」(`dashboard-v4.html`)从主导航三项里移除**——它展示的是已停用的 `gold_1m_macd` 单策略,聚焦模式下已不是主要房间。若移除,`dashboard-v4.html` 本身不删除、不加 redirect,只是不再出现在顶栏三个主链接里(仍可通过 URL 直达)。这是本任务里少数需要你自行判断的点,**做出选择并在 PR 里说明理由**。
3. 现有 `dashboard-dualtrack-v5.html` 的顶栏返回入口不变。

## TDD(先红后绿)

`tests/test_command_center_api.py` 至少:
1. 全部输入健康 → `system.overall=RUN`,`phase` 按 Part 1 规则正确派生(构造 fixture 让 `now` 落在三种 phase 各一次)
2. `system-state` 内部抛异常/不可读 → 端点仍 200,`system.overall=UNKNOWN`,不 500
3. bias-ledger summary 文件不存在 → `calibration.available=false`,不报错
4. `system.overall=BLOCKED` → `next_action.kind="fix"` 且 `label`/`target` 取自第一个 BLOCKED check
5. 盲答期且无 effective plan → `next_action.kind="blind_answer"`
6. ledger 多日多周期数据 → scoreboard 的累计 machine/human/delta 加总正确(手算一组固定样例)

`tests/test_command_center_static.py` 至少:
7. `command-center.html` 引用 `shell.js`
8. `/` 重定向到 `command-center.html`(照抄既有 redirect stub 测试风格)
9. 页面对 `calibration.available=false`、`scoreboard` 零周期两种空态各有对应的展示文案(grep 静态 HTML/JS 里的空态字符串,不要求真的起服务器渲染)

## 硬约束

- 只读组合,新端点不写任何文件、不开网络、不碰订单/执行/broker/风控/completion_audit 的判定逻辑本身(可以读它已有的输出)。
- 不引入前端框架/构建步骤/外部 CDN;vanilla JS,复用现有 shell。
- UI 永远不许比后端乐观:任何字段拿不到就显示明确的"未知/暂无",绝不默认绿色。
- 秒级倒计时用本地 `setInterval` 递减显示,不得每秒打后端。
- UTC-aware;显式错误处理;类型注解;新文件 <200 行(服务层)/ 单文件 HTML 不设硬性行数但保持现有页面的密度风格。
- TDD 先红后绿;全仓 `python3 -m pytest -q` 全绿;R4 锁定文件零 diff。
- decision-log.md 追加决策与 gotchas(含:为什么弃用旧简报的决策漏斗/策略表/review-marker 设计,以及驾驶舱是否移出主导航的决定)。

## DOD

- [ ] 上述 9 类测试全绿 + 全仓全绿
- [ ] 手工验收:打开 `command-center.html`,console error = 0;VerdictBand/CycleCard/Scoreboard 显示的数字与直接 curl 对应 API 的结果一致
- [ ] `/` 重定向生效
- [ ] PR 列出所有自行决定的点(尤其驾驶舱导航去留)
