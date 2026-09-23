# Park 交易台 · trading-desk

Park 做交易的唯一入口。**交易**（`/trade`）就是 GridMind 工作台本身（trading-system 8765，经交易台同源转发、同一道口令），交易台只在上面加左侧滚动新闻和右侧「今日判断 · 执行」；**日报**（`/desk#news`）是晨报（开头是「昨天判断对了吗」和今天的一键判断）、K 线日报卡片、宏观周报卡片，原版全文可展开；**系统**（`/desk#system`）是数据是否正常、品种管理、执行记录、已暂停的服务。需求说明见 `docs/spec-v2.md`。

```
in   Intel 新闻（本机 8001，带重要/关注/噪音分级）
     Dashboard（本机 8765）：BTC Testnet K 线、黄金 XAUUSDT K 线、黄金纸面盘持仓与盈亏
     trading-system 的网格记录（~/work/park-paper-output）
     Hyperliquid 测试盘公开账户接口（只读）
out  http://127.0.0.1:8790/trade（交易）· /desk（日报、系统）
     Park 的判断、批准和随手记，存在 ~/park-data/trading-desk/desk.db

fail 新闻服务连不上     → 新闻栏显示上一次读到的列表并标明；一次都没读到过就写明原因
fail K 线读不到         → 图表区写明原因，页面其他部分照常
fail 网格文件正在写入   → 显示"稍后自动刷新"，页面每 60 秒刷新一次
fail 现价读不到         → 计划标为"暂不可用"，批准按钮不可点，但仍可以只记录判断
fail 对账不是 ok        → 持仓盈亏那一行标出"对账异常，数字可能不准"
```

## 边界

- **只有 Park 亲自按「执行」才会下单，并且只在 Hyperliquid 测试盘。** 流程如下：
  1. 批准：只调用 Dashboard 做一次预览，不下单。
  2. 按执行：先重新预览一次。以下任何一种情况都拒绝下单：已经有网格在跑、预览没通过、最多亏损比 Park 看到的数字多出 5% 以上。
  3. 以上都通过，才以 Park 的名义确认，并调用下单程序一次。
  4. 定时器、打开页面、刷新页面都不会触发下单。每份计划只能执行一次。
- **黄金纸面盘不能从交易台执行**，批准只做记录。
- **画网格线的 K 线，用的是网格下单时同一个交易所的价格**：Hyperliquid 测试盘的 K 线，或 Binance XAUUSDT 永续的 K 线。不用研究用的行情（例如 `GC=F`），否则网格线会画错位置。
- **两个账户分开显示，不相加。**
- **交易台只写自己的库**（`desk.db` 和执行回执），不修改 trading-system 的任何文件。

## 复盘怎么算

判断记下时，把当时的价格一起存下来。72 小时后，取那个时间点的 1 小时 K 线收盘价来对照：

| 判断 | 说中了 | 没说中 | 基本没动 |
|---|---|---|---|
| 做多 | 涨 ≥ 0.5% | 跌 ≥ 0.5% | 涨跌都不到 0.5% |
| 做空 | 跌 ≥ 0.5% | 涨 ≥ 0.5% | 涨跌都不到 0.5% |
| 观望 | 涨跌都不到 2% | 涨或跌超过 2% | — |

## 运行

```bash
PYTHONPATH=src python3 -m trading_desk serve          # 默认端口 8790
PYTHONPATH=src python3 -m pytest -q tests
```

常驻服务由 launchd 的 `com.wendy.trading-desk` 管理，日志在 `~/Library/Logs/TradingDesk/`。环境变量见 `src/trading_desk/config.py`。
