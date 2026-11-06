import {act} from "@testing-library/react";
import {createRoot} from "react-dom/client";
import {AccountCards, type Card} from "./islands/AccountCards";
import legacy from "./gridmind/legacy-cards.json";

/* legacy-cards.json was captured once (#1353) from the last GridMind that built these cards itself
 * (3d3aede dashboard-gridmind.html): the cards the new GridMind publishes and the old innerHTML. */
type Case = {name: string; props: {account: {cards: Card[]; loaded: boolean}}; legacy: {account: string}};

function html(props: {cards?: Card[]; loaded?: boolean}): string {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => root.render(<AccountCards {...props} />));
  const out = host.innerHTML;
  act(() => root.unmount());
  host.remove();
  return out;
}

describe("账户指标卡 render exactly what GridMind's render() did", () => {
  for (const c of legacy as unknown as Case[]) it(c.name, () => expect(html(c.props.account)).toBe(c.legacy.account));

  it("covers both sources: the paper ledger and the execution account", () => {
    const all = (legacy as unknown as Case[]).map(c => c.legacy.account).join("");
    expect(all).toContain("纸面盘权益");
    expect(all).toContain("账户权益");
    expect(all).toContain("盈亏尚未核算");
  });
});

it("draws nothing before GridMind's first render", () => {
  expect(html({})).toBe("");
});
