import type { Metadata, Viewport } from "next";

import { ServiceWorkerRegistration } from "@/components/service-worker-registration";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "师承临床知识库",
    template: "%s · 师承临床知识库",
  },
  description: "可追溯、可重算的个人中医师承临床知识管理系统",
  applicationName: "师承临床知识库",
  manifest: "/manifest.webmanifest",
};

export const viewport: Viewport = {
  themeColor: "#f4f1e9",
  colorScheme: "light",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        {children}
        <ServiceWorkerRegistration />
      </body>
    </html>
  );
}
