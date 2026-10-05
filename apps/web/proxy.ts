import { NextResponse, type NextRequest } from "next/server";

import { SHEN_ORIGINAL_PATH_HEADER, SHEN_SESSION_COOKIE } from "@/lib/workspace-auth";

/**
 * 工作区登录闸门：未登录访问任何工作区页面时，重定向到 /login 并携带原始
 * 站内路径与查询参数（深链直达与站内导航是同一条路径），登录成功后原样回
 * 到刚才的位置——与 lib/api.ts 的 401 处理共用登录页的 next= 回跳。
 * 已登录的请求把原始路径放进请求头，layout 的兜底检查也能带上它。
 */
export default function proxy(request: NextRequest) {
  const originalPath = `${request.nextUrl.pathname}${request.nextUrl.search}`;
  if (!request.cookies.has(SHEN_SESSION_COOKIE)) {
    const login = new URL("/login", request.url);
    login.searchParams.set("next", originalPath);
    return NextResponse.redirect(login);
  }
  const headers = new Headers(request.headers);
  headers.set(SHEN_ORIGINAL_PATH_HEADER, originalPath);
  return NextResponse.next({ request: { headers } });
}

export const config = {
  // 工作区页面都是无扩展名的站内路径；api（登录与后端代理）、login、
  // _next 静态资源与带扩展名的公共文件（icon.svg、sw.js 等）不走这道闸门。
  matcher: ["/((?!api|_next|login|.*\\..*).*)"],
};
