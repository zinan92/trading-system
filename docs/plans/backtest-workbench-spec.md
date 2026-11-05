# Spec：回测工作台（BTC + 黄金，网格 + 定投，参数并排对比）

状态：Park 2026-11-05 授权按商定顺序直接做（追平 tick-stock-panel 计划的第 2 步）。
跟踪：父 Issue 见 PR 正文。数据口径：2026-11-05 仓库和生产实测。

## 1. 要解决什么

现在只有一种回测：黄金 Paper 预览通过后，「执行」上方那个框（#1309）。

它回答的是「这一份计划过去 180 天表现如何」。它回答不了下面这些问题：

- BTC 怎么样？（生产 datafeed 只存了 `XAU-USDT-SWAP` 的历史；BTC 的 report 返回 `no history kept for this series`）
- 定投（DCA）和网格比怎么样？
- 网格间距放宽一倍、止损收紧一点，会更好还是更差？

目标：在 `/trade` 加一个「回测」标签。选品种和策略，填 1–3 组参数，按一次「开始回测」，得到一张并排对比表。
数字的口径和开仓前回测框一致。

## 2. 原则

- **一个网格引擎。** 网格回测只用 `services/grid_backtest.py`（Nautilus 隔离回放，Paper 自己的补单和触边平仓规则）。
  不另写简化版：同一组参数在两个地方必须是同一个数。
- **定投用确定性模拟。** 定投没有挂单生命周期，按 1 分钟收盘价、固定金额、固定间隔买入，期末按最后收盘价估值。
  模拟口径写在结果上，手续费与网格用同一套 OKX Paper 假设费率，文案也相同。
- **数据不够就说不够。** 没有历史、有缺口、太旧，都返回「回测不可用：原因」。
  不从别的数据源补，不跨源拼接。
- **只是历史参考。** 回测不能放行或拦下任何启动；工作台没有「用这组参数开仓」按钮。
- **不抢 Paper 的机器。**
  - 全局同一时间只跑一次对比。
  - 每次对比只读一遍历史，几组参数在同一进程里依次跑。
  - 回放并发 `workers ≤ 2`。

## 3. 数据：BTC 历史

- datafeed 新增数据源 `okx_swap_crypto_public`（资产类别 crypto，允许 `BTC-USDT-SWAP`、`ETH-USDT-SWAP`），沿用 Binance 的做法：
  - `binance_usdm_futures` 是 commodity；
  - `binance_usdm_futures_research` 是 crypto；
  - 一个数据源只属于一个资产类别。
- `okx_history` 从单一 `INSTRUMENT` 改成 `SERIES = {instrument: (source_id, asset_class)}`，每个系列单独写覆盖报告。
  - 每天 08:40 的回填任务把所有系列都补一遍。
  - 任何一个系列失败，任务就以非 0 退出。
- `/api/history/{source}/{instrument}/report|funding` 只回答 `SERIES` 里的系列。

## 4. 引擎

| 策略 | 引擎 | 窗口 |
|---|---|---|
| 网格 | `grid_backtest.run_backtest`（现有）。合约精度和手续费来自品种的回测合约说明，不读 Paper preflight | 7 天一段，不重叠，180 天 |
| 定投 | 新 `services/dca_backtest.py`，纯函数 | 同上 |

- **网格 BTC 的前提：** 回放需要一份合约说明（精度、手续费）。现在只有黄金 Paper preflight 提供。
  - BTC 用一份显式的「回测合约说明」，状态标为 `backtest_only`，回放只在回测入口接受它。
  - 这是回放入口的一处变化，写进 decision-log。
  - Paper 路径照旧只接受 `ready_for_park_paper` 的 preflight。
- **定投参数：** 每次金额（USDT）、间隔（小时）、方向（只做多）。
- **定投每段输出：** 收益率、投入总额、期末市值、最大回撤（按每分钟净值算）、买入次数、平均成本。

## 5. 接口

- `POST /api/backtest/compare`：请求体 `{asset, strategy, variants: [params, ...]}`，最多 3 组，返回 `key`。
  - 已有对比在跑：返回 409「已有一个回测在跑」。
  - 参数越界：返回 422。
- `GET /api/backtest/compare/{key}`：`running`、`ready` 或 `unavailable`。
  - `ready` 时每组给出与开仓前回测框相同的汇总：中位、最差、最好、触边/止损比例、平均成交、分布。
  - 定投把「触边/止损比例」换成「最大回撤中位」。
- 前端组件只多出这**一条** POST。岛组件源码检查的规则改为：
  - 只允许 `/api/backtest/compare` 这一个 POST；
  - 仍然拒绝 `/api/execute`、`dashboard-control`、`/api/advice/refresh`。
  - 这是安全检查的变化，写进 decision-log。

## 6. 页面

「回测」标签（React 岛）：

- 品种：黄金 / BTC。
- 策略：网格 / 定投。
- 参数：每组一列。网格是区间宽度 %、格数、每格金额、止损 %；定投是每次金额、间隔。
  - 第一列默认填「今天的计划」或一组常用值。
- 「开始回测」。
- 结果：每组一列的对比表，加一行分布小图。
  - 最好的一格只加粗，不写「推荐」。
  - 底部写口径：费后、资金费覆盖了几段、假设费率尚未用账户核实。

## 7. 顺序

1. datafeed：BTC 历史（新数据源、`SERIES`、回填任务、接口）。生产回填并核对缺口为 0。
2. 定投引擎 + 对比接口 + 页面（黄金和 BTC 定投）。
3. 网格参数对比（黄金，现有引擎）。
4. BTC 网格（回测合约说明）。

## 8. 验收

- 生产 `/api/history/okx_swap_crypto_public/BTC-USDT-SWAP/report`：180 天，缺 0 分钟。
- 黄金网格：工作台第一列和开仓前回测框，在同一天、同一组参数下，数字一致。
- 定投：有单元测试按手算核对（固定价格序列 → 已知平均成本和收益）。
- 第二次「开始回测」在第一次没完时得到 409；Paper 的 cycle runner 和 dashboard 不受影响（跑回测时 boot receipt 仍 pass）。

## 9. 不做

- 不做参数自动寻优（网格搜索、遗传算法）。只比较 Park 填的几组。
- 不做「用这组参数开仓」。
- 不接新的交易所行情。BTC 只用 OKX 公共历史。
