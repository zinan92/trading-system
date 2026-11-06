/* 持仓 / 委托 / 成交 tables (#1328), moved from GridMind's renderTables. Read-only: GridMind's render()
 * still decides which orders are accepted lifecycles and which rung protects them, and publishes the
 * result in data-props; this only lays the cells out. 手动平仓 stays GridMind's: the button carries the
 * same data-close / data-side and GridMind's delegated listener on #positions handles the click. */
import type {ReactNode} from "react";
import {beijingDateTime, finiteNumber, num, pnlClass, quantityText, signed} from "../gridmind/format";

type Row = Record<string, unknown>;
export interface TablesProps {
  table?: "positions" | "orders" | "fills" | "baseline";
  trades?: Row[];
  fills?: Row[];
  open_positions?: Row[];
  orders?: Row[];          // accepted lifecycle orders, oldest first, each with rung_tp / rung_sl (null when GridMind has none)
  dca_target?: string | null;
  can_close?: boolean;
  loaded?: boolean;        // false until GridMind's first render: an empty table, not 「暂无记录」
}

const pos = (text: string) => <span className="pos">{text}</span>;
const neg = (text: string) => <span className="neg">{text}</span>;
const str = (v: unknown) => String(v ?? "--");

export function side(value: unknown): ReactNode {
  return ["buy", "long"].includes(value as string) ? pos("多") : ["sell", "short"].includes(value as string) ? neg("空") : str(value);
}

export function fillAction(fill: Row): ReactNode {
  const entry = String(fill?.event || "entry").toLowerCase() === "entry", buy = String(fill?.side || "").toLowerCase() === "buy";
  if (entry) return buy ? pos("开多") : neg("开空");
  return buy ? pos("平空") : neg("平多");
}

export function protectionText(order: Row, key: "tp" | "sl"): string {
  const protection = (order?.protection || {}) as Row;
  return protection.status === "known" ? num(protection[key]) : "未知";
}

const plannedProfitText = (order: Row) => (finiteNumber(order?.planned_net_profit_usd) === null ? "未知" : `${num(order.planned_net_profit_usd)} USD`);
const pnl = (v: unknown) => <span className={pnlClass(v)}>{signed(v)}</span>;

function Table({heads, rows}: {heads: string[]; rows: ReactNode[][]}) {
  return (
    <>
      <thead><tr>{heads.map(head => <th key={head}>{head}</th>)}</tr></thead>
      <tbody>{rows.length ? rows.map((row, i) => <tr key={i}>{row.map((value, j) => <td key={j}>{value}</td>)}</tr>) : <tr><td colSpan={heads.length}>暂无记录</td></tr>}</tbody>
    </>
  );
}

export function ExecTables(props: TablesProps) {
  if (!props.loaded) return null;
  const fills = props.fills || [], trades = props.trades || [], dca = props.dca_target || null;
  if (props.table === "baseline")
    return <Table heads={["时间（北京）", "动作", "价格", "数量"]} rows={fills.slice(-14).reverse().map(fill => [beijingDateTime(fill.ts), fillAction(fill), num(fill.price), quantityText(fill.quantity ?? fill.pnl_units)])} />;
  if (props.table === "positions")
    return <Table heads={["状态", "方向", "数量", "开仓时间（北京）", "开仓价", "止盈", "止损", "未实现", "操作"]} rows={(props.open_positions || []).map(trade => [
      "持仓中", side(trade.side), quantityText(trade.remaining_quantity ?? trade.remaining_units ?? trade.quantity), beijingDateTime(trade.entry_ts ?? trade.ts),
      num(trade.entry_price), dca || protectionText(trade, "tp"), protectionText(trade, "sl"), pnl(trade.unrealized_pnl),
      props.can_close ? <button className="button" data-close={str(trade.trade_id)} data-side={str(trade.side)}>手动平仓</button> : "登录后可平仓",
    ])} />;
  if (props.table === "orders")
    return <Table heads={["状态", "方向", "类型", "数量", "价格", "止盈", "止损", "计划净利", "时间（北京）"]} rows={(props.orders || []).slice().reverse().map(order => [
      str(order.state_label || "未知状态"), side(order.side), order.order_type === "limit" ? "限价" : "市价", quantityText(order.quantity), num(order.price),
      dca ? "整轮 TP（非 entry order）" : (order.rung_tp as string | null) ?? protectionText(order, "tp"), (order.rung_sl as string | null) ?? protectionText(order, "sl"),
      plannedProfitText(order), beijingDateTime(order.updated_at ?? order.ts),
    ])} />;
  if (!trades.length && fills.length)
    return <Table heads={["时间（北京）", "方向", "价格", "数量", "手续费", "来源"]} rows={fills.slice(-50).reverse().map(fill => {
      const at = fill.ts ?? fill.timestamp ?? fill.time, stamp = typeof at === "number" ? new Date(at).toISOString() : at, buy = ["B", "buy", "BUY"].includes(fill.side as string);
      return [beijingDateTime(stamp), buy ? "买入" : "卖出", num(fill.price), quantityText(fill.quantity ?? fill.size), fill.fee == null ? "--" : num(fill.fee), str(fill.environment === "testnet" ? "测试盘成交" : fill.source || "--")];
    })} />;
  return <Table heads={["状态", "方向", "数量", "入场时间（北京）", "入场价", "出场时间（北京）", "出场价", "结果", "已实现"]} rows={trades.slice().reverse().map(trade => [
    trade.status === "open" ? "持仓中" : trade.status === "chronology_invalid" ? "时间异常" : "已完成", side(trade.side),
    quantityText(trade.entry_quantity ?? trade.quantity ?? trade.units), beijingDateTime(trade.entry_ts ?? trade.ts), num(trade.entry_price),
    trade.exit_ts ? beijingDateTime(trade.exit_ts) : "持仓中", trade.exit_price == null ? "--" : num(trade.exit_price), str(trade.close_reason_label || "未知"), pnl(trade.realized_pnl),
  ])} />;
}
