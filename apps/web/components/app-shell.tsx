"use client";

import {
  BookLock,
  BookOpenText,
  FileUp,
  HeartPulse,
  LayoutDashboard,
  LogOut,
  Search,
  Settings2,
  Users,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";

// Four daily entries; maintenance surfaces live in the secondary group and
// stay out of the daily path (mobile bottom bar carries only the primary
// four, settings is reached from the top bar).
const primaryNavigation = [
  { href: "/", label: "工作台", icon: LayoutDashboard },
  { href: "/patients", label: "患者", icon: Users },
  { href: "/notes", label: "写作", icon: BookOpenText },
  { href: "/imports", label: "导入", icon: FileUp },
] as const;

const secondaryNavigation = [
  { href: "/dictionary", label: "中药词典", icon: BookLock },
  { href: "/settings", label: "服务设置", icon: Settings2 },
] as const;

type NavItem = {
  href: string;
  label: string;
  icon: typeof LayoutDashboard;
};

function NavLink({ href, label, icon: Icon }: NavItem) {
  const pathname = usePathname();
  const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={`focus-ring flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors ${
        active ? "bg-[#dcebe5] text-[#174f43]" : "text-stone-600 hover:bg-white/70 hover:text-stone-900"
      }`}
    >
      <Icon aria-hidden="true" size={18} strokeWidth={1.8} />
      <span>{label}</span>
    </Link>
  );
}

export function AppShell({ children, demo = false }: { children: React.ReactNode; demo?: boolean }) {
  const router = useRouter();
  const pathname = usePathname();
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState("");

  async function logout() {
    if (loggingOut) return;
    if (!window.confirm("确定退出登录吗？请先保存正在编辑的内容。")) return;
    setLoggingOut(true);
    setLogoutError("");
    try {
      const response = await fetch("/api/session", { method: "DELETE" });
      if (!response.ok) throw new Error("logout failed");
      router.replace("/login");
      router.refresh();
    } catch {
      setLogoutError("退出未完成，请检查连接后重试。");
    } finally {
      setLoggingOut(false);
    }
  }

  return (
    <div className="min-h-screen md:grid md:grid-cols-[248px_1fr]">
      <a href="#main-content" className="focus-ring sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-xl focus:bg-white focus:p-3">跳至主要内容</a>
      <aside className="hidden border-r border-[#d9d3c5] bg-[#ece8dd] md:fixed md:inset-y-0 md:flex md:w-[248px] md:flex-col">
        <div className="border-b border-[#d9d3c5] px-5 py-5">
          <div className="flex items-center gap-3">
            <span className="grid size-10 place-items-center rounded-xl bg-[#1f6857] text-white">
              <HeartPulse aria-hidden="true" size={21} />
            </span>
            <div>
              <p className="text-sm font-bold tracking-wide text-stone-900">师承临床知识库</p>
              <p className="mt-0.5 text-xs text-stone-500">证据驱动 · 本地优先</p>
            </div>
          </div>
        </div>
        <nav className="flex-1 space-y-1 px-3 py-4" aria-label="主导航">
          {primaryNavigation.map((item) => (
            <NavLink key={item.href} {...item} />
          ))}
          <p className="px-3 pb-1 pt-5 text-xs font-semibold tracking-wide text-stone-400">维护与设置</p>
          {secondaryNavigation.map((item) => (
            <NavLink key={item.href} {...item} />
          ))}
        </nav>
        <div className="border-t border-[#d9d3c5] p-3">
          <button
            type="button"
            onClick={logout}
            disabled={loggingOut}
            className="focus-ring flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm text-stone-600 hover:bg-white/70"
          >
            <LogOut aria-hidden="true" size={18} />
            {loggingOut ? "正在退出…" : "退出登录"}
          </button>
        </div>
      </aside>

      <div className="min-w-0 md:col-start-2">
        {demo && <div role="status" className="bg-amber-100 px-4 py-3 text-center text-sm text-amber-950">完全合成演示数据 · 请勿上传真实患者资料 · OCR/AI 外网连接已隔离</div>}
        <header className="sticky top-0 z-30 flex h-16 items-center justify-between border-b border-[#d9d3c5] bg-[#f4f1e9]/95 px-4 backdrop-blur md:px-8">
          <div className="flex items-center gap-3 md:hidden">
            <span className="grid size-9 place-items-center rounded-xl bg-[#1f6857] text-white">
              <HeartPulse aria-hidden="true" size={19} />
            </span>
            <span className="text-sm font-bold">师承临床知识库</span>
          </div>
          <div className="hidden text-sm text-stone-500 md:block">我的师承临床学习空间</div>
          <div className="flex items-center gap-1">
            <Link
              href="/patients"
              aria-label="查找患者"
              className="focus-ring inline-flex items-center gap-2 rounded-xl border border-[#d9d3c5] bg-white/60 px-3 py-2 text-sm text-stone-600 hover:bg-white"
            >
              <Search aria-hidden="true" size={16} />
              <span className="hidden sm:inline">查找患者</span>
            </Link>
            <Link
              href="/settings"
              aria-label="服务设置"
              className="focus-ring rounded-xl p-3 text-stone-600 hover:bg-white/70 md:hidden"
            >
              <Settings2 aria-hidden="true" size={18} />
            </Link>
            <button type="button" onClick={logout} disabled={loggingOut} aria-label={loggingOut ? "正在退出" : "退出登录"} className="focus-ring rounded-xl p-3 text-stone-600 disabled:opacity-60 md:hidden"><LogOut aria-hidden="true" size={18} /></button>
          </div>
        </header>
        <main id="main-content" tabIndex={-1} className="mx-auto w-full max-w-[1500px] px-4 py-6 pb-28 sm:px-6 md:px-8 md:py-8 md:pb-10">
          {logoutError ? <p role="alert" className="mb-4 rounded-xl bg-red-50 p-3 text-sm text-red-700">{logoutError}</p> : null}
          {children}
        </main>
      </div>

      <nav
        className="fixed inset-x-0 bottom-0 z-40 border-t border-[#d9d3c5] bg-[#f8f5ed]/95 px-2 pt-2 pb-[max(0.5rem,env(safe-area-inset-bottom))] backdrop-blur md:hidden"
        aria-label="移动端主导航"
      >
        <div className="flex gap-1">
          {primaryNavigation.map(({ href, label, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              aria-current={(href === "/" ? pathname === "/" : pathname.startsWith(href)) ? "page" : undefined}
              className="focus-ring flex min-w-[4.6rem] flex-1 flex-col items-center gap-1 rounded-lg py-1 text-[11px] leading-4 text-stone-600 aria-[current=page]:bg-[#dcebe5] aria-[current=page]:font-bold aria-[current=page]:text-[#174f43]"
            >
              <Icon aria-hidden="true" size={19} />
              {label}
            </Link>
          ))}
        </div>
      </nav>
    </div>
  );
}
