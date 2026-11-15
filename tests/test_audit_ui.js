// Run with: node tests/test_audit_ui.js (no browser or npm dependencies).
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../addon/src/openvpn_addon/webui.py"), "utf8");
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
function element() {
  return {
    children: [], textContent: "", disabled: false,
    appendChild(child) { this.children.push(child); },
    replaceChildren() { this.children = []; },
  };
}
const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, element());
  return elements.get(id);
};
let click;
let history = {enabled: false, events: [], next_before: null};
let fail = false;
const requests = [];
const context = vm.createContext({
  document: {
    getElementById: get,
    createElement: element,
    addEventListener(type, listener) { if (type === "click") click = listener; },
  },
  fetch: async (url) => {
    requests.push(url);
    if (url === "api/status") {
      return {ok: true, text: async () => JSON.stringify({
        summary: {}, health: {status: "ok", checks: []}, routes: [], crl: {}, clients: [],
      })};
    }
    return {ok: !fail, text: async () => JSON.stringify(fail ? {message: "unavailable"} : history)};
  },
});
const settle = () => new Promise((resolve) => setImmediate(resolve));
const press = async (id) => {
  await click({target: {closest: () => ({id, dataset: {}})}});
  await settle();
};

(async () => {
  vm.runInContext(script, context);
  await settle();
  assert.match(get("auditstate").textContent, /Disabled/);
  assert.equal(get("auditolder").disabled, true);

  history = {enabled: true, events: [{
    id: 2, timestamp: "2026-11-15T12:00:00+00:00", event: "profile_download",
    client: "<img src=x onerror=alert(1)>",
  }], next_before: 2};
  await press("auditrefresh");
  const row = get("audit").children[0];
  assert.equal(row.children[1].textContent, "profile download");
  assert.equal(row.children[2].textContent, history.events[0].client);
  assert.equal(row.children[2].innerHTML, undefined);
  assert.equal(get("auditolder").disabled, false);

  history = {enabled: true, events: [], next_before: null};
  await press("auditolder");
  assert.equal(requests.at(-1), "api/audit?before=2");
  assert.equal(get("audit").children.length, 0);
  assert.equal(get("auditolder").disabled, true);

  fail = true;
  await press("auditrefresh");
  assert.match(get("auditstate").textContent, /unavailable/);
  assert.equal(get("auditolder").disabled, true);
  fail = false;
  await press("auditrefresh");
  assert.match(get("auditstate").textContent, /No recorded events/);
  assert(requests.every((url) => url.startsWith("api/")));
  console.log("Audit UI: disabled state, safe rendering, pagination, errors and recovery passed.");
})().catch((error) => { console.error(error); process.exitCode = 1; });
