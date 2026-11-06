/* 账户指标卡 (#1353), moved from GridMind's render(). GridMind still picks the source (Nautilus paper
 * ledger or the execution account) and computes every number; it publishes [title, value, sub, class]
 * per card and this lays them out exactly as its innerHTML did. Nothing before GridMind's first render. */
export type Card = [title: string, value: string, sub: string, klass: string];

export function AccountCards({cards, loaded}: {cards?: Card[]; loaded?: boolean}) {
  if (!loaded || !cards) return null;
  return (
    <>
      {cards.map(([title, value, sub, klass]) => (
        <div className="metric" key={title}><span>{title}</span><strong className={klass}>{value}</strong><small>{sub}</small></div>
      ))}
    </>
  );
}
