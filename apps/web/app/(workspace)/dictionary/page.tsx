import type { Metadata } from "next";

import { DrugDictionaryView } from "./drug-dictionary-view";

export const metadata: Metadata = { title: "中药词典" };

export default function DictionaryPage() {
  return <DrugDictionaryView />;
}
