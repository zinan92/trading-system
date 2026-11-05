# Codex 任务 03:统一前端 Shell + 单一状态源(Phase A+B)

> 设计出自 2026-07-08 前端 UI/UX 全面审查(四页实测截图 + console 错误 + 导航连线 grep)。
> 你(Codex)严格按本规格实现,不扩展范围。规格未覆盖的决策,选最保守的做法并在 PR 描述里列出。
> **前置条件:本任务必须在 task-02(schedule drift)完成并提交之后开始**,避免同一工作区冲突。
> **明确排除**:不新建指挥台页面(Phase C)、不重设计任何页面内部布局(Phase D)、不碰订单/执行/broker/风控代码。

## 背景:审查发现的三类问题(你要修的就是这些)

1. **没有单一事实源**:同一时刻,replay-v4 显示某笔交易"持仓中 -18.30R"(实为裸头寸,止损未挂),v4 驾驶舱显示"护单 1/1 已覆盖全绿",OPS 显示"2 critical"。每页各读各的 artifact 拼装现实,系统最危险的状态(BLOCKED_NAKED_POSITION_SUSPECTED 已挂 3 天)在任何页面都没有以危险的样子全局出现。
2. **导航图断裂 + 死链**:只有 dashboard-dualtrack-v5.html 有完整导航;dashboard-v4.html 只链 ops;dashboard-replay-v4.html 零出站链接;**ops-dashboard.html 的 "Open Trader Console" 指向已废弃的 dashboard-v3.html**。
3. **OPS 页在轮询已不存在的端点**:`/api/dashboard?date=...&view=ops` 与 `/api/public-access-health` 均 404,加上 12 个按日期的 `outputs/...` artifact 404,每轮 14 个 404 × 反复轮询,console 常年 42+ errors。

## 动手前必读

- `pipelines/dashboard_server.py` — 现有 HTTP server 与 API 路由惯例(如 `/api/dualtrack/...`),新端点照抄它的注册方式
- `ops-dashboard.html`(3507 行)— 定位所有 fetch 调用(搜 `api/dashboard`、`public-access-health`、`outputs/`)与 `dashboard-v3.html` 链接
- `dashboard-dualtrack-v5.html` — 它已有的局部导航条(指挥台/双轨作战台/回放/运维)是本次全局 shell 的语义蓝本
- `outputs/strategies/gold_1m_macd/live_reconciliation/current.json` — BLOCKED 状态的真实样例(system_state / escalation_action 字段)
- `tests/test_dashboard_server.py`、`tests/test_dashboard_dualtrack_static.py`、`tests/test_dashboard_dualtrack_replay_static.py`、`tests/test_dashboard_v3_static.py` — 现有静态契约测试的断言方式;你的改动不许无解释地弄红它们
- 若 `services/dualtrack_cycle_heartbeat.py` 已存在(task-02 的产物),把它的产物也纳入状态聚合的 DEGRADED 来源

## 交付物

1. **新增 `GET /api/system-state`**(dashboard_server 内,或按其惯例拆到 services 层,新文件 <400 行)
2. **新增 `assets/shell.js` + `assets/shell.css`**(各 <400 行,vanilla JS,无任何外部 CDN 依赖)
3. **五个现役页面注入 shell**:ops-dashboard.html、dashboard-v4.html、dashboard-replay-v4.html、dashboard-dualtrack-v5.html、dashboard-dualtrack-replay.html
4. **OPS 页修复**(死端点、404 风暴、stale link)
5. **旧版下线**:dashboard-v3.html、dashboard.html、dashboard-replay.html 改为 redirect stub(照抄 dashboard-v2.html 的样式)
6. **测试**:新增 `tests/test_system_state_api.py` + `tests/test_frontend_shell_static.py`;更新受影响的既有静态测试
7. decision-log.md 追加决策与 gotchas

## Part 1 — `/api/system-state`(单一事实源,fail-closed)

返回结构:

```json
{
  "generated_at": "<UTC ISO>",
  "overall": "BLOCKED | UNKNOWN | DEGRADED | RUN",
  "checks": [
    {"id": "naked_position", "status": "BLOCKED", "reason": "gold_1m_macd BLOCKED_NAKED_POSITION_SUSPECTED since 2026-07-05", "room": "ops", "cta": "处理裸头寸"},
    {"id": "schedule", "status": "DEGRADED", "reason": "...", "room": "ops", "cta": "..."},
    ...
  ]
}
```

**聚合规则(严格按此优先级)**:`overall = BLOCKED`(任一 check 为 BLOCKED)> `UNKNOWN`(任一为 UNKNOWN)> `DEGRADED`(任一为 DEGRADED)> `RUN`(全部通过)。UNKNOWN 排在 DEGRADED 之前是有意的:读不到数据比已知的降级更危险,fail-closed。

**v1 检查项(只读 artifact,全部走文件,不开网络、不 per-request 起子进程)**:
1. `naked_position`:扫 `outputs/strategies/*/live_reconciliation/current.json`,`system_state` 以 `BLOCKED` 开头 → BLOCKED,附策略名与起始日期;文件缺失/无法解析 → UNKNOWN。
2. `schedule`:读 schedule_status 的最新 artifact(去 `outputs/schedules/` 找现有产物;若现有代码只打印不落盘,则在 schedule_status service 加一个写 `outputs/schedules/status_current.json` 的落盘点——这是允许的最小后端改动);`stale_installed` → DEGRADED;artifact 缺失或超过 24h → UNKNOWN。
3. `dualtrack_heartbeat`(若 task-02 的心跳服务已存在):stale → DEGRADED;not_scheduled/fresh → RUN 贡献;服务不存在则跳过此项并在 payload 里注明 `skipped`。
4. `daily_review`:读最新 daily-review 产物,fail → DEGRADED;缺失 → UNKNOWN。
5. `data_freshness`:读 market DB 或现成的 preflight artifact 判断 GOLD 1m 最新 bar 年龄,> 30 分钟 → DEGRADED;读不到 → UNKNOWN。

**性能**:端点响应 < 200ms;允许内存缓存,TTL 30 秒;shell 轮询间隔 60 秒。
**fail-closed 硬规则**:任何 check 的输入文件缺失、损坏、时间戳无法解析,该 check 一律 UNKNOWN,**绝不**默认 RUN。整个端点抛异常时返回 `overall: "UNKNOWN"` + HTTP 200(shell 要能渲染它),不是 500。

## Part 2 — 全局 Shell(assets/shell.js + shell.css)

顶栏(fixed,全部页面一致),从左到右:
1. **房间导航**:`作战台`(dashboard-dualtrack-v5.html)| `驾驶舱`(dashboard-v4.html)| `运维`(ops-dashboard.html)。当前页高亮。回放页不进导航(它们是从房间进入的上下文页,shell 在回放页显示"← 返回作战台/驾驶舱"的返回链接,按 URL 参数或 document.referrer 判断来源,判断不了就默认驾驶舱)。
2. **全局状态灯**(顶栏正中,这是整个 shell 的视觉重心):一个色点 + 一个词——`运行`(绿)/`降级`(琥珀)/`阻断`(红)/`未知`(灰)。点击 → ops-dashboard.html。数据来自 `/api/system-state`,fetch 失败时显示灰色`未知`(fail-closed,不隐藏)。
3. **右侧**:数据新鲜度"X 分钟前"(用 system-state 的 generated_at 计算,统一全站范式)。

**横幅规则**:`overall == BLOCKED` → 顶栏下方通栏红色横幅:第一条 BLOCKED check 的 reason + CTA 按钮(链到其 `room`);`DEGRADED` → 细琥珀横幅;`UNKNOWN`/`RUN` → 无横幅(UNKNOWN 已由灰灯表达)。横幅不可关闭(dismiss)——BLOCKED 状态的可见性就是本任务的存在理由。

**注入方式**:每页 `<head>` 加一个 `<link>` + `</body>` 前加一个 `<script src="assets/shell.js" defer>`,shell 自己创建 DOM 并给 `body` 加 padding-top,**不改动任何页面的既有 DOM 结构**。dashboard-dualtrack-v5.html 已有的局部导航条:用 shell.css 隐藏(`display:none`,通过它现有的选择器定位),**不删除其 HTML**(既有静态测试可能断言它存在)。

**样式约束**:深色(与 dualtrack-v5 的黑底琥珀风一致,浅色的 ops 页上 shell 仍保持深色——shell 是全站恒定物);无 emoji 图标(状态灯用 CSS 圆点);可点元素 cursor-pointer + 边框/颜色 hover,不用 scale;全中文标签。

## Part 3 — OPS 页修复(最小改动,不重设计)

1. 删除对 `/api/dashboard?view=ops` 和 `/api/public-access-health` 的 fetch(连同只被它们填充的面板一起移除或显示"数据源已下线";如果该面板还有其他数据源则保留面板只删死 fetch)。
2. 对按日期的 `outputs/...` fetch:404 时静默进入"无数据"状态,**不再重试轰炸**(轮询循环里对已 404 的资源本轮不再重复请求);console 里不留 error(用 fetch 的 response.ok 分支处理,不让浏览器打印失败)。目标:页面加载完成后 console error 数为 0。
3. `Open Trader Console` 及一切 `dashboard-v3.html` 链接 → `dashboard-v4.html`。

## Part 4 — 旧版下线

`dashboard-v3.html`、`dashboard.html`、`dashboard-replay.html` 整文件替换为 redirect stub(逐字照抄 `dashboard-v2.html` 的结构,目标分别为 v4 / v4 / dashboard-replay-v4.html,保留 query/hash 透传)。先 `grep -rn` 这三个文件名在整个仓库(pipelines/services/tests/docs)的引用,把引用逐一改指新目标或更新断言;`tests/test_dashboard_v3_static.py` 重写为断言 stub 语义(存在 meta refresh + JS replace + 正确目标),不要删测试文件。

## TDD(先红后绿)

`tests/test_system_state_api.py` 至少:
1. 构造 BLOCKED reconciliation fixture → overall=BLOCKED,check 带 reason/room
2. 无任何 artifact → 各 check UNKNOWN → overall=UNKNOWN(不是 RUN——这是 fail-closed 的核心断言)
3. 只有 schedule stale → overall=DEGRADED
4. 全部健康 fixture → overall=RUN
5. 损坏 JSON → 该 check UNKNOWN,端点仍 200
6. 优先级:同时存在 DEGRADED+UNKNOWN → overall=UNKNOWN;同时 BLOCKED+UNKNOWN → BLOCKED

`tests/test_frontend_shell_static.py` 至少:
7. 五个现役页面都含 shell.js 引用
8. ops-dashboard.html 不再含 `dashboard-v3.html` 字符串,也不再含两个死端点路径
9. 三个 stub 文件含正确 redirect 目标
10. shell.js 不含任何外部域名(http/https 的非本机 URL)

## DOD

- [ ] 上述 10 类测试全绿;全仓 `python3 -m pytest -q` 全绿(受影响的既有静态测试已按意图更新,PR 里逐个列出改了哪些断言、为什么)
- [ ] 手工验收路径写进 PR:打开五个页面各一次,console error = 0;顶栏出现且状态灯反映当前真实状态(当前应为 BLOCKED——裸头寸还挂着,红横幅应该在每一页可见)
- [ ] `curl http://127.0.0.1:8765/api/system-state` 响应 < 200ms,payload 含全部 check
- [ ] 从 ops 点 Trader Console 到达 v4;从任一页面可两跳内到达其余任何房间
- [ ] PR 描述列出所有规格未覆盖、由你自行决定的点

## 硬约束

- 不碰订单/执行/broker/风控/completion_audit/schedule 安装逻辑(唯一例外:schedule_status 允许加落盘点)
- 不引入任何前端框架/构建步骤/外部 CDN;vanilla JS + 本地文件
- 不改四个现役页面的内部布局与业务逻辑(只注入 shell、只修死链死端点)
- 时间戳一律 UTC-aware;显式错误处理;新文件 <400 行
- UI 永远不许比后端乐观:数据取不到就显示未知/灰,绝不显示绿
