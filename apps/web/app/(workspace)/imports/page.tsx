import type { Metadata } from "next";

import { ImportWorkspace } from "./workspace";

export const metadata: Metadata = { title: "本周导入" };

export default function ImportsPage() {
  return <ImportWorkspace />;
}
