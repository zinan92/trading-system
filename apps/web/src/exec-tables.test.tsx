import {act} from "@testing-library/react";
import {createRoot} from "react-dom/client";
import {ExecTables, type TablesProps} from "./islands/ExecTables";
import {beijingDateTime, finiteNumber, num, quantityText, signed} from "./gridmind/format";
import legacy from "./gridmind/legacy-tables.json";

/* legacy-tables.json was captured once (#1328) from the last GridMind that built these tables itself
 * (bc12a08 dashboard-gridmind.html): for each read model, the props the new GridMind
 * publishes and the innerHTML the old renderTables produced from the same data. */
type Case = {name: string; props: Record<string, TablesProps>; legacy: Record<string, string>};

function renderTable(props: TablesProps): string {
  const table = document.createElement("table");
  document.body.appendChild(table);
  const root = createRoot(table);
  act(() => root.render(<ExecTables {...props} />));
  const html = table.innerHTML;
  act(() => root.unmount());
  table.remove();
  return html;
}

describe("持仓 / 委托 / 成交 render exactly what GridMind's renderTables did", () => {
  for (const c of legacy as Case[]) {
    for (const id of ["positions", "orders", "fills", "baselineFills"]) {
      it(`${c.name} · #${id}`, () => {
        expect(renderTable(c.props[id]!)).toBe(c.legacy[id]);
      });
    }
  }

  it("covers the close button, a DCA target, the fills-only path and many orders", () => {
    const all = (legacy as Case[]).flatMap(c => Object.values(c.legacy)).join("");
    expect(all).toContain('data-close="');
    expect(all).toContain("聚合 TP #3");
    expect(all).toContain("<th>手续费</th>");
    expect((legacy as Case[])[0]!.props.orders!.orders?.length).toBeGreaterThan(20);
  });
});

describe("before GridMind's first render", () => {
  it("draws an empty table, not 「暂无记录」", () => {
    expect(renderTable({table: "positions"})).toBe("");
  });
});

describe("the close button is GridMind's", () => {
  it("is a plain button with data-close / data-side and no handler of its own", () => {
    const props: TablesProps = {table: "positions", loaded: true, can_close: true, open_positions: [{trade_id: "t<1>", side: "long", quantity: 1}]};
    const html = renderTable(props);
    expect(html).toContain('<button class="button" data-close="t&lt;1&gt;" data-side="long">手动平仓</button>');
    expect(renderTable({...props, can_close: false})).toContain("<td>登录后可平仓</td>");
  });
});

describe("formatters match GridMind's", () => {
  it("num / signed / finiteNumber / quantityText", () => {
    expect([num(null), num(""), num("x"), num(1234.567), num(0)]).toEqual(["--", "--", "--", "1,234.57", "0"]);
    expect([signed(2), signed(-2.5), signed(0), signed(undefined)]).toEqual(["+2", "-2.5", "0", "--"]);
    expect([finiteNumber(""), finiteNumber(undefined), finiteNumber("3")]).toEqual([null, null, 3]);
    expect([quantityText(1234.1234567), quantityText(null)]).toEqual(["1234.123457", "0"]);
  });
  it("beijingDateTime reads seconds, milliseconds, micro- and nanoseconds", () => {
    const at = "2026-07-18 09:02";
    expect(beijingDateTime("2026-07-18T01:02:00+00:00")).toBe(at);
    expect(beijingDateTime(1784336520)).toBe(at);
    expect(beijingDateTime(1784336520000)).toBe(at);
    expect(beijingDateTime("1784336520000000")).toBe(at);
    expect(beijingDateTime("1784336520000000000")).toBe(at);
    expect(beijingDateTime("")).toBe("--");
  });
});
