import type { Metadata } from "next";

import { PatientDirectory } from "./patient-directory";

export const metadata: Metadata = { title: "患者" };

export default function PatientsPage() {
  return <PatientDirectory />;
}
