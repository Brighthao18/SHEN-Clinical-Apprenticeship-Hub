import type { Metadata } from "next";

import { PatientTimeline } from "./patient-timeline";

export const metadata: Metadata = { title: "患者工作区" };

export default async function PatientPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ encounter?: string; resume?: string }>;
}) {
  const { id } = await params;
  const { encounter, resume } = await searchParams;
  return (
    <PatientTimeline
      key={id}
      patientId={id}
      initialEncounterId={encounter}
      initialResumeSessionId={resume}
    />
  );
}
