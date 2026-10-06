import { cookies } from "next/headers";
import { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const COOKIE_NAME = "shen_session";

type RouteContext = { params: Promise<{ path: string[] }> };

async function proxy(request: NextRequest, context: RouteContext) {
  const token = (await cookies()).get(COOKIE_NAME)?.value;
  if (!token) {
    return Response.json({ detail: "Not authenticated" }, { status: 401 });
  }
  const { path } = await context.params;
  const baseUrl = process.env.API_INTERNAL_URL ?? "http://localhost:8000";
  const target = `${baseUrl}/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  const headers = new Headers();
  headers.set("Authorization", `Bearer ${token}`);
  headers.set("Accept", request.headers.get("accept") ?? "application/json");
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);

  const hasBody = !["GET", "HEAD"].includes(request.method);
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? await request.arrayBuffer() : undefined,
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    // A JSON 502 instead of an unhandled route error. The request may still
    // have landed, so callers keep treating it as unconfirmed (5xx is never
    // a definitive save rejection) and the message says to check first.
    return Response.json(
      { detail: "无法连接后端服务，请稍后重试。若刚提交保存或生成，请先核对结果再重试。" },
      { status: 502, headers: { "Cache-Control": "no-store, private" } },
    );
  }
  const responseHeaders = new Headers();
  for (const name of ["content-type", "content-disposition", "x-correlation-id"]) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  responseHeaders.set("Cache-Control", "no-store, private");
  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
