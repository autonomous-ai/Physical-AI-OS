import assert from "node:assert/strict";
import test from "node:test";
import { confirmWifiConnection, isWifiHandoffError } from "../src/pages/settings/wifiReconnect.ts";

test("only a Wi-Fi save transport failure is an ambiguous handoff", () => {
  assert.equal(isWifiHandoffError(new TypeError("Load failed"), true), true);
  assert.equal(isWifiHandoffError(new TypeError("Failed to fetch"), false), false);
  assert.equal(isWifiHandoffError(Object.assign(new Error("Unauthorized"), { status: 401 }), true), false);
  assert.equal(isWifiHandoffError(Object.assign(new TypeError("Bad request"), { status: 400 }), true), false);
  assert.equal(isWifiHandoffError(new SyntaxError("Unexpected JSON"), true), false);
});

test("confirmation requires the actual target SSID, after interrupted reads", async () => {
  const responses = [new TypeError("offline"), null, { ssid: "old" }, { ssid: "target" }];
  let calls = 0;
  const result = await confirmWifiConnection("target", async () => {
    calls++;
    const value = responses.shift();
    if (value instanceof Error) throw value;
    return value;
  }, new AbortController().signal, 500, 1);
  assert.equal(result, "connected");
  assert.equal(calls, 4);
});

test("unreachable or wrong networks time out without reporting success", async () => {
  assert.equal(await confirmWifiConnection("target", async () => ({ ssid: "old" }), new AbortController().signal, 20, 1), "unconfirmed");
});

test("deadline aborts an in-flight read", async () => {
  let aborted = false;
  const result = await confirmWifiConnection("target", (signal) => new Promise((_, reject) => {
    signal.addEventListener("abort", () => { aborted = true; reject(new Error("aborted")); }, { once: true });
  }), new AbortController().signal, 20, 1);
  assert.equal(result, "unconfirmed");
  assert.equal(aborted, true);
});

test("unmount cancels the read and no further reads happen", async () => {
  const controller = new AbortController();
  let calls = 0;
  const result = await confirmWifiConnection("target", (signal) => new Promise((_, reject) => {
    calls++;
    signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    controller.abort();
  }), controller.signal, 500, 1);
  assert.equal(result, "cancelled");
  assert.equal(calls, 1);
});

test("already cancelled checks make no requests", async () => {
  const controller = new AbortController();
  controller.abort();
  assert.equal(await confirmWifiConnection("target", async () => { throw new Error("must not read"); }, controller.signal), "cancelled");
});
