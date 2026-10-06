/**
 * Credential exchange behind POST /api/session, kept free of Next imports so
 * it can be unit-tested. Only an API 401 means wrong credentials: an
 * unreachable API, an upstream error page or an unreadable token response is
 * reported as what it is, never as a password problem and never as a crash.
 */
export type SessionLoginResult =
  | { ok: true; token: string; expiresIn: number }
  | { ok: false; status: number; detail: string };

export async function exchangeCredentials(
  payload: unknown,
  tokenUrl: string,
  fetchImpl: typeof fetch = fetch,
): Promise<SessionLoginResult> {
  const { username, password } = (payload ?? {}) as { username?: unknown; password?: unknown };
  if (typeof username !== "string" || !username || typeof password !== "string" || !password) {
    return { ok: false, status: 422, detail: "请输入用户名和密码" };
  }
  let upstream: Response;
  try {
    upstream = await fetchImpl(tokenUrl, {
      method: "POST",
      body: new URLSearchParams({ username, password }),
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      cache: "no-store",
    });
  } catch {
    return { ok: false, status: 503, detail: "无法连接后端服务，请确认服务已启动" };
  }
  if (upstream.status === 401) {
    return { ok: false, status: 401, detail: "用户名或密码不正确" };
  }
  if (!upstream.ok) {
    return {
      ok: false,
      status: 502,
      detail: `登录服务暂时不可用（${upstream.status}），请稍后重试`,
    };
  }
  try {
    const body = (await upstream.json()) as { access_token?: unknown; expires_in?: unknown };
    if (
      typeof body?.access_token === "string" &&
      body.access_token &&
      typeof body.expires_in === "number" &&
      body.expires_in > 0
    ) {
      return { ok: true, token: body.access_token, expiresIn: body.expires_in };
    }
  } catch {
    // Not JSON: fall through to the unreadable-response result.
  }
  return { ok: false, status: 502, detail: "登录服务返回了无法读取的结果，请稍后重试" };
}
