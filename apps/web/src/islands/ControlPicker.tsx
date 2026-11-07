/* The venue and instrument selects and the strategy buttons (#1396, ADR 0008). GridMind owns the
 * selection and publishes it; this draws it and nothing else. Pressing a control reaches GridMind
 * through the document-level intent router, so there are no handlers here and no requests. */
interface Row {value: string; label: string; disabled?: boolean; title?: string}
interface Props {
  venues?: Row[];
  venue?: string;
  instruments?: Row[];
  instrument?: string;
  instrumentDisabled?: boolean;
  strategy?: string;
  strategyDisabled?: boolean;
}

const STRATEGIES = [["grid", "Grid", "旧版网格"], ["dca", "DCA", "旧版 DCA"]] as const;

// A controlled select needs an onChange. GridMind's capture listener has already published the new value.
const published = () => {};

export function ControlPicker({
  venues = [{value: "", label: "读取中…"}],
  venue = "",
  instruments = [{value: "", label: "先选择 Venue"}],
  instrument = "",
  instrumentDisabled = true,
  strategy = "",
  strategyDisabled = true,
}: Props) {
  return (
    <>
      <div className="gm-pick-step">
        <span className="label">1 · Venue Profile</span>
        <select className="select" id="dashboardVenueSelect" data-gm-intent="control.venue" aria-label="选择 Venue Profile" value={venue} onChange={published}>
          {venues.map(row => <option key={row.value} value={row.value}>{row.label}</option>)}
        </select>
      </div>
      <div className="gm-pick-step">
        <span className="label">2 · Instrument</span>
        <select className="select" id="dashboardInstrumentSelect" data-gm-intent="control.instrument" aria-label="选择交易资产" disabled={instrumentDisabled} value={instrument} onChange={published}>
          {instruments.map(row => <option key={row.value} value={row.value} disabled={row.disabled === true} title={row.title}>{row.label}</option>)}
        </select>
      </div>
      <div className="gm-pick-step">
        <span className="label">3 · Strategy Family</span>
        <div className="choice-row" id="dashboardStrategyChoices">
          {STRATEGIES.map(([family, name, note]) => (
            <button key={family} className={strategy === family ? "choice on" : "choice"} data-dashboard-strategy={family} data-gm-intent="control.strategy" type="button" disabled={strategyDisabled}>
              {name}<small>{note}</small>
            </button>
          ))}
        </div>
      </div>
    </>
  );
}
