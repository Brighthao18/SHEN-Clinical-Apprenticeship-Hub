import type { Metadata } from "next";
import Link from "next/link";

import { ProviderSettings } from "./provider-settings";

export const metadata: Metadata = { title: "AI Provider 设置" };

export default function SettingsPage() {
  return (
    <div>
      <ProviderSettings />
      <section className="surface mt-7 rounded-2xl p-5 sm:p-6">
        <h2 className="text-base font-bold text-stone-900">维护与说明</h2>
        <p className="mt-2 text-sm leading-6 text-stone-600">
          AI 结果与医生记录分层保存，任何缺失的临床事实保持未记录。原图出境的确认始终发生在上传处：Mock 模式仅处理明确粘贴的 OCR 文本；MinerU 模式会收到未经预脱敏的原始图片，上传前需再次确认。
        </p>
        <Link href="/reviews" className="focus-ring mt-3 inline-flex items-center gap-1 text-sm font-medium text-[#1f6857] hover:underline">
          兼容审查记录（维护入口）→
        </Link>
      </section>
    </div>
  );
}
