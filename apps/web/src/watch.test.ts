import {metaLine, whenLabel} from "./watch";

const NOW = Date.parse("2026-11-05T04:00:00Z");  // 周四 12:00 北京

test("scheduled items count down in Beijing time", () => {
  expect(whenLabel({at: "2026-11-07T13:30:00Z"}, NOW)).toBe("周六 21:30 · 还有 2 天 9 小时");
  expect(whenLabel({at: "2026-11-05T06:15:00Z"}, NOW)).toBe("周四 14:15 · 还有 2 小时 15 分");
  expect(whenLabel({at: "2026-11-05T04:20:00Z"}, NOW)).toBe("周四 12:20 · 还有 20 分钟");
  expect(whenLabel({at: "2026-11-05T03:00:00Z"}, NOW)).toBe("周四 11:00 · 已公布");
});

test("breaking items show only the time and a bad date shows nothing", () => {
  expect(whenLabel({kind: "breaking", at: "2026-11-05T01:05:00Z"}, NOW)).toBe("突发 · 09:05");
  expect(whenLabel({at: "not a date"}, NOW)).toBe("");
});

test("the header says how the three were ranked", () => {
  expect(metaLine(null)).toBe("还在生成");
  expect(metaLine({generated_at: "2026-11-05T09:12:00+08:00", provider: "codex"})).toBe("09:12 更新 · 按全市场冲击排序");
  expect(metaLine({generated_at: "2026-11-05T09:12:00+08:00", provider: "rules"})).toBe("09:12 更新 · 模型暂不可用，按日历重要性排序");
});
