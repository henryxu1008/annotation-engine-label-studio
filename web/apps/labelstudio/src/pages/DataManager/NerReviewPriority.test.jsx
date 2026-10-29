import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ReviewBatches } from "./ReviewBatches";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
jest.mock("./ReviewerWorkspace", () => ({ ReviewerWorkspace: () => null }));
const hash = "a".repeat(64);
const person = { principal: "user:1", source: "session", actor_id: 1 };
const candidates = [1, 2].map((id) => ({
  submission_id: id,
  assignment_id: id,
  task_id: 7,
  revision: 1,
  submitter_id: id + 10,
  result_hash: hash,
  selected: id === 1,
}));
const preview = {
  viewer_hash: hash,
  plan_hash: hash,
  plan: {
    algorithm: "ner-conflict-ranked/v1",
    candidates,
    priority: {
      kind: "ner_conflict",
      schema: "review-ner-priority/v1",
      batch_id: 5,
      manifest_hash: hash,
      metric_rule: "ner-strict-entities/v1",
      selection: "active-assignment-latest/v1",
      explanation: "ner-overlap-explanation/v1",
      engine: "nervaluate/0.2.0/strict-correct-counter",
      creator: person,
      evidence: candidates.map((c) => ({
        submission_id: c.submission_id,
        state: "conflict",
        reason: "frozen_conflict",
        calculation_hash: hash,
      })),
    },
  },
};
const detail = {
  id: 9,
  name: "NER sample",
  role: "manager",
  viewer_hash: hash,
  plan_hash: hash,
  algorithm: "ner-conflict-ranked/v1",
  as_of: "2026-10-29T00:00:00Z",
  candidate_count: 2,
  selected_count: 1,
  rule: { rate: 50, seed: "fixed" },
  priority_summary: { conflict_candidates: 2, selected_conflicts: 1, unknown_candidates: 0 },
  priority_source: { schema: "review-ner-priority/v1", batch_id: 5, manifest_hash: hash },
  items: candidates.map((c) => ({ ...c, status: "pending", can_review: false, priority_reason: "frozen_conflict" })),
};
let viewer;
let brokenReads;
let createResult;
let previewResult;
const implementation = (method, args) => {
  if (method === "reviewBatchCapability")
    return Promise.resolve({
      enabled: true,
      can_create: true,
      can_prioritize: false,
      can_prioritize_ner: true,
      can_read_ner_priority: true,
      can_read_handoffs: true,
      viewer_hash: viewer,
    });
  if (method === "reviewBatches") return Promise.resolve(brokenReads ? { error: true } : { results: { batches: [] } });
  if (method === "reviewBatchDetail")
    return Promise.resolve(brokenReads ? { error: true } : { ...detail, viewer_hash: viewer });
  if (method === "nerBatches")
    return Promise.resolve({
      results: [{ id: 5, name: "Frozen NER", manifest_hash: hash }],
      project_id: 4,
      viewer: "b".repeat(64),
      next: null,
    });
  if (method === "reviewBatchPreview")
    return typeof previewResult === "function" ? previewResult(args) : Promise.resolve(previewResult);
  if (method === "reviewBatchCreate")
    return typeof createResult === "function" ? createResult(args) : Promise.resolve(createResult);
  return Promise.resolve({ error: true });
};
beforeEach(() => {
  viewer = hash;
  brokenReads = false;
  createResult = { error: true };
  previewResult = preview;
  mockCallApi.mockReset();
  mockCallApi.mockImplementation(implementation);
});
const mount = (locale = "en-US") => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const view = render(
    <Provider>
      <ReviewBatches projectId={4} />
    </Provider>,
  );
  return {
    ...view,
    finish: () => {
      view.unmount();
      runtime.destroy();
    },
  };
};
const select = async (zh = false) => {
  fireEvent.click(await screen.findByLabelText(zh ? "优先抽审冻结 NER 分歧" : "Prioritize frozen NER conflicts"));
  await screen.findByRole("option", { name: "#5 · Frozen NER" });
  fireEvent.change(screen.getByLabelText(zh ? "冻结 NER 批次" : "Frozen NER batch"), { target: { value: "5" } });
};
const choose = async (zh = false) => {
  await select(zh);
  fireEvent.click(screen.getByRole("button", { name: zh ? "预览抽样" : "Preview sampling" }));
  await screen.findByTestId("review-batch-preview");
};
const writes = () => mockCallApi.mock.calls.filter(([method]) => method === "reviewBatchCreate");

it.each(["en-US", "zh-CN"])("reuses sampling, original-key recovery and NER provenance in %s", async (locale) => {
  const zh = locale === "zh-CN";
  const view = mount(locale);
  await choose(zh);
  fireEvent.click(screen.getByRole("button", { name: zh ? "创建／重试" : "Create / retry" }));
  await screen.findByTestId("review-priority-recover");
  brokenReads = true;
  fireEvent.click(screen.getByRole("button", { name: zh ? "刷新批次" : "Refresh batch" }));
  await screen.findByRole("alert");
  expect(writes()).toHaveLength(1);
  brokenReads = false;
  createResult = (args) =>
    Promise.resolve({ id: 9, plan_hash: hash, idempotency_key: args.headers["Idempotency-Key"], replayed: true });
  fireEvent.click(screen.getByTestId("review-priority-recover"));
  await screen.findByRole("heading", { name: "NER sample" });
  expect(writes()).toHaveLength(2);
  expect(writes()[0][1].body).toEqual(writes()[1][1].body);
  expect(writes()[0][1].headers).toEqual(writes()[1][1].headers);
  expect(mockCallApi.mock.calls.filter(([m]) => m === "reviewBatchPreview")).toHaveLength(1);
  expect(writes()[0][1].body.priority.kind).toBe("ner_conflict");
  expect(mockCallApi.mock.calls.some(([m]) => m === "classificationBatches")).toBe(false);
  expect(view.container.querySelector("[data-handoff-source]")).toBeNull();
  expect(view.container.textContent).toContain(zh ? "冻结 NER 依据批次 #5" : "Frozen NER evidence batch #5");
  view.finish();
});

it("drops an old unknown command on a changed viewer even when another read fails", async () => {
  const view = mount();
  await choose();
  fireEvent.click(screen.getByRole("button", { name: "Create / retry" }));
  await screen.findByTestId("review-priority-recover");
  viewer = "c".repeat(64);
  brokenReads = true;
  fireEvent.click(screen.getByRole("button", { name: "Refresh batch" }));
  await waitFor(() => expect(screen.queryByTestId("review-priority-recover")).toBeNull());
  expect(writes()).toHaveLength(1);
  expect(screen.queryByTestId("review-batch-preview")).toBeNull();
  view.finish();
});

it("checks current identity before a new POST", async () => {
  const view = mount();
  await choose();
  viewer = "c".repeat(64);
  fireEvent.click(screen.getByRole("button", { name: "Create / retry" }));
  await screen.findByRole("alert");
  expect(writes()).toHaveLength(0);
  expect(screen.queryByTestId("review-priority-recover")).toBeNull();
  view.finish();
});

it("rejects a late preview from another identity", async () => {
  let resolve;
  previewResult = () =>
    new Promise((done) => {
      resolve = done;
    });
  const view = mount();
  await select();
  fireEvent.click(screen.getByRole("button", { name: "Preview sampling" }));
  await waitFor(() => expect(resolve).toBeDefined());
  viewer = "c".repeat(64);
  await act(async () => {
    resolve(preview);
  });
  await screen.findByRole("alert");
  expect(screen.queryByTestId("review-batch-preview")).toBeNull();
  expect(writes()).toHaveLength(0);
  view.finish();
});

it.each(["schema", "rule", "source"])(
  "rejects a mixed NER %s instead of falling back to classification",
  async (what) => {
    const value = JSON.parse(JSON.stringify(preview));
    if (what === "schema") value.plan.priority.schema = "review-classification-priority/v1";
    if (what === "rule") value.plan.priority.metric_rule = "classification-exact-sets/v1";
    if (what === "source") value.plan.priority.manifest_hash = "c".repeat(64);
    previewResult = value;
    const view = mount();
    await select();
    fireEvent.click(screen.getByRole("button", { name: "Preview sampling" }));
    await screen.findByRole("alert");
    expect(screen.queryByTestId("review-batch-preview")).toBeNull();
    expect(mockCallApi.mock.calls.some(([m]) => m === "classificationBatches")).toBe(false);
    view.finish();
  },
);

it("rejects foreign NER metadata and never transfers the selected ID", async () => {
  mockCallApi.mockImplementation((method, args) =>
    method === "nerBatches"
      ? Promise.resolve({ results: [{ id: 5, name: "Foreign", manifest_hash: hash }], project_id: 999, viewer: hash })
      : implementation(method, args),
  );
  const view = mount();
  fireEvent.click(await screen.findByLabelText("Prioritize frozen NER conflicts"));
  await screen.findByRole("alert");
  expect(screen.queryByRole("option", { name: "#5 · Foreign" })).toBeNull();
  expect(screen.getByRole("button", { name: "Preview sampling" }).disabled).toBe(true);
  view.finish();
});

it("preserves a matched save acknowledgement after same-identity read failure", async () => {
  createResult = (args) => {
    brokenReads = true;
    return Promise.resolve({ id: 9, plan_hash: hash, idempotency_key: args.headers["Idempotency-Key"] });
  };
  const view = mount();
  await choose();
  fireEvent.click(screen.getByRole("button", { name: "Create / retry" }));
  await waitFor(() => expect(view.container.textContent).toContain("9"));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("review-priority-recover")).toBeNull();
  expect(writes()).toHaveLength(1);
  viewer = "c".repeat(64);
  fireEvent.click(screen.getByRole("button", { name: "Refresh batch" }));
  await waitFor(() => expect(view.container.textContent).not.toContain("Batch #9"));
  expect(writes()).toHaveLength(1);
  view.finish();
});

it("ignores a late NER list after the evidence type changes", async () => {
  let resolveNer;
  mockCallApi.mockImplementation((method, args) => {
    if (method === "reviewBatchCapability")
      return Promise.resolve({
        enabled: true,
        can_create: true,
        can_prioritize: true,
        can_prioritize_ner: true,
        viewer_hash: hash,
      });
    if (method === "classificationBatches") return Promise.resolve({ results: [] });
    if (method === "nerBatches")
      return new Promise((resolve) => {
        resolveNer = resolve;
      });
    return implementation(method, args);
  });
  const view = mount();
  fireEvent.click(await screen.findByLabelText("Prioritize frozen classification conflicts"));
  fireEvent.change(screen.getByLabelText("Conflict evidence type"), { target: { value: "ner_conflict" } });
  await waitFor(() => expect(resolveNer).toBeDefined());
  fireEvent.change(screen.getByLabelText("Conflict evidence type"), { target: { value: "classification_conflict" } });
  await act(async () =>
    resolveNer({ results: [{ id: 5, name: "Late NER", manifest_hash: hash }], project_id: 4, viewer: hash }),
  );
  expect(screen.queryByRole("option", { name: "#5 · Late NER" })).toBeNull();
  expect(screen.getByRole("button", { name: "Preview sampling" }).disabled).toBe(true);
  view.finish();
});
