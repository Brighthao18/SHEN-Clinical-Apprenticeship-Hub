import { NextRequest, NextResponse } from "next/server";

const COOKIE_NAME = "shen_session";

export async function POST(request: NextRequest) {
  const payload = (await request.json()) as { username?: string; password?: string };
  if (!payload.username || !payload.password) {
    return NextResponse.json({ detail: "请输入用户名和密码" }, { status: 422 });
  }
  const form = new URLSearchParams({
    username: payload.username,
    password: payload.password,
  });
  const upstream = await fetch(
    `${process.env.API_INTERNAL_URL ?? "http://localhost:8000"}/api/v1/auth/token`,
    {
      method: "POST",
      body: form,
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      cache: "no-store",
    },
  );
  const body = await upstream.json();
  if (!upstream.ok) {
    return NextResponse.json({ detail: "用户名或密码不正确" }, { status: upstream.status });
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, body.access_token, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.COOKIE_SECURE === "true",
    path: "/",
    maxAge: body.expires_in,
  });
  return response;
}

export async function DELETE() {
  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, "", { httpOnly: true, path: "/", maxAge: 0 });
  return response;
}
