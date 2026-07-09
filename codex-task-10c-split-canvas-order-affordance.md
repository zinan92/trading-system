# Codex 任务 10c:下单确认键 CSS 修复 + 禁用态一致性(纯前端,小改动)

> 背景:task-10a/10b 交付后,所有者要求逐项独立复核(不采信报告,自己点、拖、造异常场景)。整体结论是核心交易链路是真实且 fail-closed 的(下单 payload 真实、TP/SL 拖拽真实生效、陈旧行情确实拦截了提交),但复核中发现三个报告没提到的问题,全部是纯前端、零后端、零测试契约变更。本任务修这三个,预计改动量很小。
>
> 每一条都已经在浏览器里用 DOM 实测定位到根因和具体选择器,不是审美意见,照此实施即可。

## 硬约束(与 10a/10b 相同,重申)

1. v5 零 diff、R4 三文件零 diff、decision-log.md 追加末尾、design-token 白名单全绿。
2. **零后端改动**;不改任何 API 契约、不改测试对 payload 字段的既有断言(只新增断言)。
3. 不改盲答协议相关代码路径。

## C1 · 确认下单按钮被压成 11px 宽(P1,最优先)

**根因已定位**:[dashboard-dualtrack-split.html:92](dashboard-dualtrack-split.html:92)
```css
.confirmbar{...display:flex;gap:var(--sp-2)...}
.confirmbar .go{margin-left:auto;color:var(--run);border-color:var(--run)}
```
`.confirmbar` 是一行 flex,子元素包括:方向/金额/价格文字、限价/SL/TP 三个 `width:88px` 的 input(line 94)、一条 `.tpsl-note` 免责声明文字、以及确认按钮 `.go`。**`.go` 没有 `flex-shrink:0`**,内容超宽时和其它子元素一起被压缩,但按钮的 min-content 比文字 span 更容易被压穿,实测被压到 `width:11px`,"确认"两字换行挤成一团,看起来像渲染故障。

**修法**(两步,都要做,不是二选一):
1. CSS 修复:`.confirmbar .go{flex-shrink:0;white-space:nowrap}`。这是最小修复,先让按钮不再被压碎。
2. 结构修复:`.confirmbar` 当前一行塞了太多东西(3 个输入框 + 免责声明 + 按钮),即使按钮不被压碎,窄画布下这一行本身还是会拥挤。改成两行:第一行价格参数(方向/金额/限价/SL/TP 输入),第二行左侧免责声明 + 右侧确认/取消按钮(参考 mockup 里 confirmbar 的原始两行意图)。`.go` 和取消按钮固定在第二行右侧,不参与和输入框的空间竞争。
3. **必须核对 [dashboard-dualtrack-split.html:148](dashboard-dualtrack-split.html:148) 现有的 `@media(max-width:760px)` 规则**:这条规则已经把 `.confirmbar` 在窄屏下改成 `flex-direction:column;align-items:stretch`,`.go` 的 `margin-left` 也在这个断点里被清零。新拆出来的两行结构(桌面态)必须和这条移动端规则相容,不能互相打架(比如两行结构叠加移动端的 column 规则后变成四行、或 `.go` 定位规则冲突)。如果拆两行后移动端规则需要联动调整,一并改;**不许对移动端断点保持沉默**——本任务验收只测 1280/1440/1600(见下),如果确认移动端不在本轮验收范围内,要在 PR 里显式写清楚"移动端未测,已保留现有 column 规则但未验证协同效果",不能什么都不提。

**验收标准(可测)**:1280/1440/1600 三档宽度下,`.confirmbar .go` 的 `getBoundingClientRect().width` 必须 ≥ 44px(可点击的最小触控目标经验值),且按钮文字 `scrollWidth <= clientWidth`(不换行截断)。**另外用 ≤760px(如 375/414)截一张图**,确认两行结构没有和现有的 column 断点规则叠加出更糟的布局(哪怕只是"没变得更差"的最低要求,不要求移动端本轮做到完美)。

## C2 · 方向按钮(做多/做空/观望)锁定态无视觉禁用(P2)

**证据**:作战单锁定后,同一张表单里"信心"、"区间低/高"、"关键位"、"失效条件"全部正确显示为灰态 `disabled`(有 `.btn[disabled]` 之类的样式覆盖),唯独方向分段按钮组([dashboard-dualtrack-split.html:178](dashboard-dualtrack-split.html:178) `data-plan-direction` 三个 button)没有 `disabled` 属性。点击处理器里([dashboard-dualtrack-split.html:1142](dashboard-dualtrack-split.html:1142))确实有 `humanPlanLocked()` 守卫,点了没有副作用,**但用户看不出来按钮已经失效**,点了没反应会以为是 bug。

**修法**:
1. 在渲染/同步逻辑里(`syncPlanForm()` 或锁定态刷新的那个函数),给 `[data-plan-direction]` 三个按钮同步设置 `disabled = humanPlanLocked()`,和其它 `[data-plan-control]` 字段的禁用判定走同一个锁定态来源,不要新造第二套判断。
2. CSS 补一条:目前只有 `.btn[disabled],.inbtn[disabled]` 有禁用样式,`.seg button` 分段按钮组没有对应规则。新增 `.seg button[disabled]{opacity:.45;cursor:not-allowed}`,让方向按钮锁定后视觉上和其它字段一致地变灰。

**验收标准**:作战单锁定后,`document.querySelectorAll('[data-plan-direction]')` 每个按钮 `.disabled === true`,且 computed `opacity` 明显低于未锁定态(用 `preview_inspect` 或等价方式核对,不要求具体数值,要求"看得出来是灰的")。

## C3 · 陈旧行情拦截下单时,做多/做空按钮本体无视觉反馈(P2)

**证据**:`fresh:false` 时点击图内做多/做空按钮,提交链路确实被 `orderBlockReason()` 正确拦截(这条已验证是真实生效的 fail-closed,不是本任务要修的安全问题)——但按钮本身([dashboard-dualtrack-split.html:150](dashboard-dualtrack-split.html:150) `.inbtn`)在此状态下依然是全不透明度、`cursor:pointer` 的正常可点样式,拦截提示只出现在下方"下单"widget 里的一行小字状态文本。用户视线还停留在图表按钮上时,点击没有任何即时反应,不知道发生了什么。

**修法**:
1. `orderBlockReason()`(已存在,[dashboard-dualtrack-split.html:804](dashboard-dualtrack-split.html:804))已经是判断"能不能下单"的唯一真相源,直接复用它,不要新写一套判断。在每次渲染/轮询刷新时,把 `.inbtn.buy`/`.inbtn.sell` 的 `disabled` 属性绑定到 `Boolean(orderBlockReason())`。
2. 好消息是 `.inbtn[disabled]` 的禁用样式已经存在([dashboard-dualtrack-split.html:61](dashboard-dualtrack-split.html:61) `.btn[disabled],.inbtn[disabled]{opacity:.45;cursor:not-allowed;...}`),**不需要新增 CSS**,只需要在 JS 里把 `disabled` 属性真正设置上去。
3. 按钮价格位保持现状(`fresh:false` 时显示 `--`),这条不用改。

**验收标准**:强制 `state.market.human.fresh = false` 后重新渲染(和上一轮复核用的方法一致),`.inbtn.buy`/`.inbtn.sell` 的 `disabled === true` 且 `getComputedStyle(...).opacity` 明显下降;恢复 `fresh:true` 后按钮恢复可点状态。同样,`state.realPhase !== "mid"` 或非当前查看阶段时(`orderBlockReason()` 的另外两个分支)按钮也应该同步禁用——**这两个分支目前可能也没有驱动 `.inbtn` 的 disabled 属性,一并核实并修好,不要只修 stale 这一个分支漏了另外两个**。

## 测试(先红后绿)

追加到 `tests/test_dashboard_dualtrack_split_static.py`(不改现有断言):
1. `.confirmbar .go` 的 CSS 规则含 `flex-shrink:0`(静态断言页面源码里存在该声明,或断言 confirmbar 拆分成两行结构存在)。
2. `.seg button[disabled]` 的禁用样式规则存在。
3. 渲染方向按钮禁用逻辑的函数/代码路径存在对 `[data-plan-direction]` 设置 `disabled` 的引用(grep 断言即可,不需要跑无头浏览器)。
4. `orderBlockReason()` 的返回值被用于设置 `.inbtn` 的 `disabled` 属性(grep 断言调用点存在)。

全仓 `python3 -m pytest -q` 全绿;不需要新增/修改包内测试(本任务不碰 standard-kline)。

## 验收(浏览器,真实数据,复用上一轮复核用的手法)

- C1:1280/1440/1600 三档,`.confirmbar .go` 宽度 ≥44px,文字不换行截断,截图对比修复前后。
- C2:人轨锁定一份作战单后,方向三个按钮视觉可辨识为禁用态,点击无副作用(`state.planDraft.direction` 不变)。
- C3:三种拦截场景各测一遍——(a)`fresh:false`,(b)`state.realPhase !== "mid"`,(c)查看非当前相位——每种场景下 `.inbtn` 按钮都是禁用视觉态,不是只有点击后才知道被拦。
- console error = 0;v5/R4 零 diff。

## DOD

- [ ] C1/C2/C3 全部完成,浏览器截图(修复前/后对比)附 PR
- [ ] C3 的三个拦截分支(相位不对/非当前查看相位/行情陈旧)全部核实,不只修 stale 一条
- [ ] 新增测试 + 全仓 pytest 全绿
- [ ] v5/R4 零 diff;decision-log 追加末尾
