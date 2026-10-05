import assert from "node:assert/strict";
import test from "node:test";
import { apiFetch, ApiError } from "../lib/api.ts";

test("API handles validation arrays without rendering objects or submitted clinical values", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({ detail: [{ msg: "invalid", input: "private-value" }] }, { status: 422 }));
  await assert.rejects(apiFetch("/example"), (error) => error instanceof ApiError && error.status === 422 && error.message.includes("校验") && !error.message.includes("private-value"));
});

test("API preserves Headers instances and accepts empty success", async (t) => {
  t.mock.method(globalThis, "fetch", async (_url, init) => {
    assert.equal(init.headers.get("Content-Type"), "application/json");
    assert.equal(init.headers.get("Accept"), "application/json");
    return new Response(null, { status: 204 });
  });
  assert.equal(await apiFetch("/example", { headers: new Headers({ "Content-Type": "application/json" }) }), undefined);
});

test("API explains network errors without automatically repeating writes", async (t) => {
  const mock = t.mock.method(globalThis, "fetch", async () => { throw new TypeError("Failed to fetch"); });
  await assert.rejects(apiFetch("/example", { method: "POST" }), /先核对结果/);
  assert.equal(mock.mock.callCount(), 1);
});

test("API keeps cancelled requests distinguishable", async (t) => {
  const controller = new AbortController();
  controller.abort();
  t.mock.method(globalThis, "fetch", async () => { throw controller.signal.reason; });
  await assert.rejects(apiFetch("/example", { signal: controller.signal }), { name: "AbortError" });
});

test("API provides safe fallback for non-JSON errors and malformed success", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("<html>error</html>", { status: 503 }));
  await assert.rejects(apiFetch("/example"), /503/);
  globalThis.fetch.mock.restore();
  t.mock.method(globalThis, "fetch", async () => new Response("not-json", { status: 200 }));
  await assert.rejects(apiFetch("/example"), /刷新核对/);
});
