import { Bot, BookOpen, FileText, PenLine, WandSparkles } from "lucide-react";

const sourceMap = {
  physician_record: {
    label: "医生记录",
    icon: FileText,
    style: "border-stone-300 bg-stone-50 text-stone-700",
  },
  student_note: {
    label: "学生补录",
    icon: PenLine,
    style: "border-blue-200 bg-blue-50 text-blue-800",
  },
  ai_normalized: {
    label: "AI 标准化",
    icon: WandSparkles,
    style: "border-emerald-200 bg-emerald-50 text-emerald-800",
  },
  ai_inferred: {
    label: "AI 推断",
    icon: Bot,
    style: "border-amber-200 bg-amber-50 text-amber-800",
  },
  external_reference: {
    label: "外部资料",
    icon: BookOpen,
    style: "border-violet-200 bg-violet-50 text-violet-800",
  },
} as const;

export function ProvenanceBadge({ source }: { source: string }) {
  const known = sourceMap[source as keyof typeof sourceMap];
  // An unmapped source must never silently display as 医生记录 — provenance
  // honesty requires surfacing the raw value instead.
  const item = known ?? {
    label: source,
    icon: FileText,
    style: "border-stone-300 bg-stone-50 text-stone-700",
  };
  const Icon = item.icon;
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] ${item.style}`}>
      <Icon aria-hidden="true" size={12} />
      {item.label}
    </span>
  );
}
