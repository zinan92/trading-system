/* Reads, and the few allow-listed POSTs, for the islands. Same error wording as gm-desk.js call();
 * a 401 goes to /login. Order and control requests stay in the legacy page until #1329 moves them
 * with byte-identical bodies. */

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, {cache: "no-store"});
  const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  if (res.status === 401) {
    window.location.href = "/login";
    throw new ApiError("需要重新登录", 401);
  }
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : typeof data.message === "string" ? data.message : "请求失败，请稍后再试";
    throw new ApiError(detail, res.status);
  }
  return data as T;
}

/** The POSTs an island may send (decision-log #1335, widened by #1327). None of them can place,
 * approve for execution, stop or change an order:
 * - backtest/compare starts a history calculation;
 * - plan builds a plan from the price and saves nothing;
 * - judgments records Park's judgment (and, when he approves, the desk previews; 执行 stays in the
 *   legacy page until #1329 moves it with a byte-identical body);
 * - advice/refresh asks the model for today's advice again. */
export const COMPARE_PATH = "/api/backtest/compare";
export const POST_PATHS = [COMPARE_PATH, "/api/plan", "/api/judgments", "/api/advice/refresh"] as const;
export type PostPath = (typeof POST_PATHS)[number];

/** `body` is sent exactly as given when it is a string, so a caller can fix the byte order. */
export async function postJson<T>(path: PostPath, body: unknown): Promise<T> {
  if (!(POST_PATHS as readonly string[]).includes(path)) throw new ApiError(`不允许的请求：${path}`, 0);
  const res = await fetch(path, {method: "POST", headers: {"content-type": "application/json"}, body: typeof body === "string" ? body : JSON.stringify(body)});
  const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  if (res.status === 401) {
    window.location.href = "/login";
    throw new ApiError("需要重新登录", 401);
  }
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : typeof data.message === "string" ? data.message : "请求失败，请稍后再试";
    throw new ApiError(detail, res.status);
  }
  return data as T;
}

export const startComparison = (body: unknown) => postJson<{key: string}>(COMPARE_PATH, body);
