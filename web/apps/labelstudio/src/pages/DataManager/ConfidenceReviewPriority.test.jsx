import { TextEncoder } from "node:util";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { fieldDigest } from "./fieldEvidence";
import {
  confidenceAlgorithm,
  confidenceKind,
  confidenceSchema,
  confidenceSummary,
  confidenceText,
  validConfidenceAck,
  validConfidencePreview,
} from "./confidenceReviewPriority";
import { validBatchCapability, validBatchDetail, validBatchList } from "./reviewBatchResponses";
import { ReviewBatches } from "./ReviewBatches";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
jest.mock("./ReviewerWorkspace", () => ({ ReviewerWorkspace: () => null }));
global.TextEncoder = TextEncoder;
const hash = "a".repeat(64);
const ownerHash = "b".repeat(64);
const profile = {
  schema: "review-confidence-profile/v1",
  profile_id: "30000000-0000-4000-8000-000000000003",
  label: "Synthetic 修订",
  version: "v1",
  evaluator_id: "fixture",
  evaluator_version: "1",
  model_id: "no-model-called",
  model_artifact_sha256: hash,
  score_definition: "revision-confidence-lower-uncertain/v1",
  calibration_version: "1",
  integer_rule: "millionths-int/v1",
  input_contract: "static-text-choices-submission/v1",
};
const choice = { kind: confidenceKind, profile_id: profile.profile_id, profile_digest: hash, threshold_units: 400000 };
const candidates = [1, 2].map((id) => ({
  submission_id: id,
  assignment_id: id,
  task_id: id,
  revision: 1,
  submitter_id: id + 10,
  result_hash: hash,
  selected: id === 1,
}));
const evidence = candidates.map((c, i) => ({
  submission_id: c.submission_id,
  state: i === 0 ? "low_confidence" : "not_low_confidence",
  reason: i === 0 ? "frozen_low_confidence" : "frozen_not_low_confidence",
  score_units: i === 0 ? 100000 : 500000,
  fact_id: `40000000-0000-4000-8000-00000000000${i + 1}`,
  fact_hash: hash,
}));
const perPass = {
  snapshot_stored_bytes: 1024,
  snapshot_canonical_bytes: 1024,
  metadata_stored_bytes: 1024,
  metadata_canonical_bytes: 1024,
  candidate_count: 2,
  fact_count: 2,
};
const processingCost = {
  materialization_passes: 3,
  sorting_passes: 2,
  per_pass: perPass,
  total: Object.fromEntries(Object.entries(perPass).map(([k, v]) => [k, v * 3])),
  scorer_calls: 0,
};
const source = {
  schema: confidenceSchema,
  kind: confidenceKind,
  profile,
  profile_digest: hash,
  threshold_units: 400000,
};
const preview = {
  schema: confidenceSchema,
  kind: confidenceKind,
  algorithm: confidenceAlgorithm,
  project_id: 4,
  actor_id: 1,
  owner_hash: ownerHash,
  viewer_hash: hash,
  processing_cost: processingCost,
  plan_hash: hash,
  plan: {
    algorithm: confidenceAlgorithm,
    project_id: 4,
    rule: { rate: 50, per_submitter: false, seed: "review-v1", package_ids: [], submitter_ids: [] },
    candidates,
    priority: { ...source, evidence, summary: confidenceSummary(candidates, evidence) },
  },
};
const detail = {
  id: 9,
  name: "Frozen confidence",
  role: "manager",
  viewer_hash: hash,
  plan_hash: hash,
  algorithm: confidenceAlgorithm,
  as_of: "2026-11-01T00:00:00Z",
  rule: { rate: 50, seed: "review-v1" },
  candidate_count: 2,
  selected_count: 1,
  priority_source: source,
  priority_summary: preview.plan.priority.summary,
  items: candidates.map((c, i) => ({
    ...c,
    status: "pending",
    can_review: false,
    priority_reason: evidence[i].reason,
    confidence_score_units: evidence[i].score_units,
  })),
};
let viewer;
let owner;
let profiles;
let brokenReads;
let createResult;
let previewResult;
const cap = () => ({
  enabled: true,
  can_create: true,
  can_prioritize: false,
  can_read_confidence_priority: true,
  can_prioritize_confidence: profiles.length > 0,
  can_recover_confidence: true,
  confidence_owner_hash: owner,
  confidence_profiles: profiles,
  viewer_hash: viewer,
});
const ack = (args) => ({
  schema: confidenceSchema,
  kind: confidenceKind,
  algorithm: confidenceAlgorithm,
  id: 9,
  project_id: 4,
  actor_id: 1,
  owner_hash: owner,
  viewer_hash: viewer,
  plan_hash: hash,
  request_hash: fieldDigest(args.body),
  idempotency_key: args.headers["Idempotency-Key"],
  replayed: true,
  processing_cost: { materialization_passes: 0, sorting_passes: 0, per_pass: {}, total: {}, scorer_calls: 0 },
});
const implementation = (method, args) => {
  if (method === "reviewBatchCapability") return Promise.resolve(cap());
  if (method === "reviewBatches") return Promise.resolve(brokenReads ? { error: true } : { results: { batches: [] } });
  if (method === "reviewBatchDetail")
    return Promise.resolve(brokenReads ? { error: true } : { ...detail, viewer_hash: viewer });
  if (method === "reviewBatchPreview")
    return typeof previewResult === "function" ? previewResult(args) : Promise.resolve(previewResult);
  if (method === "reviewBatchCreate")
    return typeof createResult === "function" ? createResult(args) : Promise.resolve(createResult);
  return Promise.resolve({ error: true });
};
beforeEach(() => {
  viewer = hash;
  owner = ownerHash;
  profiles = [{ ...profile, digest: hash }];
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
  fireEvent.click(await screen.findByLabelText(zh ? "优先抽审低置信度修订" : "Prioritize low confidence revisions"));
  fireEvent.change(screen.getByLabelText(zh ? "置信来源与版本" : "Confidence source and version"), {
    target: { value: `${profile.profile_id}:${hash}` },
  });
};
const choose = async (zh = false) => {
  await select(zh);
  fireEvent.change(screen.getByLabelText(zh ? "审核批次名称" : "Batch name"), { target: { value: "Confidence" } });
  fireEvent.change(screen.getByLabelText(zh ? "抽样比例（1–100）" : "Sample percentage (1–100)"), {
    target: { value: "50" },
  });
  fireEvent.click(screen.getByRole("button", { name: zh ? "预览抽样" : "Preview sampling" }));
  await screen.findByTestId("review-batch-preview");
};
const writes = () => mockCallApi.mock.calls.filter(([method]) => method === "reviewBatchCreate");
const clone = (value) => JSON.parse(JSON.stringify(value));

it("accepts the scoped typed DTO without hashing redacted public proof as the internal plan", () => {
  expect(validConfidencePreview(preview, choice, 4)).toBe(true);
  expect(fieldDigest(preview.plan)).not.toBe(preview.plan_hash);
  expect(validBatchDetail(detail, 9)).toBe(true);
});
it.each(["\u0085", "\u001c", "\u001d", "\u001e", "\u001f", "\u3000"])(
  "matches DRF Unicode stripping for %j",
  (space) => {
    expect(confidenceText(`${space}Confidence${space}`)).toBe("Confidence");
    expect(confidenceText(`${space}review-v1${space}`)).toBe("review-v1");
  },
);
it("preserves FEFF, which Python CharField does not strip", () => {
  expect(confidenceText("\ufeffConfidence\ufeff")).toBe("\ufeffConfidence\ufeff");
});
it.each([
  "profile",
  "score",
  "threshold",
  "actor",
  "scope",
  "cost",
  "passes",
  "order",
  "selection",
  "reason",
  "summary",
  "private",
])("rejects inconsistent confidence preview %s", (kind) => {
  const v = clone(preview);
  const p = v.plan.priority;
  if (kind === "profile") p.profile_digest = "c".repeat(64);
  if (kind === "score") p.evidence[0].score_units = true;
  if (kind === "threshold") p.threshold_units = 100000;
  if (kind === "actor") v.actor_id = true;
  if (kind === "scope") v.project_id = 5;
  if (kind === "cost") v.processing_cost.total.candidate_count = 5;
  if (kind === "passes") v.processing_cost.sorting_passes = 3;
  if (kind === "order") v.plan.candidates.reverse();
  if (kind === "selection")
    v.plan.candidates.forEach((c) => {
      c.selected = !c.selected;
    });
  if (kind === "reason") p.evidence[0].reason = "model_guaranteed";
  if (kind === "summary") p.summary.known_candidates = 5;
  if (kind === "private") p.profile.producer_namespace = profile.profile_id;
  expect(validConfidencePreview(v, choice, 4)).toBe(false);
});
it.each([
  "schema",
  "kind",
  "algorithm",
  "actor_id",
  "project_id",
  "owner_hash",
  "viewer_hash",
  "request_hash",
  "plan_hash",
  "idempotency_key",
])("does not acknowledge a missing or changed %s", (key) => {
  const args = { body: { expected_plan_hash: hash, priority: choice }, headers: { "Idempotency-Key": "key" } };
  const intent = { body: args.body, key: "key", ownerHash, viewerHash: hash, actorId: 1, processingCost };
  expect(validConfidenceAck(ack(args), intent, 4)).toBe(true);
  const r = ack(args);
  delete r[key];
  expect(validConfidenceAck(r, intent, 4)).toBe(false);
});
it.each(["can_prioritize_confidence", "can_read_confidence_priority", "can_recover_confidence"])(
  "rejects malformed capability %s",
  (key) => {
    expect(validBatchCapability({ ...cap(), [key]: "true" })).toBe(false);
  },
);
it("rejects partial or unknown confidence detail and reviewer score exposure", () => {
  expect(validBatchDetail({ ...detail, algorithm: "revision-confidence-ranked/v2" }, 9)).toBe(false);
  expect(validBatchDetail({ ...detail, algorithm: "sha256-ranked-v1" }, 9)).toBe(false);
  expect(validBatchDetail({ ...detail, role: "reviewer", priority_source: undefined }, 9)).toBe(false);
});
it.each(["summary", "score", "unknownScore", "selected"])("rejects contradictory detail %s", (kind) => {
  const r = clone(detail);
  if (kind === "summary") r.priority_summary.selected_low_confidence = 0;
  if (kind === "score") r.items[0].confidence_score_units = 800000;
  if (kind === "unknownScore") r.items[0].priority_reason = "not_covered";
  if (kind === "selected") r.items[0].selected = false;
  expect(validBatchDetail(r, 9)).toBe(false);
});
it("binds confidence list identity and rejects either crossed list/capability order", () => {
  const list = {
    schema: confidenceSchema,
    kind: confidenceKind,
    project_id: 4,
    viewer_hash: hash,
    results: { batches: [{ id: 9, name: "Frozen" }] },
  };
  expect(validBatchList(list, 4, cap())).toBe(true);
  expect(validBatchList({ ...list, viewer_hash: "c".repeat(64) }, 4, cap())).toBe(false);
  expect(validBatchList(list, 4, { ...cap(), viewer_hash: "c".repeat(64) })).toBe(false);
  expect(validBatchList({ ...list, schema: "review-confidence-priority/v2" }, 4, cap())).toBe(false);
});
it.each(["en-US", "zh-CN"])("reuses the complete workflow and recovers the same unknown key in %s", async (locale) => {
  const zh = locale === "zh-CN";
  const view = mount(locale);
  await choose(zh);
  fireEvent.click(screen.getByRole("button", { name: zh ? "创建／重试" : "Create / retry" }));
  await screen.findByTestId("review-priority-recover");
  profiles = [];
  viewer = "c".repeat(64);
  createResult = (args) => Promise.resolve(ack(args));
  fireEvent.click(screen.getByTestId("review-priority-recover"));
  await screen.findByRole("heading", { name: "Frozen confidence" });
  expect(writes()).toHaveLength(2);
  expect(writes()[0][1].body).toEqual(writes()[1][1].body);
  expect(writes()[0][1].headers).toEqual(writes()[1][1].headers);
  expect(mockCallApi.mock.calls.filter(([m]) => m === "reviewBatchPreview")).toHaveLength(1);
  expect(view.container.textContent).toContain(zh ? "冻结来源" : "Frozen source");
  expect(mockCallApi.mock.calls.some(([m]) => /classificationBatches|fieldBatches|nerBatches/.test(m))).toBe(false);
  view.finish();
});
it("clears an unknown command on owner change even when list reads fail", async () => {
  const view = mount();
  await choose();
  fireEvent.click(screen.getByRole("button", { name: "Create / retry" }));
  await screen.findByTestId("review-priority-recover");
  owner = "d".repeat(64);
  brokenReads = true;
  fireEvent.click(screen.getByRole("button", { name: "Refresh batch" }));
  await waitFor(() => expect(screen.queryByTestId("review-priority-recover")).toBeNull());
  expect(writes()).toHaveLength(1);
  view.finish();
});
it("invalidates a pending preview on a form edit", async () => {
  let resolve;
  previewResult = () =>
    new Promise((done) => {
      resolve = done;
    });
  const view = mount();
  await select();
  fireEvent.click(screen.getByRole("button", { name: "Preview sampling" }));
  await waitFor(() => expect(resolve).toBeDefined());
  fireEvent.change(screen.getByLabelText("Batch name"), { target: { value: "edited" } });
  await act(async () => {
    resolve(preview);
  });
  expect(screen.queryByTestId("review-batch-preview")).toBeNull();
  expect(writes()).toHaveLength(0);
  view.finish();
});
it("keeps confirmed creation distinct from a failed detail read", async () => {
  createResult = (args) => {
    brokenReads = true;
    return Promise.resolve(ack(args));
  };
  const view = mount();
  await choose();
  fireEvent.click(screen.getByRole("button", { name: "Create / retry" }));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("review-priority-recover")).toBeNull();
  expect(writes()).toHaveLength(1);
  expect(view.container.textContent).toContain("9");
  view.finish();
});
