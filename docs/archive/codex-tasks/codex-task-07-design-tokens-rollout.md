# Codex 任务 07:统一设计 Token 全站铺开(Phase D 落地)

> 设计已定稿并获所有者批准(2026-07-08):见 `mockups/design-tokens-proposal.html`——**该页即规范**,它本身就是用批准的 token 渲染的,任何视觉疑问以它为准。三个开放问题所有者已全部按建议拍板:①运维页转深色;②K线涨跌色统一到 run/block;③放弃 IBM Plex,用系统字体栈。
> 你(Codex)严格按本规格实现。这是**纯样式层任务**:零业务逻辑改动、零数据流改动、零交互行为改动。
>
> ## 两条不可违反的硬约束
> 1. **盲答协议**:任何页面改动不得让机器轨点位在收盘前出现在盘中主图/上下文图(样式任务本不该碰这些,列出以防连带)。`tests/test_dashboard_dualtrack_static.py` 里的盲答回归断言必须保持全绿。
> 2. **decision-log.md 追加在文件末尾**,不许插到开头。

## 交付物

### 1. `assets/tokens.css`(新建,全站唯一的视觉真相源)

从 `mockups/design-tokens-proposal.html` 的 `:root` 抽取,字体栈按 Q3 决议改为系统栈(去掉 IBM Plex 引用):

```css
:root{
  /* surfaces */
  --bg:#0a0b0c; --panel:#101216; --panel2:#15181e;
  /* ink */
  --ink:#e8eaed; --muted:#9aa3ad; --faint:#5d666f;
  /* brand (the ONLY decorative color) */
  --gold:#d8aa3f;
  /* semantic states (color == state, nothing else) */
  --run:#35d07f; --warn:#e8a33d; --block:#ef5f5f; --unknown:#7d8590;
  /* track identity */
  --human:#7aa2ff; --machine:#b894ff;
  /* hairlines & shape */
  --rule:rgba(255,255,255,.08); --rule-strong:rgba(255,255,255,.16); --r:6px;
  /* type */
  --mono:"SFMono-Regular","Roboto Mono",Menlo,Consolas,ui-monospace,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
  /* type scale (integer only) */
  --fs-0:10px; --fs-1:11px; --fs-2:12px; --fs-3:13px; --fs-4:16px; --fs-5:22px; --fs-6:34px;
  /* spacing scale */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px;
}
```

### 2. 五个现役页 + shell 全部改为引用 token

对象:`command-center.html`、`dashboard-dualtrack-v5.html`、`dashboard-dualtrack-replay.html`、`ops-dashboard.html`、`dashboard-replay-v4.html`、`assets/shell.css`(shell 自身的两个布局变量保留,颜色引用 token)。

每页的机械步骤:
1. `<head>` 加 `<link rel="stylesheet" href="assets/tokens.css">`(在页内 style 之前;replay 页注意相对路径)。
2. 删除页内 `:root` 里与 token 重复/冲突的定义;页内旧变量名全部替换到新 token:
   - `--surface/--panel` → `--panel`;`--surface2/--panel2` → `--panel2`;`--surface3` → `--panel2`
   - `--text` → `--ink`;`--line` → `--rule`;`--line2` → `--rule-strong`
   - `--amber`(指挥台的 #d8aa3f)→ `--gold`;作战台/v4 的 `--amber:#f5b84b` 用途逐处判断:表状态的 → `--warn`,表品牌的 → `--gold`
   - `--teal` 退役:表盈利/正向的 → `--run`;`--red` → `--block`;`--blue` → `--human`;`--violet` → `--machine`
   - `--gray` → `--unknown` 或 `--muted`(按用途)
3. **字重收敛**:所有 640/660/720/740/760/820/900 → 就近归入 600 或 700(<700 归 600,≥700 归 700);400 保持。
4. **字号收敛**:10.5→10 或 11、11.5→11 或 12、12.5→12(按视觉层级就近);其余映射到 --fs-* 阶梯;30/28/24/22 的大数字统一归 --fs-5(22)或 --fs-6(34),按"关键数字 vs 裁决带"层级判断。
5. **盒内边距规则**:所有带边框的内嵌格子(`.plan-metric`、`.kv` 等)水平内边距 ≥ `--sp-3`(12px);`.plan-metric` 明确改为 `padding:10px 14px`。
6. box-shadow 全部移除(`--shadow` 变量删除);圆角统一 `--r`;金色调边框(指挥台 `--line2:rgba(216,170,63,.26)`)除裁决带/品牌时刻外禁用,改 `--rule-strong`。
7. **v4 特别清理**:删除全部 `--v1-*` 残留变量及其引用(引用处替换到新 token)。
8. 双语题头收敛:如"运行状态 · SYSTEM VERDICT"式中英并列题头,保留中文,英文可作 mono 小注或删除(逐处判断,PR 列出)。

### 3. 运维页转深色(Q1 决议)

`ops-dashboard.html` 整页从浅色纸面转入统一深色系统:`--bg:#f6efe4`→token 深色、`--ink:#17130c`→`--ink`、`--panel:#fffaf1`→`--panel`、浅色系 blue/green/red/gold 全部映射到 `--human/--run/--block/--gold`。**只换颜色与字体引用,不改布局结构与文案**。转换后逐屏检查对比度(浅色页遗留的 rgba 深色叠加在深底上会不可见——如 `--line: rgba(38,31,18,.16)` 这类必须换成 `--rule`)。

### 4. K线涨跌色对齐(Q2 决议)

StandardKline 图表实例的涨跌色配置(up/down color,现为 teal/red 系)统一为 `--run`/`--block` 的字面值(#35d07f/#ef5f5f)。改动点:各页构造 `StandardKlineChart` 或 `setAdaptedData` 时传入的颜色选项、以及 `packages/standard-kline/standard-kline.js` 的**默认值**(允许改默认色值,不许改任何逻辑)。人轨/机器轨的标记与价格线继续用 `--human`/`--machine` 身份色,不受此条影响。

### 5. 防回归静态测试(这次统一的"锁")

新增 `tests/test_design_tokens_static.py`:
1. `assets/tokens.css` 存在且含全部核心 token 名(逐个断言)。
2. 六个对象文件(五页+shell.css)都引用 `tokens.css`(shell.css 用 var() 引用即可,断言其不再定义颜色字面量)。
3. **页面不得再定义自有颜色字面量**:对每个 HTML 的 `<style>` 块提取 `#rrggbb`/`rgba(...)` 字面量,允许白名单 = token 定义值 + 少量必要例外(如 `rgba(0,0,0,...)` 遮罩,列成显式白名单),超出白名单的字面量 → 测试失败。这是防止未来任何 agent 偷偷加新颜色的机关。
4. 禁用字重断言:六个文件中不得出现 `font-weight:640|660|720|740|760|820|900`。
5. 禁用半像素字号断言:不得出现 `font-size:10.5|11.5|12.5px`。
6. ops 页深色断言:不再含 `#f6efe4`/`#fffaf1`/`#17130c`。
7. 盲答回归:确认 `test_dashboard_dualtrack_static.py` 既有断言不动且全绿。

既有静态测试若断言了旧颜色/旧变量名,按意图更新(PR 逐个列出改了哪些、为什么)。

## 验证

- TDD:先写 `test_design_tokens_static.py` 看它红(tokens.css 不存在、页面满是字面量),再逐步变绿。
- 全仓 `python3 -m pytest -q` 全绿;R4 锁定文件零 diff。
- **浏览器逐页截图验收**(PR 附截图):指挥台/作战台/双轨回放/运维/replay-v4 五页 + console error 各 = 0;运维页深色后每个区块文字可读(特别检查原浅色 rgba 边线/底色有没有变成不可见);作战台图表涨跌色已是绿涨红跌;盲答面板格子文字不再顶边。
- 视觉基准:与 `mockups/design-tokens-proposal.html` 的 B/C 区并排对照,面板题头、格子、四态色一致。

## 硬约束(重申)

- 纯样式:不改任何 JS 业务逻辑/数据流/API/交互行为(K线涨跌色的配置值除外);不碰订单/执行/broker/风控;R4 零 diff。
- 不引入字体文件/CDN/构建步骤(Q3 决议:系统字体栈)。
- 盲答协议回归断言保持全绿;decision-log 追加末尾。
- `mockups/design-tokens-proposal.html` 是规范文档,**不要修改它**(如实现中发现规范矛盾处,在 PR 里提出,不要自行改规范)。
- decision-log 记录:token 唯一来源原则、三个 Q 的决议、颜色字面量白名单机制、旧变量名→新 token 的映射表。

## DOD

- [ ] `test_design_tokens_static.py` 7 类断言全绿 + 全仓全绿
- [ ] 五页浏览器截图 + console 0 错误,附进 PR
- [ ] 运维页深色转换后无不可读区块
- [ ] 盲答回归断言全绿
- [ ] PR 列出:全部旧值→新 token 映射决策、双语题头逐处处理、颜色白名单最终内容、所有自行决定的点
