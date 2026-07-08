# Codex 任务 09:双轨画布重建(人轨/机器轨零共享 split canvas)+ 14 类 widget 注册表

> 背景:所有者对 `dashboard-dualtrack-v5.html` 的单页共享布局提出了根本性反对——人轨和机器轨共用一张 K 线图会在两边都有持仓时产生视觉混淆,且不符合"两条轨道完全独立"的心智模型。经过两轮 mockup 迭代 + 一轮命名评审(`mockups/dualtrack-split-v1.html` → `v2` → `v3` → **v3.1**),所有者已经**定稿** `mockups/dualtrack-split-v3.html`(文件名不变,内容已是 v3.1):左右两个零共享画布(人轨/机器轨),每个画布内实例化同一套 **14 类 widget**,盘中常驻 12 类 + 盘后复盘专属 1 类(`review`,内部 3 个分节)+ 1 类以页头页签形式存在(`phase`)。widget 命名规则是**一个直白中文词一个 widget**,不用比喻/军事化命名,也不中英混杂(所有者原话:"就一个词儿我就能听懂是什么才行")。
>
> 你(Codex)的任务是把这份定稿 mockup 接上真实后端数据,变成可用页面。**这不是重写 v5**,是并行新建一个页面——v5 目前是每天实盘纸面交易在用的生产页面(launchd 调度、`assets/shell.js` 导航、`tests/test_dashboard_dualtrack_static.py` 近 20 条断言锁死其现有单页视觉契约),贸然原地重写风险太大。规格未覆盖的决策,选最保守做法并在 PR 里列出。

## 实施前置条件

- 开工前先确认上一轮 task-08/task-browser-comment 改动已经提交或另行收口。执行前必须跑:
  - `git diff --exit-code -- dashboard-dualtrack-v5.html tests/test_dashboard_dualtrack_static.py`
  - 若不为 0,**不要**在 task-09 里继续编辑这两个文件;先提交/隔离上一轮改动,否则本任务的 "v5 零 diff" 无法验证。
- 本任务默认按 **09a / 09b 两段执行**:
  - **09a**:只做并行新页面骨架、14 类 widget、token/nav/cache/static tests,不接 order 级新数据。
  - **09b**:做 `apply_unrealized()`、新增只读 trades/config 端点、`risk/data/review/pnl` 的真实数据、浏览器验收。
  - 如果实现者判断可以单 PR 完成,仍必须在 PR/decision-log 里说明没有拆分的理由。

## Widget 注册表(14 类,照此实现,不许改名不许加隐喻词)

| 中文名 | slug | 说明 |
|---|---|---|
| 阶段 | `phase` | 盘前/盘中/盘后复盘切换,画布头部页签,**不是**独立卡片;标 `data-widget="phase"` 方便定位 |
| 方向 | `direction` | 方向判断 |
| 关键位 | `levels` | 关键价格位 |
| 信号 | `signal` | 入场信号 |
| K线图 | `kline` | 主图(70% 宽)+ 图内下单 + 可拖 TP/SL |
| 大级别图 | `context` | 大周期 K 线(30% 宽,竖排两个;**同一 widget 两个实例**,slot=15m/1h 区分,不要拆成 `context15`/`context1h` 两个新 widget) |
| 下单 | `order` | 下单参数面板(自己不是按钮,真正触发键在 K线图 widget 内) |
| 风险 | `risk` | 杠杆/保证金占用/最大可亏/强平距离/SL 有效性 |
| 持仓 | `position` | 当前持仓(方向/数量/均价/TP-SL) |
| 成交 | `fills` | 逐 order 成交表(状态+已实现+未实现) |
| 收益 | `pnl` | 收益汇总(已实现/未实现/今日/一周)——**不叫 `balance`**,balance 像账户余额,这里是 PnL |
| 数据 | `data` | 数据源/新鲜度/K线类型/bar 数量 |
| 状态 | `status` | 运行中健康度,**不含**评分/归因内容 |
| 复盘 | `review` | 盘后专属,**一个 widget、三个内部分节**(归因/判卷/台账),不是三张卡片 |

## 参考文件(先读,不要凭空实现)

- `mockups/dualtrack-split-v3.html` — 定稿骨架(v3.1),HTML/CSS 结构、相位切换 JS、TP/SL 拖拽逻辑(`makeDraggable`)是视觉和交互参考;迁移时只换掉假数据和内联色板。
- `dashboard-dualtrack-v5.html` — 真实端点调用方式(`api()` 封装、`state` 结构、`loadAll()` 时序)、盲答协议现有实现(机器轨遮罩/成交折叠)、EMA/MACD 指标接入(task-08 产物)、context 时间框选择器(task-08 产物)、**市场数据 provider/source_mode/fresh 字段的现有消费方式**(`sourceModeLabel()`/`isSyntheticMarket()` 等函数,目前塞在图表 header 的 title 属性里)、**`state.invalidation` 结构**(人轨 SL/失效价来源)。这些都要**迁移复用**,不要重新发明。
- `packages/standard-kline/standard-kline.js` — 新页面真实 K 线必须继续走 `standard-kline`/Lightweight Charts 路线,保留 task-08 的时间轴、hover 时间、EMA/MACD、sourceFormatter、context timeframe 控件;mockup 的 SVG 只能作为布局/拖拽 overlay 参考,不能回退成假 SVG 主图。
- `services/dualtrack_scoring.py` — `_trades_from_fills()`(已经是逐 order 聚合:entry/exit/status/realized_pnl)、`_attribution()`(payload 里吐出 `trades:{machine,human}`/`tracks`/`ledger`)、`_grade_plans()`(**目前只做 direction-only 评分**,human/ai 分开存放于 `plan_grades`)。
- `services/dualtrack_machine.py` — 机器轨 `sl` 字段的计算方式(约 line 402-451,基于 `plan.invalidation`)。
- `services/dualtrack_config.py` / `configs/dualtrack.yaml` — `max_leverage`(杠杆是配置常量,不是计算值)。
- `pipelines/dashboard_server.py` — 现有 `/api/dualtrack/*` 路由注册方式、`_should_disable_static_cache()` 静态文件白名单、`build_dualtrack_market_bars_response()`(market/bars 响应已经带 `provider`/`source_mode`/`fresh` 字段)。
- `assets/shell.js` / `assets/tokens.css` — 全局导航与设计 token 单一真相源。

## 四条不可违反的硬约束

1. **盲答协议**:机器轨在 `mid`(盘中)相位下,任何界面(主图/成交表/新增的 trades 端点)都不得泄露网格点位、逐档明细或未收盘的 order 级数据;只能给聚合数字(笔数、汇总 PnL)。收盘(`post`)后才逐单揭示。判定"是否已收盘"复用 v5 现有的 `cycleClosed(state.cycle)` 口径,不要自造第二套。
2. **v5 零 diff**:不许修改 `dashboard-dualtrack-v5.html` 本身,也不许修改 `tests/test_dashboard_dualtrack_static.py` 里任何一条现有断言——这是全新并行页面。新增测试放新文件。
3. **decision-log.md 追加末尾** + **R4 锁定文件**(`pipelines/lab_run.py`/`services/lab_registry.py`/`services/lab_r4_promotion.py`)零 diff。
4. **design-token 纪律**:新页面样式必须引用 `assets/tokens.css` 的既有变量,不得像 mockup 那样在页面内重新定义一套颜色字面量。`tests/test_design_tokens_static.py` 的白名单测试必须保持全绿;如确实缺一个必要的表达,走 PR 说明 + 保守新增,不许绕过测试。

## Part A — 新页面骨架:`dashboard-dualtrack-split.html`

1. 按 `mockups/dualtrack-split-v3.html`(v3.1)的结构原样迁移:双画布(`.canvas.human` / `.canvas.machine`)、每画布顶部相位页签(`data-widget="phase"`)、12 个常驻 widget + 1 个 `ph-post` 专属 widget(`review`,内部 3 分节)。widget 外壳(`.w`/`.wh`/`.nm`/`.slug`)、命名与上面 14 类注册表原样保留,不许改名不许加隐喻词,不许中英混杂(如"Context图"这种)。
2. 接入真实端点。现有读端点要复用,新增读端点只在 09b 做:
   - `GET /api/dualtrack/cycle/current` — 驱动 cycle 窗口、相位判断、倒计时、收盘判断基准。注意:该端点**不**包含完整作战单内容。
   - `GET /api/dualtrack/plan/<cycle_id>` — 驱动人轨/AI 作战单的方向、关键位、信号、`invalidation`。
   - `GET /api/dualtrack/machine/<cycle_id>` — 驱动机器轨方向、网格参数、`sl`、盲态聚合。
   - `GET /api/dualtrack/human/<cycle_id>` — 驱动人轨持仓/成交基础状态。
   - `GET /api/dualtrack/ledger` / `GET /api/dualtrack/runtime/status` — 驱动收益、状态、runtime 健康度。
   - `GET /api/dualtrack/market/bars?timeframe=...&limit=...` — 主图 + 两个大级别图(15m/30m/1h/4h 可选,localStorage 持久化,复用 task-08 的 tf-seg 实现);响应里已有的 `provider`/`source_mode`/`fresh`/`bars.length` 直接喂给**数据(data)** widget,不需要新字段。
   - **新增只读** `GET /api/dualtrack/trades/<cycle_id>?track=human|machine` — 09b 新增。返回当前 cycle 的 order 级 trades + `unrealized_pnl`。cycle 未收盘时,`track=machine` 必须拒绝(403)或只返回聚合,不得返回逐单明细;`track=human` 可返回逐单明细。
   - **新增只读** `GET /api/dualtrack/config` — 09b 新增。只暴露展示所需的配置快照,至少包含 `max_leverage`;不得包含 secrets/broker credentials。
   - `POST /api/dualtrack/plan` — 锁定作战单(盘前相位的"锁定"按钮)。
   - `POST /api/dualtrack/orders` — 图内下单按钮 + TP/SL 拖拽确认后提交。
   - `POST /api/dualtrack/verdict` — 盘后复盘阶段的裁决归档(mockup 没设计这块 UI,保留 v5 现状的输入框即可,你保守处理)。
3. K线图 widget:真实蜡烛图用 `standard-kline` 渲染,不得回退成 mockup 的假 SVG。图内左上角"做多/做空"按钮相邻放置;TP=前高/SL=前低建议 + 可拖拽虚线覆盖作为 `standard-kline` 上层 overlay 实现,交互逻辑参考 mockup 的 `makeDraggable`。
4. 机器轨盘中盲态:主图遮罩 + 成交折叠为笔数,迁移 v5 现有做法,不要重新发明。
5. `assets/shell.js` 加一个新导航入口指向这个新页面(不要求替换现有"作战台"指向 v5 的入口——并行共存,cutover 是后续人工决定)。
6. `pipelines/dashboard_server.py` 的 `_should_disable_static_cache()` 加入 `/dashboard-dualtrack-split.html`。

## Part B — 成交(fills)按 order 展示 + 未实现 PnL

**产品要求**(所有者原话):"fillbook 那里展示持仓中或者已平仓,pnl 以及 unrealized pnl,每一行是一个 order"。

**先澄清一个真实存在的数据缺口**:`_trades_from_fills()` 产出的 trade 行有 `status: open|closed`,但**完全没有 `unrealized_pnl` 字段**,`close_cycle()` 也从不 flatten 未平仓的仓位——如果一笔 trade 在周期收盘时仍是 `status=="open"`,现有 `_attribution()` 对它的未实现 PnL 是**没有算过的空白**。下面几处要用**同一个**新纯函数,只是 mark price 来源不同,不要写三套逻辑:

1. 在 `services/dualtrack_scoring.py` 新增纯函数 `apply_unrealized(trades, mark_price, *, mark_fresh=True)`:对 `status=="open"` 的 trade 补 `unrealized_pnl = (mark_price - entry_price) * remaining_units * (1 if side=="long" else -1)`;`status=="closed"` 的 trade 不动、不覆盖已有 `realized_pnl`。**不要改 `_trades_from_fills` 本体**,新函数单独写单测。
2. **fail-closed**(硬约束):mark price 缺失、陈旧、解析失败/非有限数时,`unrealized_pnl` 必须是 `null`,前端渲染 `--`,**不许默认 0、不许沿用旧值**。陈旧判定由调用方复用项目已有 staleness 口径(例如 `system_state.py`/`command_center.py` 的 freshness 窗口概念),并以 `mark_fresh=False` 传给 `apply_unrealized()`;函数本身不得猜测当前时间。本项目历史上已经因为"陈旧数据当健康"的 fail-open 语义出过至少两次生产事故(裸头寸、日亏损护栏),这里不能重蹈。
3. **盘后(post)**:在 `close_cycle()` 组装 `attribution` 之前,对 `machine_trades`/`human_trades` 跑一遍 `apply_unrealized(trades, cycle["close_price"], mark_fresh=True)`(mark price = 该周期最后一根 bar 的收盘价,已经在 `cycle` dict 里);`close_price` 非有限数时按上条 fail-closed 处理。这一步**要改 `services/dualtrack_scoring.py`**。前端渲染:状态(持仓中/已平仓,圆点色区分)| 方向 | 数量 | 开仓 | 平仓 | 已实现 | 未实现,**已平仓行"未实现"列必须是 `--`,持仓中行"已实现"列必须是 `--`**,不许两列同时填数字。
4. **盘中(mid)· 人轨**:人轨从不盲测,盘中就要能看到自己的 order 级明细 + 实时未实现 PnL。新增只读 `GET /api/dualtrack/trades/<cycle_id>?track=human`,对当前(未收盘)cycle 的人轨 fills 跑 `_trades_from_fills()` 再套 `apply_unrealized(trades, mark_price, mark_fresh=...)`,`mark_price` 取当前 cycle 最新 bar 的收盘价。**机器轨调用同一端点在 cycle 未收盘时必须拒绝(403)或只返回聚合,不得返回逐单明细**——判定口径同硬约束 1。
5. **盘中(mid)· 机器轨**:维持现状,只显示"共 N 格盲测中"的聚合行,不出 order 表格。

### Part B.4 — 收益(`pnl`)widget 取数口径

四格必须各自有明确数据来源,不许留 mock 或只填一半(注意 widget 中文名叫"收益",slug 是 `pnl`,**不是** `balance`):

- **已实现**:mid 用 `_pnl(fills)` 对当前 cycle 该轨 fills 求和;post 用 `cycle["machine_realized_pnl"]` / `cycle["human_realized_pnl"]`。
- **未实现**:当前周期该轨所有 `status=="open"` trade 的 `unrealized_pnl` 之和(用 Part B 新增的 `apply_unrealized` 结果求和);没有开仓 trade 时为 `0`(不是 fail-closed 场景)。
- **今日**:`ledger.daily.tracks.{track}.realized_pnl` 加上当前进行中周期的(已实现+未实现)——mark-to-market 口径,不是纯 realized。
- **一周**:同样逻辑,基数换成 `ledger.weekly.tracks.{track}.realized_pnl`。
- 这四个数字 mid/post 两个相位都要显示。

## Part C — 复盘(`review`)widget:归因 + 判卷 + 台账三个分节

**这是一个 widget,不是三个**——`mockups/dualtrack-split-v3.html` 用 `.rv-sec` 把三段内容放进同一张卡片,迁移时保持这个结构(一个 `.w.ph-post` 外壳,内部三个分节,每段一个 `.rv-h` 小标题)。各轨**自评,不做跨轨对照**——v5 原有的判卷卡是人机对照卡,新画布零共享架构下已经不适用,不要照搬那种对照形态;所有者已明确确认不做画布外的头对头计分条。

1. **归因分节**:消费 `attribution payload.tracks.{human|machine}`,展示笔数/胜率/已实现/捕获比/纪律违规。零新增后端。
2. **判卷分节**:**只能展示后端真实存在的评分维度**。目前 `_grade_plans()` 只做 direction-only 评分(`plan_grades.human` / `plan_grades.ai`,含 `graded`/`hit`/`eligible`)。"方向"行给真实 ✓/✗;"关键位"、"信号"两行**必须显式渲染"未建立评分口径"**(灰态文字,不给 ✓/✗ 图标,参考 mockup 的 `.gpending` 样式)——**这是硬约束,不是可选项**:编造一个看起来像真评分的通过/未通过状态,比不做更糟,这是所有者反复强调的"实事求是"红线。footer 的"回放"链接指向 `dashboard-dualtrack-replay.html?layout=dualtrack&cycle={cycle_id}`(复用 v5 现有 `replayLink()` 空 URL 禁用逻辑)。
3. **台账分节**:消费 `attribution payload.ledger.daily` / `.weekly`,展示最近几个周期结果 + 已实现。零新增后端。

## Part D — 风险(`risk`)widget 取数口径

全部是**纯展示计算,不影响风控/下单主链路**。除 `GET /api/dualtrack/config` 暴露配置快照外,不新增 risk 专用后端:

- **杠杆**:前端读新增只读 `GET /api/dualtrack/config` 返回的 `max_leverage`(后端来源是 `configs/dualtrack.yaml` / `services/dualtrack_config.py`;配置常量,不是计算值)。不许在 HTML/JS 里硬编码 `10x`。
- **保证金占用** = `qty * entry_price / leverage`(用 position widget 已有的 qty/entry_price)。
- **最大可亏 / 距失效价**(必须 direction-aware,不许直接 `abs()` 糊过去):`stop_price` 来源同前——人轨用 `state.invalidation`,机器轨用 `services/dualtrack_machine.py` 里已经算好的 `sl` 字段(约 line 451),都是复用现有字段。判断 `stop_price` 相对 `entry_price` 和 `side` 是不是在"亏损那一侧"(long 且 `stop_price < entry_price`,或 short 且 `stop_price > entry_price`):
  - **是**(真的是保护性止损):label 用"最大可亏(距SL)",值 = `(entry_price - stop_price) * qty`(long)或 `(stop_price - entry_price) * qty`(short),渲染成负数/亏损色。
  - **不是**(失效价在盈利那一侧,常见于网格策略的失效价不等于止损):label 改用中性的"距失效价(价格)",值就是距离的绝对量,**不要**套红色/不要暗示这是亏损。mockup 里机器轨的"距失效价(4,155)$88.44"就是这个情况——4155 在多头入场价上方,不是保护性止损,不能标成"最大可亏"。
  这条是硬约束:错误地把一个中性距离标成"最大可亏"会让用户误判下行风险,这和本规格其它地方的 fail-closed 原则(未实现 PnL 缺失显示 `--`、判卷不编造维度)是同一类问题——展示一个看似真实但方向错误的风险数字,比不显示更危险。
- **强平距离** = `entry_price * (1 - 1/leverage)`(long)或 `entry_price * (1 + 1/leverage)`(short)。**这是一个简化估算公式**(不含维持保证金、手续费、资金费率等真实交易所强平逻辑,本项目双轨是纸面模拟,没有对接真实强平引擎)。**硬约束**:UI 上必须标注"简化估算"或等价字样,不许让用户误以为这是交易所真实强平价——这条不是可选的措辞建议,是防止误导用户做真实交易决策的红线。
- **SL 状态**:`stop_price` 非空/非零 → "有效";否则 → "未设置"。纯前端判断已加载数据是否存在,不新增后端。

## Part E — 数据(`data`)widget 取数口径

零新增后端。`/api/dualtrack/market/bars` 的响应里已经有 `provider`/`source_mode`/`fresh`/`bars`(`bars.length` 即数量),v5 现有的 `sourceModeLabel()`/`isSyntheticMarket()` 等函数已经在消费这些字段,只是目前渲染在图表 header 的 title 属性里(不显眼)。这个 widget 只是把同样的字段挪到一个专门的、常驻可见的地方,**不要臆造字段名**,以真实响应结构为准;如果字段名和上面描述的不完全一致,以你读到的真实响应为准。展示:数据源(provider)、K线类型(source_mode 的人话翻译,复用 `sourceModeLabel()` 逻辑)、新鲜度(fresh + 距今秒数)、bar 数量(主图/大级别图分别计数)。

## 测试(先红后绿)

新文件 `tests/test_dashboard_dualtrack_split_static.py`,覆盖:
1. 双画布 + 相位页签(`data-widget="phase"`)+ 12 个常驻 widget + 1 个 `review`(内含 3 个 `.rv-sec` 分节)全部存在,命名与 slug 与 14 类注册表一致(尤其 `pnl` 不是 `balance`,`context` 中文显示"大级别图"不是"Context图")。
2. 盲答回归:机器轨 mid 相位下,页面源码渲染路径里不出现机器轨逐单 trades/网格点位。
3. 成交表 open/closed 互斥填充断言(已实现与未实现不同时非空)。
4. `review` widget 判卷分节:检查"关键位"/"信号"渲染的是"未建立评分口径"文案而不是 ✓/✗ 图标(只有"方向"行允许出现 ✓/✗)。
5. `risk` widget 强平距离文案含"简化估算"或等价字样。
6. `risk` widget 的"最大可亏/距失效价"分支覆盖两种场景(失效价在亏损侧 → 标"最大可亏"+亏损色;失效价在盈利侧 → 标"距失效价"+中性色),不许用无条件 `abs()` 把两种场景渲染成同一个正数。
7. `v5` 现有测试文件字节级零 diff。
8. design-token 白名单测试保持绿。

后端新增 `apply_unrealized()` 的独立单测(放 `tests/test_dualtrack_scoring.py` 或新文件):
- 正常计算(long/short 两个方向都测)。
- mark price 为 `None`/非有限数,或 `mark_fresh=False` 时返回 `None`,不是 `0`。
- `status=="closed"` 的 trade 不被覆盖已有 `realized_pnl`。

全仓 `python3 -m pytest -q` 全绿;R4 锁零 diff;`node --test packages/standard-kline/` 保持绿。

## 验收(设计师级 + 事实核查级)

浏览器用真实数据逐区截图:
- 新页面与定稿 mockup(v3.1)的忠实度对比(14 类 widget 名称、布局、相位切换)。
- 相位页签真的驱动 `state`/重新渲染,不是纯 CSS 切换假状态。
- 机器轨盘中:读 DOM/network response 确认逐单数据确实没有被发送到前端(不只是视觉遮罩挡住)。
- 找一个当前有真实持仓的场景,验证人轨盘中未实现 PnL 数字合理(与 position widget 的持仓量/均价手算对上)。
- 人为制造 mark price 陈旧/缺失场景,确认前端显示 `--` 而不是 `0` 或旧值。
- `risk` widget 的强平距离标注清晰可见,不会被误认成真实强平价。
- `risk` widget 的"最大可亏/距失效价"跟当前真实持仓方向核对:如果失效价其实在盈利那一侧,必须显示中性的"距失效价"而不是红色"最大可亏"——用一个当前失效价在盈利侧的真实场景截图验证,不能只测试保护性止损的那一种情况。
- `review` widget 的判卷分节:关键位/信号确实是灰态"未建立评分口径",不是伪装的 ✓/✗。
- 五页(新页面 + 现有四个)console error = 0。

## 硬约束重申

- 不碰 broker/风控/执行主链路;新增的 `unrealized_pnl`、`risk` widget 的强平估算都是**只读展示计算**,不得反向影响下单/风控判断。
- v5 文件与其测试文件零 diff。
- R4 三个锁定文件零 diff。
- decision-log 记录:新页面的端点复用清单、新增只读端点的路径与 fail-closed 语义、判卷 widget 的"只展示真实评分维度"决策、强平距离公式的简化声明、v5/新页面并行策略与 cutover 未决事项。
- 默认拆成 09a(Part A 骨架)/09b(Part B/C/D/E 数据与 widget)。两个 PR 都要在本文件规格下完成、各自过全仓测试、各自追加 decision-log。若选择不拆,PR/decision-log 必须说明原因。

## DOD

- [ ] Part A/B/C/D/E 全部实现或按上条拆分说明完成
- [ ] 新增测试 + 全仓测试 + R4 + token 白名单全绿
- [ ] 盲答协议回归:视觉 + DOM/network 双重验证
- [ ] `review` widget 判卷分节无编造评分
- [ ] `risk` widget 强平距离有"简化估算"标注
- [ ] v5 文件/测试零 diff
- [ ] PR 列出所有自行决定的点(尤其 mark price 新鲜度阈值取值、若未拆 09a/09b 的原因)
