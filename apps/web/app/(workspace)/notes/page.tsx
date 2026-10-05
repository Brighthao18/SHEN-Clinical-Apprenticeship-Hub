import type { Metadata } from "next";

import { WritingLibrary } from "./writing-library";

export const metadata: Metadata = { title: "师承写作" };

export default function NotesPage() {
  return <WritingLibrary />;
}
