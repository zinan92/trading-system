# Codex 任务 10b:双画布真实交易链路(下单 payload/价格接入/TP-SL 拖拽/错误反馈)

> 背景:审计发现 `dashboard-dualtrack-split.html` 的三个 POST 全是 09a 占位 stub——`lockPlan()`/`submitOrder()`/verdict 只发 `{source:"split_canvas_09a"}`(line 588-594),不带任何业务字段;后端 `validate_plan`/`submit_order` 会正确 400 拒收(已验证,不会损坏数据),但前端 `.catch(() => null)` 把错误静默吞掉——用户点"锁定作战单"或"确认"下单,界面毫无反应。**页面长得像交易台但不能交易**,这是当前与 production-ready 之间最大的一块缺口。
>
> 本任务把交易链路做真。前置:task-10a(布局/文案/相位 gating)应已合入;若未合入,本任务不得顺手做 10a 的内容,冲突处以 10a 为准。

## 先读的事实(已核实,不要重新发明)

- **order payload 契约**(v5 line 488,后端 `services/dualtrack_human.py` `submit_order()` line 29-81 消费):
  `{cycle_id, ts, side: "buy"|"sell", order_type: "market"|"limit", price, notional, sl, tp}`
  后端校验 side/order_type/price,`sl`/`tp` 走 `_optional_float` **仅记录在 fill 上**。
- **关键事实:人轨没有自动止盈止损执行器**。`services/dualtrack_human.py` 全文没有任何 protective/watcher/auto-exit 机制;`sl`/`tp` 记录后,价格到达时**不会**自动平仓,平仓靠人工再提交一笔 exit 方向的 order。这决定了本任务 UI 的诚实性要求(见 B3)。
- **plan payload 契约**(v5 line 481):
  `{cycle_id, direction, range:{low,high}, key_levels:[...], invalidation:[{side,price,confirm}], confidence, source}`
  后端 `save_human_plan`:已锁定的 plan 不可变(`locked_mutation_rejected`),`validate_plan` 校验字段。
- **价格来源**:`/api/dualtrack/market/bars` 已在页面轮询,最新 bar 的 close 即最新价;响应带 `fresh` 标志。

## 硬约束

1. **盲答协议**:所有改动只涉及人轨交互;机器轨保持只读,不新增任何干预面。
2. **fail-closed 下单前置**:行情 `fresh === false` 或最新价缺失时,做多/做空按钮 disabled,按钮价格位显示 `--`,confirmbar 提示"行情不新鲜 · 禁止下单"。**陈旧价格下允许下单是本项目最不可接受的一类 fail-open**。
3. **错误必须可见**:所有 POST 的失败(网络/400/超时)必须在界面上显示后端返回的真实错误信息(参考 v5 的 `orderStatus` 做法),**全文禁止 `.catch(() => null)` 模式**,静态测试锁死。
4. **确认制**:下单必须两步(点做多/做空 → confirmbar 显示完整参数 → 点确认才 POST);confirmbar 默认隐藏,只在选边后出现;pending 期间确认按钮 disabled 防重复提交。
5. **相位 gating**(10a 已建):动作只在真实 `mid` 相位可用(锁定作战单只在真实 `pre`);本任务接真实行为时不得绕过该 gating。
6. v5 零 diff、R4 零 diff、decision-log 追加末尾、token 白名单全绿——同前。

## B1 · 锁定作战单:真实表单 + 真实 payload

现状:split 页盘前只有只读展示 + 一个发空 POST 的按钮,**没有输入界面**——用户在 split 页上根本没法填作战单(v5 有完整表单)。

1. 在人轨盘前相位加最小表单(方向 long/short/flat、range low/high、关键位、失效条件行 `{side,price,confirm}` 可增删、信心 1-10),字段与 v5 的 payload 逐一对应,`source: "split_canvas"`。UI 布局融入现有 方向/关键位/信号 三个 widget 的可编辑态(mockup 盘前相位"可编辑"标记就是这个意思),不要另起一块大表单区。
2. 已存在 locked plan 时:表单只读 + 按钮显示"已锁定"disabled;后端本来就会拒绝,前端不给用户一个注定失败的按钮。
3. 锁定成功后刷新并显示锁定状态;失败显示后端错误原文。

## B2 · 下单链路:真实 payload + 价格接入

1. **按钮价格**:`data-last-price` 用最新 bar close 填充,轮询同步更新;`fresh === false` 时按约束 2 处理。买卖双按钮显示同一个最新价即可——1m bar 数据没有 bid/ask,**不要编造价差**。
2. **选边 → confirmbar**:点做多/做空后 confirmbar 出现,内容:方向、金额(来自下单 widget 选中的 pill)、限价(默认=最新价,可改)、SL、TP(来源见 B3)。点确认 → POST 完整 payload(契约见上)→ 成功后显示"paper fill recorded"并刷新 fills/持仓/收益;失败显示错误原文。
3. **金额 pill 联动**:下单 widget 选中的金额就是 payload 的 `notional`;TP/SL 预设选"手动"时 confirmbar 的 SL/TP 可直接输入。

## B3 · TP/SL:前高/前低建议 + 可拖拽线 + 诚实标注

1. **建议值纯函数**:`suggestTpSl(bars, side, n=30)` —— TP=最近 n 根 bar 的最高高点(做多;做空取最低低点),SL 反向。纯函数放页面 JS,常量 n 可配,配一条静态测试断言函数存在。
2. **可拖拽线**:在主图上以 overlay 实现 TP(绿)/SL(红)两条虚线,拖动改价、confirmbar 同步。实现路线:优先用 standard-kline 暴露坐标换算(如无现成 API,允许包加最小只读 API,例如 `priceToY/yToPrice` 或带拖拽回调的 price-line 选项;provider-agnostic 命名,版本递增,包内测试)。**不许**为了拖拽把主图退回假 SVG。
3. **诚实标注(硬约束)**:因为后端没有自动执行器,TP/SL 行与 confirmbar 必须明示**"仅记录 · 不自动执行"**(措辞可调,语义必须无歧义)。不做这个标注 = 用户以为挂了保护单实际裸奔,这是比没有 TP/SL 功能更危险的状态。把"人轨保护单自动执行器"列入 PR 未决事项,不在本任务做。
4. TP/SL 建议只算自 bars(人轨自己的数据),不引用任何机器轨数据——盲答协议常规检查。

## B4 · 裁决归档

盘后相位(真实 post)加一行裁决输入框 + 归档按钮,POST `{cycle_id, note}`(v5 现有契约),成功/失败反馈同约束 3。放在复盘 widget 的台账分节下方。

## 测试(先红后绿)

静态(追加 `tests/test_dashboard_dualtrack_split_static.py`):
1. 全文不含 `.catch(() => null)`(允许 `.catch` 后接真实错误处理)。
2. order POST 构造包含 `cycle_id/side/order_type/price/notional/sl/tp` 字段引用。
3. plan POST 构造包含 `direction/range/key_levels/invalidation/confidence` 字段引用,`source` 为 `split_canvas`。
4. `suggestTpSl` 存在;TP/SL 渲染路径含"不自动执行"标注文案。
5. confirmbar 默认隐藏(初始态断言)。
6. 盲答回归:上述改动后,机器轨渲染路径仍不含逐单/网格点位(09 现有断言继续绿)。

包(如加了坐标 API):`node --test packages/standard-kline/` 覆盖新 API;未传时行为与前版一致。

后端:**本任务预期零后端改动**(契约全部已存在)。若实现中发现确需后端小改,停下来在 PR 里说明理由再动手,禁止顺手改。

全仓 `python3 -m pytest -q` 全绿。

## 验收(浏览器,真实数据 + 破坏性场景)

- 正向链路:盘前锁定一份完整作战单成功;盘中选边 → confirmbar 参数完整 → 确认 → fills 表出现该笔、持仓/收益联动刷新;金额 pill 换档后 payload 的 notional 跟着变(network 面板核对)。
- **破坏性场景 1**:停掉 dashboard server(或 mock 断网),点确认 → 界面显示可读错误,不是无反应。
- **破坏性场景 2**:构造 `fresh:false` 行情(mock 数据层),做多/做空按钮 disabled、价格 `--`、confirmbar 拒绝下单。
- **破坏性场景 3**:对已锁定周期再进盘前页签,表单只读、按钮"已锁定"disabled。
- TP/SL 拖线:拖动后 confirmbar 数字实时同步;"仅记录 · 不自动执行"标注在截图中清晰可见。
- 双击/连点确认只产生一笔 fill(pending disabled 生效,fills 表核对)。
- console error = 0;v5/R4 零 diff。

## DOD

- [ ] B1-B4 完成;三个 POST 无一发送占位 payload、无一静默吞错
- [ ] fail-closed:陈旧行情禁止下单,实测截图
- [ ] TP/SL"仅记录 · 不自动执行"标注在位
- [ ] 破坏性场景 3 项全部实测附 PR
- [ ] 新增测试 + 全仓 + 包测试全绿;v5/R4 零 diff;decision-log 追加末尾
- [ ] 未决事项列出(至少:人轨保护单自动执行器、bid/ask 真实价差数据源)
