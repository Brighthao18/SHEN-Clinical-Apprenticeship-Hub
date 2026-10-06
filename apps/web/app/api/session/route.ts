import { NextRequest, NextResponse } from "next/server";

import { exchangeCredentials } from "@/lib/session-login";

const COOKIE_NAME = "shen_session";

export async function POST(request: NextRequest) {
  let payload: unknown = null;
  try {
    payload = await request.json();
  } catch {
    // Malformed body: exchangeCredentials answers 422 for missing fields.
  }
  const result = await exchangeCredentials(
    payload,
    `${process.env.API_INTERNAL_URL ?? "http://localhost:8000"}/api/v1/auth/token`,
  );
  if (!result.ok) {
    return NextResponse.json({ detail: result.detail }, { status: result.status });
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, result.token, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.COOKIE_SECURE === "true",
    path: "/",
    maxAge: result.expiresIn,
  });
  return response;
}

export async function DELETE() {
  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, "", { httpOnly: true, path: "/", maxAge: 0 });
  return response;
}
