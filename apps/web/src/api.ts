/* GET-only reads for the islands. Same error wording as gm-desk.js call(); a 401 goes to /login.
 * There is no POST helper here on purpose: order and control requests stay in the legacy page
 * until #1329 moves them with byte-identical bodies. */

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
