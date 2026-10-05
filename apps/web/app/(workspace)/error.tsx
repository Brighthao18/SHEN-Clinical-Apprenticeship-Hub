"use client";

import Link from "next/link";

export default function WorkspaceError({ reset }: { reset: () => void }) {
  return (
    <section role="alert" className="surface rounded-2xl p-6">
      <h1 className="text-xl font-semibold">页面暂时无法显示</h1>
      <p className="mt-2 text-sm text-stone-600">请重新加载页面。如果刚提交保存或生成，请先核对结果，再决定是否重复操作。</p>
      <div className="mt-5 flex flex-wrap gap-3">
        <button type="button" onClick={reset} className="focus-ring rounded-xl bg-[#1f6857] px-4 py-2 text-sm text-white">重新加载</button>
        <Link href="/" className="focus-ring rounded-xl border border-stone-300 px-4 py-2 text-sm">返回工作台</Link>
      </div>
    </section>
  );
}
