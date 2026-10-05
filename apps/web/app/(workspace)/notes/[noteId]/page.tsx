import type { Metadata } from "next";

import { WritingWorkspace } from "./writing-workspace";

export const metadata: Metadata = { title: "作品编辑" };

export default async function NoteEditorPage({
  params,
  searchParams,
}: {
  params: Promise<{ noteId: string }>;
  searchParams: Promise<{ resume?: string }>;
}) {
  const { noteId } = await params;
  const { resume } = await searchParams;
  return <WritingWorkspace noteId={noteId} resumeSessionId={resume} />;
}
