# Codex 任务 06:作战台功能与清晰度修复(7 项)

> 目标:修复 `dashboard-dualtrack-v5.html`(人机双轨作战台)上 7 个真实的功能/清晰度 bug。**不做视觉系统重设计**(颜色/字体/边框/加粗一致性、框体美观、文字边距那一整套 = Phase D,本任务明确排除)。
> 你(Codex)严格按本规格实现。规格未覆盖的决策,选最保守做法并在 PR 里列出。
>
> ## 两条不可违反的硬约束(违反任何一条 = 整个任务作废重做)
>
> 1. **盲答协议神圣不可侵犯。** 参见 `docs/dualtrack-critical-blind-protocol-corruption.md`。当前盘中主图(`#mainChart`)只喂 `state.human.fills`(人轨自己的标记)+ 人轨计划线,**绝不显示机器轨点位**;机器点位只在收盘后的"机器轨 MACHINE"卡片和回放页出现。本任务任何图表/标记改动,**不得让机器轨 fills/点位在收盘前出现在盘中主图或 15m/1h 上下文图上**。改任何图表代码前,先确认你没有把 `state.machine.fills` 接到盘中主图/上下文图。这是最高优先级,高于本任务任何一个功能点。
> 2. **decision-log.md 追加在文件末尾。** 之前两次改动都错误地插到了文件开头,必须手动修复。这次:新的 `## 2026-07-08 ...` 段落**追加到 decision-log.md 的最后一行之后**,不要插到 `# Decision Log` 标题下方。

## 动手前必读

- `dashboard-dualtrack-v5.html` — 单文件内联 JS,本任务主战场
- `pipelines/dashboard_server.py:766` — 运行状态 `status` 计算(WATCH 根因);`_runtime_check`(~2810)确认子检查是二元 ok/blocked
- `tests/test_dashboard_dualtrack_static.py` — 现有静态契约测试风格(本任务的结构性改动照它加断言)
- `packages/standard-kline/standard-kline.js` — 已在用的图表组件(`StandardKlineChart`/`adaptBarPayload`/`setAdaptedData`/`nearestTime`),主图已用它

## 7 个修复项

### 修复 1 — WATCH 标签(清晰度)
**根因**:`dashboard_server.py:766` `status = "blocked" if any blocked else "warn" if not closed else "ok"`。子检查是纯二元(`_runtime_check` 只发 ok/blocked)。所以顶部 WATCH 唯一含义是"当前周期还没收盘",**不是警告**,也没盖住任何降级子检查。但卡片标题"运行红灯"+黄色 WATCH+全绿子检查,让用户以为出事了。
**改法**:前端 `statusText`(`dashboard-dualtrack-v5.html:517` 附近)与卡片渲染(~264):
- 未收盘且无 blocked 子检查 → 显示"盘中 · 正常"(用 ok/中性色,不用报警黄),而不是 WATCH。
- 有 blocked 子检查 → 显示"需处理 · N 项"(报警色)。
- 已收盘且归因齐 → "已收盘 · 已评分"。
- 卡片标题"运行红灯"改为中性表述如"运行状态 · 样本完整性"(红灯这个词只在真 blocked 时才配得上)。
- **不要**改后端 `status` 语义(`warn/ok/blocked` 保持),只改前端如何把它翻译成人话。

### 修复 2 — 1m/5m 时间框切换(功能缺失)
**根因**:`#mainChart` 上方 `data-tf="1m"/"5m"` 两个按钮**没有任何点击处理**(`setSegment` 只接了 direction/orderSide/orderType/orderNotional,且它找的是 `data-value` 不是 `data-tf`);主图固定拉 `/api/dualtrack/market/bars?limit=96`(默认 1m),没有取 5m 的路径。
**改法**:
- 给 tf-seg 两个按钮加点击处理:切换 `.on` 高亮 + 记录 `state.mainTf`(默认 "1m")。
- 主图数据加载改为按 `state.mainTf` 请求(`/api/dualtrack/market/bars?timeframe=${tf}&limit=96`,该端点已支持 timeframe——上下文图就在用 `timeframe=15m/1h`)。
- 切换后重新走 `renderMainKline()`,标记/计划线随之按新时间框的 candles 重新对齐(`nearestTime` 已有)。
- **盲答约束**:切到 5m 后主图仍然只喂 `state.human.fills`,不许因为换了时间框就把机器 fills 带进来。

### 修复 3 — 15m/1h 上下文图改用真 K 线(功能)
**根因**:15m/1h 走的是自写的简陋 SVG 函数 `drawMini`(~405),不是 StandardKline——数据是真的,渲染是糊的。
**改法**:把 `#ctx15`/`#ctx1h` 改成 StandardKline 实例(复用 `packages/standard-kline`,和主图同一套),高度按现有 150px 容器适配(StandardKline 构造支持 `height`)。上下文图**只渲染 K 线本身,不加任何 fills 标记**(既符合"大级别定方向"的用途,也天然满足盲答约束——上下文图从来不该有点位)。保留现有的"无真实数据"降级态(数据取不到时不要崩)。
- 注意:`#ctx15`/`#ctx1h` 目前是 `<svg>` 元素,StandardKline 需要一个普通容器 `<div>`,改标签类型,别硬塞进 svg。

### 修复 4 — 机器轨图标记密度/可读性(清晰度)
**根因**:收盘后"机器轨 MACHINE"卡片一次画进 18+ 笔 fills,每笔都生成 entry/fill/tp/sl 多条价格线+标签,价格轴右侧标签严重重叠(见用户截图)。
**改法**:构建机器轨 overlay 时对价格线标签做去重/稀释——同一价位(或极接近价位,阈值如 ±0.05%)的重复止损/止盈线合并成一条,标签只标一次;或对 fill 标记按时间聚合,避免同一根 K 线上堆叠多个 arrow。目标:标记稀疏可读,不是每笔都画满。**只改机器轨卡片的 overlay 构建**,不动盘中主图的人轨标记逻辑。给出你选择的稀释策略并在 PR 说明。

### 修复 5 — "周期结束·对账复盘"错行(布局)
**根因**:`.judge .row` 用 `display:flex + margin-left:auto`,中间描述文字长度不一时,标签/描述/数值无法对齐成列,视觉错行。
**改法**:把 `.judge .row` 改成固定三列 grid(标签列 / 描述列 / 右对齐数值列),让每行的三部分纵向对齐成列。纯 CSS 调整,不改数据。

### 修复 6 — `grid traded` / `trend armed` 英文 → 中文(清晰度)
**根因**:机器层的 `layers` 字段原样是 `["grid:traded", "trend:armed"]`,前端直接显示了英文。
**改法**:前端加一个显示映射(如 `grid:traded`→"网格 · 已成交",`trend:armed`→"趋势 · 已激活",`grid:armed`/`trend:frozen` 等同类值一并覆盖),在渲染 layers 的地方套用。**后端 `layers` 原始值不改**(保持数据原样,只改展示)。映射表建议集中定义,漏配的值回退显示原文而不是报错。

### 修复 7 — "查看最近回放"按钮(功能核实)
**现状**:按钮 href 取 `previous.replay_url`(`dashboard-dualtrack-v5.html:289`),该值来自 runtime status 的 `previous_closeout.replay_url`。
**改法**:核实 `previous_closeout.replay_url` 后端确实在生成且指向有效的 `dashboard-dualtrack-replay.html?...&cycle=...`;若为空则按钮应显示禁用态而不是一个死链(`href=""` 点了会刷新当前页——这本身就是个 bug)。修正:replay_url 为空时渲染成灰色不可点的"暂无可回放周期",非空时才是可点链接。同样检查 `closeout.replay_url`(修复文件里第 298 行"查看完整回放"是否有同样的空链接问题)。

## TDD 与验证

单文件 HTML 的既有测试方式是静态契约测试(断言结构/字符串存在)+ 浏览器验证。两者都要:

`tests/test_dashboard_dualtrack_static.py` 追加断言(先红后绿):
1. tf-seg 按钮存在点击处理的证据(如绑定 `data-tf` 的 handler 代码存在);主图请求含 `timeframe=` 参数化
2. `#ctx15`/`#ctx1h` 通过 StandardKline 渲染(断言不再调用 `drawMini`,改为 StandardKline 实例化)
3. WATCH 文案:断言"盘中 · 正常"类文案存在、且"运行红灯"标题不再无条件出现
4. layers 中文映射表存在,断言 `grid:traded` 有对应中文
5. 空 replay_url 渲染成禁用态的分支存在(断言不再产生 `href=""`)
6. **盲答回归断言(关键)**:盘中主图渲染路径只引用 `state.human`,断言 15m/1h 上下文图与盘中主图的渲染代码里**不出现** `state.machine.fills` 之类机器点位喂入

全仓 `python3 -m pytest -q` 全绿。

浏览器验证(PR 里附步骤与结果):打开 `dashboard-dualtrack-v5.html`,console error = 0;1m↔5m 点击能真实切换主图;15m/1h 显示为真 K 线;对账复盘区三列对齐;机器轨标记不再重叠糊成一片;layers 显示中文;无可回放时按钮为禁用态。

## 硬约束(重申)

- **盲答协议**(见顶部约束 1):机器点位绝不泄漏到盘中主图/上下文图。这是验收第一关,不过则整体打回。
- **decision-log 追加到文件末尾**(见顶部约束 2)。
- 不碰订单/执行/broker/风控/双轨评分逻辑;不做 Phase D 视觉系统重设计;不引入前端框架/CDN;R4 锁定文件零 diff。
- 只改 `dashboard-dualtrack-v5.html` + 必要时 `dashboard_server.py` 的 replay_url 生成(若修复 7 需要)+ 对应静态测试;改动面尽量小。
- decision-log 记录:WATCH 语义澄清(为什么是清晰度 bug 不是功能 bug)、上下文图统一到 StandardKline、机器轨标记稀释策略、盲答约束如何在本次图表改动中被守住。

## DOD

- [ ] 6 类静态断言 + 全仓测试全绿
- [ ] 浏览器验证 7 项逐条通过,结果贴进 PR(尤其 1m/5m 真能切、15m/1h 是真 K 线)
- [ ] 盲答回归断言通过:机器点位未泄漏到盘中主图/上下文图
- [ ] decision-log 追加在文件**末尾**
- [ ] PR 列出所有自行决定的点(尤其修复 4 的标记稀释策略)
