import {act} from "@testing-library/react";
import {createRoot} from "react-dom/client";
import {ControlPicker} from "./islands/ControlPicker";
import gridmind from "../../../dashboard-gridmind.html?raw";

function draw(props: Record<string, unknown>): HTMLElement {
  const host = document.createElement("div");
  const root = createRoot(host);
  act(() => root.render(<ControlPicker {...props} />));
  return host;
}

const legacy = new DOMParser().parseFromString(gridmind, "text/html").querySelector("#dashboardPicker")!;
const venues = [{value: "okx.paper", label: "OKX Paper"}, {value: "hyperliquid.testnet", label: "Hyperliquid Testnet"}];
const buttons = (host: HTMLElement) => [...host.querySelectorAll<HTMLButtonElement>("[data-dashboard-strategy]")];
const venueOf = (host: HTMLElement) => host.querySelector<HTMLSelectElement>("#dashboardVenueSelect")!;
const instrumentOf = (host: HTMLElement) => host.querySelector<HTMLSelectElement>("#dashboardInstrumentSelect")!;

describe("the picker draws what GridMind publishes", () => {
  it("is the legacy markup, node for node, before anything is published", () => {
    const host = draw({});
    expect(legacy.children).toHaveLength(3);
    expect(host.children).toHaveLength(3);
    [...legacy.children].forEach((node, i) => expect(host.children[i]!.isEqualNode(node)).toBe(true));
  });

  it("every control carries the intent GridMind listens for", () => {
    const names = [...draw({}).querySelectorAll<HTMLElement>("[data-gm-intent]")].map(node => node.dataset.gmIntent);
    expect(names).toEqual(["control.venue", "control.instrument", "control.strategy", "control.strategy"]);
    expect(names).toEqual([...legacy.querySelectorAll<HTMLElement>("[data-gm-intent]")].map(node => node.dataset.gmIntent));
  });

  it("shows the selection, a blocked instrument and its reason", () => {
    const host = draw({
      venues, venue: "hyperliquid.testnet", instrument: "BTC-USD-PERP", instrumentDisabled: false, strategy: "dca", strategyDisabled: false,
      instruments: [{value: "", label: "选择 Instrument"}, {value: "BTC-USD-PERP", label: "BTC", title: "可用于 Preview"}, {value: "SOL-USD-PERP", label: "SOL · 暂不可交易", disabled: true, title: "没有行情"}],
    });
    expect(venueOf(host).value).toBe("hyperliquid.testnet");
    expect(instrumentOf(host).value).toBe("BTC-USD-PERP");
    expect(instrumentOf(host).disabled).toBe(false);
    const blocked = instrumentOf(host).options[2]!;
    expect([blocked.disabled, blocked.title, blocked.text]).toEqual([true, "没有行情", "SOL · 暂不可交易"]);
    expect(buttons(host).map(button => [button.disabled, button.className])).toEqual([[false, "choice"], [false, "choice on"]]);
  });

  it("a retired selection shows the venue and offers nothing to press", () => {
    // What production publishes after 「之前的选择 … 已停用」: no instrument, so no strategy, no preview.
    const host = draw({venues, venue: "okx.paper", instruments: [{value: "", label: "选择 Instrument"}, {value: "XAU-USDT-SWAP", label: "XAU", title: "可用于 Preview"}], instrument: "", instrumentDisabled: false, strategy: "", strategyDisabled: true});
    expect(venueOf(host).value).toBe("okx.paper");
    expect(instrumentOf(host).value).toBe("");
    expect(instrumentOf(host).selectedOptions[0]!.text).toBe("选择 Instrument");
    expect(buttons(host).every(button => button.disabled && button.className === "choice")).toBe(true);
  });

  it("states why an instrument list is empty", () => {
    const host = draw({venues, venue: "okx.paper", instruments: [{value: "", label: "Instrument Catalog 暂不可用"}], instrumentDisabled: true});
    expect(instrumentOf(host).disabled).toBe(true);
    expect(instrumentOf(host).selectedOptions[0]!.text).toBe("Instrument Catalog 暂不可用");
  });
});
