// Run with: node --test tests/test_activity_ui.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

const page = fs.readFileSync(path.join(__dirname, "../addon/src/openvpn_addon/webui.py"), "utf8");
const script = page.match(/<script>([\s\S]*?)<\/script>/)[1];

function panel(clients) {
  const elements = {};
  const context = vm.createContext({
    document: {
      getElementById: (id) => elements[id] ||= {},
      addEventListener: () => {},
    },
    fetch: async () => ({
      ok: true,
      text: async () => JSON.stringify({
        summary: {}, health: {checks: [], status: "ok"}, routes: [], crl: {}, clients,
      }),
    }),
  });
  vm.runInContext(script, context);
  return {context, elements};
}

test("last-seen labels distinguish unknown, sub-day, singular and plural ages", () => {
  const {context} = panel([]);
  for (const [days, expected] of [
    [undefined, "unknown"], [null, "unknown"], [false, "unknown"], [-1, "unknown"],
    ["<img src=x>", "unknown"], [1.5, "unknown"],
    [0, "less than 1 day ago"], [1, "1 day ago"], [34, "34 days ago"],
  ]) {
    context.days = days;
    assert.equal(vm.runInContext("lastSeenLabel(days)", context), expected);
  }
});

test("actual panel render includes age for offline and revoked clients", async () => {
  const {context, elements} = panel([
    {name: "alice", status: "valid", online: false, inactive_days: 34},
    {name: "bob", status: "revoked", online: false, inactive_days: 1},
    {name: "carol", status: "valid", online: true, inactive_days: 0},
    {name: "dave", status: "valid", online: false, inactive_days: null},
  ]);
  await vm.runInContext("load()", context);
  for (const label of ["34 days ago", "1 day ago", "less than 1 day ago", "unknown"]) {
    assert.ok(elements.clients.innerHTML.includes(`<td>${label}</td>`));
  }
  assert.match(page, /<th>Last seen<\/th>/);
});

test("empty table spans the new column", async () => {
  const {context, elements} = panel([]);
  await vm.runInContext("load()", context);
  assert.match(elements.clients.innerHTML, /colspan="6"/);
});
