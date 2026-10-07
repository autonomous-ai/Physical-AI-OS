import assert from "node:assert/strict";
import test from "node:test";
import { shouldSendOnEnter } from "../src/pages/monitor/chat/composerKeys.ts";
const enter = { key: "Enter", shiftKey: false, isComposing: false, keyCode: 13 };
test("plain Enter sends while Shift+Enter keeps a newline", () => {
  assert.equal(shouldSendOnEnter(enter), true);
  assert.equal(shouldSendOnEnter({ ...enter, shiftKey: true }), false);
});
test("IME confirmation never sends, including Safari's keyCode 229", () => {
  assert.equal(shouldSendOnEnter({ ...enter, isComposing: true }), false);
  assert.equal(shouldSendOnEnter({ ...enter, keyCode: 229 }), false);
  assert.equal(shouldSendOnEnter({ ...enter, key: "Process", keyCode: 229 }), false);
});
