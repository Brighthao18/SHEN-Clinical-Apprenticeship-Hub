export class ApiError extends Error {
  public readonly status: number;
  constructor(
    message: string,
    status: number,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  let response: Response;
  try {
    response = await fetch(`/api/backend${path}`, {
    ...init,
    cache: "no-store",
    headers,
    });
  } catch (reason) {
    if (init?.signal?.aborted) throw reason;
    throw new ApiError("无法连接服务，请检查连接。若刚提交保存或生成，请先核对结果再重试。", 0);
  }
  if (response.status === 401 && typeof window !== "undefined") {
    // 回登录页时记住原位置：登录成功后回到刚才的编辑/列表，而不是工作台。
    const next = encodeURIComponent(`${window.location.pathname}${window.location.search}`);
    window.location.replace(`/login?next=${next}`);
    throw new ApiError("登录已过期", 401);
  }
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object") {
        const { detail, error } = body as { detail?: unknown; error?: { message?: unknown } };
        if (typeof detail === "string" && detail.trim()) message = detail;
        else if (Array.isArray(detail)) message = "提交内容未通过校验，请检查必填项与输入格式。";
        else if (typeof error?.message === "string") message = error.message;
      }
    } catch {
      // Upstream did not return JSON.
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError("服务返回了无法读取的结果，请先刷新核对操作是否完成。", response.status);
  }
}

export function apiContentUrl(path: string): string {
  return `/api/backend${path}`;
}
