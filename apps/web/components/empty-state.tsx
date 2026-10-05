import { Inbox } from "lucide-react";

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="surface flex min-h-48 flex-col items-center justify-center rounded-2xl px-6 py-10 text-center">
      <Inbox aria-hidden="true" className="mb-3 text-stone-400" size={28} />
      <h3 className="font-semibold text-stone-800">{title}</h3>
      <p className="mt-1 max-w-md text-sm leading-6 text-stone-500">{detail}</p>
    </div>
  );
}
