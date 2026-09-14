# Park 交易台 · trading-desk

Park 每天在一个页面里完成交易，顺序是：**看新闻 → 看 K 线并判断多空 → 系统出计划 → 批准 → 盯盘 → 复盘**。支持 BTC 和黄金两个品种。

```
in   Intel 新闻（本机 8001，带重要/关注/噪音分级）
     Dashboard（本机 8765）：BTC Testnet K 线、黄金 XAUUSDT K 线、黄金纸面盘持仓与盈亏
     trading-system 的网格记录（~/work/park-paper-output）
     Hyperliquid 测试盘公开账户接口（只读）
out  一页交易台，地址 http://127.0.0.1:8790
     Park 的判断、批准和随手记，存在 ~/park-data/trading-desk/desk.db

fail 新闻服务连不上     → 新闻栏显示上一次读到的列表并标明；一次都没读到过就写明原因
fail K 线读不到         → 图表区写明原因，页面其他部分照常
fail 网格文件正在写入   → 显示"稍后自动刷新"，页面每 60 秒刷新一次
fail 现价读不到         → 计划标为"暂不可用"，批准按钮不可点，但仍可以只记录判断
fail 对账不是 ok        → 持仓盈亏那一行标出"对账异常，数字可能不准"
```

## 边界

- **本服务从不下单。** 批准只是把 Park 的决定和计划记下来，交给执行员走 trading-system Dashboard 的预览和确认流程。`/api/health` 里 `submits_orders: false` 表示本服务没有任何下单代码。
- **画网格线的 K 线，一律用网格下单时用的那个价格。** BTC 用 Hyperliquid Testnet 的价格，黄金用 Binance XAUUSDT 永续的价格。不用 datafeed 的研究行情（比如 `GC=F`），否则网格线相对价格的位置会画错。
- **两个账户分开显示，不合计。** BTC 测试盘和黄金纸面盘的钱不是一回事，加在一起没有意义。
- **只写自己的库。** 本服务只写 `desk.db`，不改 trading-system 的任何文件。

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
