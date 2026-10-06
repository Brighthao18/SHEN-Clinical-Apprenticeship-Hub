import assert from "node:assert/strict";
import test from "node:test";
import { exchangeCredentials } from "../lib/session-login.ts";

const TOKEN_URL = "http://api.test/api/v1/auth/token";
const CREDENTIALS = { username: "admin", password: "synthetic-password" };

function respondWith(response) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (response instanceof Error) throw response;
    return response;
  };
  return { calls, fetchImpl };
}

test("a valid login returns the token and lifetime and posts form credentials", async () => {
  const { calls, fetchImpl } = respondWith(
    Response.json({ access_token: "synthetic.jwt", token_type: "bearer", expires_in: 28800 }),
  );
  const result = await exchangeCredentials(CREDENTIALS, TOKEN_URL, fetchImpl);
  assert.deepEqual(result, { ok: true, token: "synthetic.jwt", expiresIn: 28800 });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, TOKEN_URL);
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body.toString(), "username=admin&password=synthetic-password");
});

test("missing or non-string fields are rejected before calling the API", async () => {
  for (const payload of [
    null,
    "not an object",
    {},
    { username: "admin" },
    { username: "", password: "x" },
    { username: "admin", password: 123 },
  ]) {
    const { calls, fetchImpl } = respondWith(Response.json({}));
    const result = await exchangeCredentials(payload, TOKEN_URL, fetchImpl);
    assert.deepEqual(result, { ok: false, status: 422, detail: "请输入用户名和密码" });
    assert.equal(calls.length, 0);
  }
});

test("only an API 401 is reported as wrong credentials", async () => {
  const { fetchImpl } = respondWith(
    Response.json({ detail: "Incorrect username or password" }, { status: 401 }),
  );
  const result = await exchangeCredentials(CREDENTIALS, TOKEN_URL, fetchImpl);
  assert.deepEqual(result, { ok: false, status: 401, detail: "用户名或密码不正确" });
});

test("an unreachable API is reported as unavailable, not as a crash", async () => {
  const { fetchImpl } = respondWith(new TypeError("fetch failed"));
  const result = await exchangeCredentials(CREDENTIALS, TOKEN_URL, fetchImpl);
  assert.equal(result.ok, false);
  assert.equal(result.status, 503);
  assert.match(result.detail, /无法连接后端服务/);
});

test("an upstream error page is a gateway failure, never a password problem", async () => {
  const { fetchImpl } = respondWith(
    new Response("Internal Server Error", { status: 500, headers: { "content-type": "text/plain" } }),
  );
  const result = await exchangeCredentials(CREDENTIALS, TOKEN_URL, fetchImpl);
  assert.equal(result.ok, false);
  assert.equal(result.status, 502);
  assert.match(result.detail, /（500）/);
  assert.doesNotMatch(result.detail, /密码/);
});

test("a success response without a usable token is not turned into a session", async () => {
  for (const body of [
    "<html>not json</html>",
    JSON.stringify(null),
    JSON.stringify({ expires_in: 60 }),
    JSON.stringify({ access_token: "", expires_in: 60 }),
    JSON.stringify({ access_token: "synthetic.jwt" }),
    JSON.stringify({ access_token: "synthetic.jwt", expires_in: 0 }),
  ]) {
    const { fetchImpl } = respondWith(new Response(body, { status: 200 }));
    const result = await exchangeCredentials(CREDENTIALS, TOKEN_URL, fetchImpl);
    assert.deepEqual(result, {
      ok: false,
      status: 502,
      detail: "登录服务返回了无法读取的结果，请稍后重试",
    });
  }
});
