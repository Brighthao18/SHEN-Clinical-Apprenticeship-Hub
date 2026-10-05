"use client";

import { LockKeyhole, UserRound } from "lucide-react";
import { FormEvent, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

/** 只接受站内相对路径，拒绝 //、外链与协议型地址（开放重定向防护）。 */
function safeNextPath(raw: string | null): string {
  if (raw && /^\/[^/\\]/.test(raw)) return raw;
  return "/";
}

export function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const next = safeNextPath(searchParams.get("next"));
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
      });
      if (!response.ok) {
        let detail = "登录失败";
        try {
          const body = (await response.json()) as { detail?: string };
          detail = body.detail ?? detail;
        } catch {
          // Non-JSON upstream body (e.g. an HTML error page): keep the
          // generic message instead of crashing on .json().
        }
        setError(detail);
        return;
      }
      // 回到登录前的位置（未保存编辑与待核对保存都在本机原样保留）。
      router.replace(next);
      router.refresh();
    } catch {
      setError("无法连接服务器，请确认服务已启动");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={submit} className="surface rounded-2xl p-6 sm:p-7">
      <label className="block text-sm font-medium text-stone-700" htmlFor="username">
        用户名
      </label>
      <div className="relative mt-2">
        <UserRound aria-hidden="true" className="absolute left-3 top-3 text-stone-400" size={18} />
        <input
          id="username"
          name="username"
          autoComplete="username"
          defaultValue="admin"
          required
          className="focus-ring w-full rounded-xl border border-stone-300 bg-white py-2.5 pl-10 pr-3 text-sm"
        />
      </div>
      <label className="mt-5 block text-sm font-medium text-stone-700" htmlFor="password">
        密码
      </label>
      <div className="relative mt-2">
        <LockKeyhole aria-hidden="true" className="absolute left-3 top-3 text-stone-400" size={18} />
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          className="focus-ring w-full rounded-xl border border-stone-300 bg-white py-2.5 pl-10 pr-3 text-sm"
        />
      </div>
      {error ? (
        <p role="alert" className="mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      ) : null}
      <button
        type="submit"
        disabled={submitting}
        className="focus-ring mt-6 w-full rounded-xl bg-[#1f6857] px-4 py-3 text-sm font-semibold text-white hover:bg-[#185548] disabled:opacity-60"
      >
        {submitting ? "正在验证…" : "进入我的师承工作台"}
      </button>
    </form>
  );
}
