import assert from "node:assert/strict";
import test from "node:test";
import { RequestGeneration } from "../lib/request-generation.ts";

test("a delayed prior batch response cannot overwrite a newer selection", async () => {
  const requests = new RequestGeneration();
  let finishOld;
  let displayed = "";
  const oldGeneration = requests.next();
  const oldRead = new Promise((resolve) => { finishOld = resolve; }).then(() => {
    if (requests.accepts(oldGeneration)) displayed = "old batch";
  });
  const newGeneration = requests.next();
  if (requests.accepts(newGeneration)) displayed = "new batch";
  finishOld();
  await oldRead;
  assert.equal(displayed, "new batch");
});

test("refresh and unmount invalidate already running reads", () => {
  const requests = new RequestGeneration();
  const polling = requests.current();
  const refresh = requests.next();
  assert.equal(requests.accepts(polling), false);
  assert.equal(requests.accepts(refresh), true);
  requests.next();
  assert.equal(requests.accepts(refresh), false);
});
