import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";

import { AppShell } from "@/components/app-shell";
import { SHEN_ORIGINAL_PATH_HEADER, SHEN_SESSION_COOKIE } from "@/lib/workspace-auth";

export default async function WorkspaceLayout({ children }: { children: React.ReactNode }) {
  // proxy.ts 是主闸门（重定向时已带 next= 原位置）；这里只在请求绕过了
  // proxy 时兜底，并读取 proxy 写入的原始路径头，保持同样的回跳行为。
  if (!(await cookies()).has(SHEN_SESSION_COOKIE)) {
    const originalPath = (await headers()).get(SHEN_ORIGINAL_PATH_HEADER);
    redirect(originalPath ? `/login?next=${encodeURIComponent(originalPath)}` : "/login");
  }
  return <AppShell demo={process.env.SHEN_DEMO_MODE === "true"}>{children}</AppShell>;
}
