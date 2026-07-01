import io, json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, "/Users/wendy/trading-orchestrator")
os.environ["BINANCE_API_KEY"] = "x"
os.environ["BINANCE_API_SECRET"] = "y"

from services.live_reconciliation import LiveBrokerReconciliation

RUN_DATE = "2026-06-15"
def ms(dt): return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)

# boundary timestamps for RUN_DATE 2026-06-15
day_start = ms(datetime(2026,6,15,0,0,0,0))                 # 00:00:00.000 -> IN
just_before = ms(datetime(2026,6,14,23,59,59,999000))       # prev day 23:59:59.999 -> OUT
edge_last = ms(datetime(2026,6,15,23,59,59,999000))         # 23:59:59.999 -> IN
next_day_start = ms(datetime(2026,6,16,0,0,0,0))            # next day 00:00:00.000 -> OUT
mid_prev = ms(datetime(2026,6,10,12,0,0))                   # multi-day earlier -> OUT
mid_day = ms(datetime(2026,6,15,12,0,0))                    # -> IN

# realizedPnl tagged so we can tell which rows got summed
FILLS = [
    {"symbol":"XAUUSDT","id":"1","time":just_before,   "realizedPnl":"-1000","commission":"1","commissionAsset":"USDT"}, # OUT
    {"symbol":"XAUUSDT","id":"2","time":day_start,     "realizedPnl":"-1",    "commission":"1","commissionAsset":"USDT"}, # IN
    {"symbol":"XAUUSDT","id":"3","time":mid_day,       "realizedPnl":"-2",    "commission":"1","commissionAsset":"USDT"}, # IN
    {"symbol":"XAUUSDT","id":"4","time":edge_last,     "realizedPnl":"-4",    "commission":"1","commissionAsset":"USDT"}, # IN
    {"symbol":"XAUUSDT","id":"5","time":next_day_start,"realizedPnl":"-8000", "commission":"1","commissionAsset":"USDT"}, # OUT
    {"symbol":"XAUUSDT","id":"6","time":mid_prev,      "realizedPnl":"-9000", "commission":"1","commissionAsset":"USDT"}, # OUT
]
INCOME = [
    {"symbol":"XAUUSDT","incomeType":"FUNDING_FEE","income":"-500","asset":"USDT","time":mid_prev,     "tranId":"a"},  # OUT
    {"symbol":"XAUUSDT","incomeType":"FUNDING_FEE","income":"-0.5","asset":"USDT","time":day_start,    "tranId":"b"},  # IN
    {"symbol":"XAUUSDT","incomeType":"FUNDING_FEE","income":"-0.25","asset":"USDT","time":edge_last,   "tranId":"c"},  # IN
    {"symbol":"XAUUSDT","incomeType":"FUNDING_FEE","income":"-700","asset":"USDT","time":next_day_start,"tranId":"d"}, # OUT
]

class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self,*a): return False

def make_opener(server_honors_time):
    captured = {}
    def opener(request, timeout=None):
        url = request.full_url
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)
        endpoint = urlparse(url).path
        captured.setdefault(endpoint, q)
        if "/userTrades" in endpoint:
            rows = FILLS
            if server_honors_time and "startTime" in q and "endTime" in q:
                s,e = int(q["startTime"][0]), int(q["endTime"][0])
                rows = [r for r in FILLS if s <= r["time"] <= e]
            return Resp(json.dumps(rows).encode())
        if "/income" in endpoint:
            rows = INCOME
            if server_honors_time and "startTime" in q and "endTime" in q:
                s,e = int(q["startTime"][0]), int(q["endTime"][0])
                rows = [r for r in INCOME if s <= r["time"] <= e]
            return Resp(json.dumps(rows).encode())
        return Resp(b"[]")
    return opener, captured

cfg = {"margin_asset":"USDT","instrument_map":{"GOLD":"XAUUSDT"},"trade_history_limit":1000,"income_history_limit":1000,"base_url":"https://demo-fapi.binance.com"}

# Expected IN-window realized PnL from fills: -1 + -2 + -4 = -7 ; commission = 3 USDT
# Expected IN-window funding: -0.5 + -0.25 = -0.75
# net_realized_pnl_estimate = -7 + (-0.75) - 3 = -10.75

for honors in (True, False):
    label = "server-honors-time" if honors else "server-IGNORES-time (returns ALL rows)"
    opener, captured = make_opener(honors)
    rec = LiveBrokerReconciliation("/tmp/x_out_p1", broker_config=cfg, opener=opener)
    fills = rec.exchange_fills("XAUUSDT", RUN_DATE)
    income = rec.exchange_income("XAUUSDT", RUN_DATE)
    acct = rec._exchange_accounting(fills, income, run_date=RUN_DATE)
    ids = sorted(f["id"] for f in fills)
    print(f"\n=== {label} ===")
    print("  fill ids kept:", ids, "(expect ['2','3','4'])")
    print("  net_realized_pnl_estimate:", acct["net_realized_pnl_estimate"], "(expect -10.75)")
    print("  utc_trading_day:", acct["utc_trading_day"])
    # verify query params
    ut = captured.get("/fapi/v1/userTrades", {})
    print("  query startTime:", ut.get("startTime"), "endTime:", ut.get("endTime"))
    # boundary window numbers
    assert ids == ["2","3","4"], f"WINDOW LEAK: {ids}"
    assert abs(acct["net_realized_pnl_estimate"] - (-10.75)) < 1e-9, acct["net_realized_pnl_estimate"]

# Verify window ms are exactly [00:00:00.000, 23:59:59.999]
s,e = rec._utc_day_window_ms(RUN_DATE)
print("\nwindow start_ms:", s, "==", day_start, s==day_start)
print("window end_ms:  ", e, "==", edge_last, e==edge_last)
print("gap to next-day 00:00 in ms:", next_day_start - e, "(expect 1)")
print("\nALL ASSERTIONS PASSED" )
