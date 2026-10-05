import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { Suspense } from "react";

import { LoginForm } from "./login-form";

export default async function LoginPage() {
  if ((await cookies()).has("shen_session")) redirect("/");
  return (
    <main className="subtle-grid grid min-h-screen place-items-center px-4 py-12">
      <div className="w-full max-w-md">
        <div className="mb-6 text-center">
          <p className="text-xs font-semibold tracking-[0.22em] text-[#1f6857]">LOCAL-FIRST</p>
          <h1 className="mt-3 text-3xl font-bold tracking-tight text-stone-900">师承临床知识库</h1>
          <p className="mt-2 text-sm leading-6 text-stone-600">资料在本机保存；OCR、结构化抽取和原图核验可向已确认服务发送含身份信息的资料。病程分析与写作使用脱敏上下文。</p>
        </div>
        {/* useSearchParams 需要 Suspense 边界（next= 登录后回跳原位置）。 */}
        <Suspense fallback={<div className="surface h-72 rounded-2xl" aria-busy="true" />}>
          <LoginForm />
          {process.env.SHEN_DEMO_MODE === "true" && <p className="mt-4 text-sm text-amber-900">完全合成演示 · 账号 admin · 密码 shen-demo-only</p>}
        </Suspense>
        <p className="mt-5 text-center text-xs leading-5 text-stone-500">
          V1 为单用户系统。请勿使用默认密码存放真实患者资料。
        </p>
      </div>
    </main>
  );
}
