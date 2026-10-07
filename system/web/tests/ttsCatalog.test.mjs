import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = ts.transpileModule(
  readFileSync(new URL("../src/hooks/setup/useTTSCatalog.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS } },
).outputText;
const japanese = ["Shizuka", "Konoha", "Rin", "Asahi", "Hinata", "Hiroki"];
const flush = () => new Promise((resolve) => setImmediate(resolve));

// Exercise the real hook effects with controlled requests and React-style cleanup.
function mount(initial = {}) {
  const state = [], effects = [], pendingEffects = [], requests = [];
  let cursor = 0;
  const props = {
    ttsProvider: "elevenlabs", sttLanguage: "ja", ttsVoice: "Rachel", urlProvider: "", urlVoice: "",
    setTtsProvider: (value) => { props.ttsProvider = value; },
    setTtsVoice: (value) => { props.ttsVoice = value; },
    ...initial,
  };
  const module = { exports: {} };
  vm.runInNewContext(source, {
    exports: module.exports, console,
    require(name) {
      if (name === "react") return {
        useState(initialValue) {
          const index = cursor++;
          if (!(index in state)) state[index] = initialValue;
          return [state[index], (value) => { state[index] = value; }];
        },
        useEffect(effect, dependencies) {
          const index = cursor++, previous = effects[index];
          if (previous && dependencies.every((value, i) => Object.is(value, previous.dependencies[i]))) return;
          previous?.cleanup?.();
          const entry = { dependencies };
          effects[index] = entry;
          pendingEffects.push(() => { entry.cleanup = effect(); });
        },
      };
      assert.equal(name, "@/lib/api");
      return {
        getTTSProviders: async () => ["elevenlabs", "openai"],
        getTTSVoices: (provider, language) => new Promise((resolve, reject) => {
          requests.push({ provider, language, resolve, reject });
        }),
      };
    },
  });
  const render = (next = {}) => {
    Object.assign(props, next);
    cursor = 0;
    const result = module.exports.useTTSCatalog(props);
    pendingEffects.splice(0).forEach((effect) => effect());
    return result;
  };
  render();
  return { props, requests, render, unmount: () => effects.forEach((effect) => effect?.cleanup?.()) };
}

test("initial Japanese setup replaces Rachel with Shizuka", async () => {
  const hook = mount();
  assert.equal(hook.requests.length, 1);
  assert.equal(hook.requests[0].language, "ja");
  hook.requests[0].resolve(japanese);
  await flush();
  assert.equal(hook.props.ttsVoice, "Shizuka");
  assert.deepEqual(hook.render().ttsVoices, japanese);
});

test("keeps a valid saved or URL-selected Japanese voice", async () => {
  const hook = mount({ ttsVoice: "Rin", urlVoice: "Rin" });
  hook.requests[0].resolve(japanese);
  await flush();
  assert.equal(hook.props.ttsVoice, "Rin");
});

test("language changes ignore the previous language's late response", async () => {
  const hook = mount({ sttLanguage: "en" });
  hook.render({ sttLanguage: "ja" });
  hook.requests[1].resolve(japanese);
  await flush();
  hook.requests[0].resolve(["Rachel"]);
  await flush();
  assert.equal(hook.props.ttsVoice, "Shizuka");
  assert.deepEqual(hook.render().ttsVoices, japanese);
});

test("late responses cannot overwrite a newer user or config selection", async () => {
  const hook = mount();
  hook.render({ ttsVoice: "Hinata" });
  hook.requests[0].resolve(japanese);
  await flush();
  assert.equal(hook.props.ttsVoice, "Hinata");
  hook.requests[1].resolve(japanese);
  await flush();
  assert.equal(hook.props.ttsVoice, "Hinata");
});

test("provider changes select a voice from the new provider", async () => {
  const hook = mount();
  hook.render({ ttsProvider: "openai" });
  hook.requests[1].resolve(["alloy", "nova"]);
  await flush();
  hook.requests[0].resolve(japanese);
  await flush();
  assert.equal(hook.props.ttsVoice, "alloy");
});

test("empty, failed and unmounted requests preserve the selected voice", async () => {
  for (const result of ["empty", "error", "unmounted"]) {
    const hook = mount({ ttsVoice: "Rin" });
    if (result === "error") hook.requests[0].reject(new Error("HAL unavailable"));
    else {
      if (result === "unmounted") hook.unmount();
      hook.requests[0].resolve(result === "empty" ? [] : ["Shizuka"]);
    }
    await flush();
    assert.equal(hook.props.ttsVoice, "Rin");
  }
});
