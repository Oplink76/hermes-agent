"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const bundlePath = process.argv[2];
if (!bundlePath) throw new Error("dashboard bundle path is required");

class CapturedFormData {
  constructor() { this.values = new Map(); }
  append(name, value) { this.values.set(name, value); }
  get(name) { return this.values.get(name); }
}

const requests = [];
const testHook = {};
const noopComponent = function () {};
const sandbox = {
  console,
  FormData: CapturedFormData,
  URLSearchParams,
  setTimeout,
  clearTimeout,
  window: {
    __HERMES_KANBAN_TEST_HOOK__: testHook,
    __HERMES_PLUGINS__: { register: function () {} },
    __HERMES_PLUGIN_SDK__: {
      React: {
        Component: class {},
        createElement: function () { return null; },
      },
      components: {
        Card: noopComponent,
        CardContent: noopComponent,
        Badge: noopComponent,
        Button: noopComponent,
        Input: noopComponent,
        Label: noopComponent,
        Select: noopComponent,
        SelectOption: noopComponent,
      },
      hooks: {},
      utils: { cn: function () {}, timeAgo: function () {} },
      fetchJSON: function (url, options) {
        requests.push({ transport: "json", url, options });
        return Promise.resolve({});
      },
      authedFetch: function (url, options) {
        requests.push({ transport: "multipart", url, options });
        return Promise.resolve({ ok: true });
      },
    },
  },
};

vm.runInNewContext(fs.readFileSync(bundlePath, "utf8"), sandbox, {
  filename: bundlePath,
});

const task = {
  id: "t_contract",
  status: "review",
  title: "Current title",
  assignee: null,
  current_step_key: "review",
  current_run_id: 42,
};
const expectedSnapshot = {
  expected_status: "review",
  expected_title: "Current title",
  expected_assignee: null,
  expected_current_step_key: "review",
  expected_current_run_id: 42,
};
const jsonMutationCases = [
  ["PATCH", "/tasks/t_contract", { title: "Edited" }],
  ["DELETE", "/tasks/t_contract", {}],
  ["POST", "/tasks/t_contract/comments", { body: "Note" }],
  ["POST", "/tasks/t_contract/reclaim", { reason: "Operator" }],
  ["POST", "/tasks/t_contract/reassign", { profile: "developer" }],
  ["POST", "/tasks/t_contract/specify", {}],
  ["POST", "/tasks/t_contract/decompose", {}],
  ["POST", "/links", { parent_id: "t_parent", child_id: task.id, expected_task_id: task.id }],
  ["DELETE", "/links?parent_id=t_parent&child_id=t_contract", { expected_task_id: task.id }],
  ["DELETE", "/attachments/7", {}],
  ["POST", "/tasks/t_contract/home-subscribe/telegram", {}],
  ["DELETE", "/tasks/t_contract/home-subscribe/telegram", {}],
];

async function main() {
  assert.equal(typeof testHook.sendTaskMutation, "function");
  assert.equal(typeof testHook.sendBulkMutation, "function");
  assert.equal(typeof testHook.appendExpectedTaskSnapshot, "function");

  for (const [method, url, payload] of jsonMutationCases) {
    requests.length = 0;
    await testHook.sendTaskMutation(task, url, method, {
      expected_title: "caller must not override current state",
      ...payload,
    });
    assert.equal(requests.length, 1, `${method} ${url} request count`);
    const captured = requests[0];
    assert.equal(captured.transport, "json", `${method} ${url} transport`);
    assert.equal(captured.options.method, method, `${method} ${url} method`);
    assert.deepEqual(
      JSON.parse(captured.options.body),
      { ...payload, ...expectedSnapshot },
      `${method} ${url} body`,
    );
  }

  const second = {
    id: "t_second",
    status: "ready",
    title: "Second",
    assignee: "developer",
    current_step_key: null,
    current_run_id: null,
  };
  requests.length = 0;
  await testHook.sendBulkMutation(
    [task, second],
    "/tasks/bulk",
    { ids: [task.id, second.id], priority: 9 },
  );
  const bulkBody = JSON.parse(requests[0].options.body);
  assert.deepEqual(bulkBody.expected_snapshots[task.id], expectedSnapshot);
  assert.deepEqual(bulkBody.expected_snapshots[second.id], {
    expected_status: "ready",
    expected_title: "Second",
    expected_assignee: "developer",
    expected_current_step_key: null,
    expected_current_run_id: null,
  });

  const form = new CapturedFormData();
  testHook.appendExpectedTaskSnapshot(form, task);
  assert.deepEqual(JSON.parse(form.get("expected_snapshot")), expectedSnapshot);
}

main().catch(function (error) {
  console.error(error.stack || error);
  process.exitCode = 1;
});
