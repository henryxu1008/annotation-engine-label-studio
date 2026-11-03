import { TextEncoder } from "node:util";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { SecondReviewWorkspace } from "./SecondReviewWorkspace";
import { capabilitySchema, sourceSchema, taskSchema } from "./secondReviewResponses";
import { fieldDigest } from "./fieldEvidence";

global.TextEncoder = TextEncoder;
const mockCall = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCall }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return {
    Button: ({ children, waiting, look, size, variant, ...props }) => React.createElement("button", props, children),
    Typography: ({ children }) => React.createElement("span", {}, children),
  };
});
const hash = "a".repeat(64);
const ownerHash = "b".repeat(64);
const sourceHash = "c".repeat(64);
const taskId = "10000000-0000-4000-8000-000000000001";
const instanceId = "20000000-0000-4000-8000-000000000002";
const time = "2026-11-02T00:00:00Z";
const firstProof = {
  schema: "first-review-proof/v1",
  id: 1,
  submission_id: 9,
  reviewer_id: 6,
  decision: "approved",
  reason: "Initial 初审",
  created_at: time,
};
const firstHash = fieldDigest(firstProof);
let cap;
let record;
let detail;
let post;
let sourceReads;
let captured;
let detailFailure;
const task = (projectId = 4) => ({
  id: taskId,
  project_id: projectId,
  batch_id: 8,
  submission_id: 9,
  revision: 1,
  assignee_id: 7,
  status: "pending",
  version: 1,
  effect: "supervisory_only",
  source_hash: sourceHash,
  first_review_hash: firstHash,
  created_at: time,
  source_integrity: "not_checked",
  decision: null,
  cancellation: null,
  viewer_hash: cap.viewer_hash,
  owner_hash: cap.owner_hash,
});
const source = (projectId = 4) => ({
  task: record,
  result_snapshot: {
    project: { id: projectId, label_config: "<View/>" },
    task: { id: 10, data: { text: "Frozen 静态 source" } },
    annotation: { result: [] },
  },
  first_review: { ...firstProof },
  subject: {
    submission_id: 9,
    assignment_id: 3,
    task_id: 10,
    revision: 1,
    assignee_id: 2,
    submitter_id: 2,
    result_hash: hash,
    task_data_hash: hash,
    label_config_sha256: hash,
  },
  historical_revision: false,
  source_health: "target_source_verified",
  metric_integrity: "not_checked",
  viewer_hash: cap.viewer_hash,
  owner_hash: cap.owner_hash,
  effect: "supervisory_only",
});
const preview = () => ({
  batch_id: 8,
  submission_id: 9,
  plan_hash: hash,
  source_hash: sourceHash,
  effect: "supervisory_only",
  viewer_hash: cap.viewer_hash,
  owner_hash: cap.owner_hash,
  eligible: true,
  existing: null,
  source_health: "target_source_verified",
  metric_integrity: "not_checked",
});
const ack = (method, options) => {
  const command = { secondReviewCreate: "dispatch", secondReviewDecide: "decision", secondReviewCancel: "cancel" }[
    method
  ];
  const receipt = {
    schema: "second-review-command-receipt/v1",
    command,
    task_id: taskId,
    project_id: 4,
    organization_id: 2,
    instance_id: instanceId,
    owner: { principal: "synthetic", source: "local", actor_id: 7 },
    key: options.headers["Idempotency-Key"],
    request_hash: fieldDigest(options.body),
    created_at: time,
    effect: "supervisory_only",
    source_hash: sourceHash,
    version: command === "dispatch" ? 1 : 2,
    ...(command !== "cancel" ? { first_review_hash: firstHash } : {}),
    ...(command === "dispatch"
      ? { batch_id: 8, submission_id: 9, revision: 1, assignee_id: 7 }
      : command === "decision"
        ? { decision_id: instanceId, outcome: options.body.outcome, reason: options.body.reason }
        : { reason: options.body.reason }),
  };
  return {
    receipt,
    replayed: captured.length > 1,
    source_integrity: "not_checked",
    viewer_hash: cap.viewer_hash,
    owner_hash: cap.owner_hash,
  };
};
const implementation = async (method, options = {}) => {
  if (method === "secondReviewCapability") return cap;
  if (method === "secondReviews") return { results: [record], count: 1, next: null, ...cap };
  if (method === "secondReviewDetail") {
    if (detailFailure) throw Error("read failed");
    return { ...record, viewer_hash: cap.viewer_hash, owner_hash: cap.owner_hash };
  }
  if (method === "secondReviewSource") {
    sourceReads++;
    return detail ?? source();
  }
  if (method === "secondReviewContext") return preview();
  if (method === "secondReviewAssignees")
    return { ...preview(), results: [{ id: 7, display_name: "独立 Independent" }], count: 1, next: null };
  if (method.startsWith("secondReviewC") || method === "secondReviewDecide") {
    captured.push({ method, ...options });
    return post ? post(method, options) : ack(method, options);
  }
  throw Error(method);
};
beforeEach(() => {
  cap = {
    effect: "supervisory_only",
    viewer_hash: hash,
    owner_hash: ownerHash,
    can_read: true,
    can_organize: false,
    can_decide: true,
    can_cancel: false,
  };
  record = task();
  detail = null;
  sourceReads = 0;
  captured = [];
  post = null;
  detailFailure = false;
  mockCall.mockReset();
  mockCall.mockImplementation(implementation);
});
const mount = (locale = "en-US", props = {}) => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const view = render(
    <Provider>
      <SecondReviewWorkspace projectId={4} {...props} />
    </Provider>,
  );
  return {
    ...view,
    runtime,
    Provider,
    finish: () => {
      view.unmount();
      runtime.destroy();
    },
  };
};
const openTask = async () => {
  fireEvent.click(await screen.findByText(/#9 \/ 1/));
  await screen.findByTestId("review-result-snapshot");
};

test.each(["en-US", "zh-CN"])("A01 A03 F07 independent confirmed/concern flow in %s", async (locale) => {
  const view = mount(locale);
  await openTask();
  expect(screen.getByText(locale === "zh-CN" ? /初审结论/ : /Initial review/)).toBeTruthy();
  expect(screen.getByText(locale === "zh-CN" ? /本轮独立意见不控制发布/ : /does not control release/)).toBeTruthy();
  fireEvent.change(screen.getByTestId("second-reason"), { target: { value: "复核 Concern" } });
  fireEvent.click(screen.getByTestId("second-concern"));
  await waitFor(() => expect(captured).toHaveLength(1));
  expect(captured[0].body.outcome).toBe("concern");
  expect(captured[0].body.reason).toBe("复核 Concern");
  await screen.findByText(locale === "zh-CN" ? /命令已保存；/ : /Command saved/);
  view.finish();
});

test("D01 D09 UNKNOWN refresh is read-only and salt rotation preserves original body/key", async () => {
  cap = { ...cap, can_organize: true, can_decide: false, can_cancel: true };
  post = (m, o) => (captured.length === 1 ? { error: true, $meta: { ok: false, status: 503 } } : ack(m, o));
  const view = mount("en-US", { initialSource: { batch: 8, source: 9 } });
  fireEvent.change(await screen.findByTestId("second-assignee"), { target: { value: "7" } });
  fireEvent.click(screen.getByTestId("second-dispatch"));
  const recover = await screen.findByTestId("second-recover");
  await waitFor(() => expect(document.activeElement).toBe(recover));
  const original = captured[0];
  cap = { ...cap, viewer_hash: "e".repeat(64) };
  fireEvent.click(screen.getByText("Refresh"));
  await screen.findByTestId("second-assignee");
  expect(captured).toHaveLength(1);
  fireEvent.click(screen.getByTestId("second-recover"));
  await screen.findByText(/Command saved/);
  expect(captured).toHaveLength(2);
  expect(captured[1].body).toEqual(original.body);
  expect(captured[1].headers).toEqual(original.headers);
  view.finish();
});

test("D09 changing actual owner clears UNKNOWN intent and old answers", async () => {
  post = () => ({ error: true });
  const view = mount();
  await openTask();
  fireEvent.click(screen.getByTestId("second-confirm"));
  await screen.findByTestId("second-recover");
  cap = { ...cap, owner_hash: "e".repeat(64), viewer_hash: "f".repeat(64) };
  fireEvent.click(screen.getByText("Refresh"));
  await waitFor(() => expect(screen.queryByTestId("second-recover")).toBeNull());
  expect(screen.queryByTestId("review-result-snapshot")).toBeNull();
  expect(captured).toHaveLength(1);
  view.finish();
});

test("D10 success followed by history failure remains saved and refresh never POSTS", async () => {
  post = (m, o) => {
    detailFailure = true;
    return ack(m, o);
  };
  const view = mount();
  await openTask();
  fireEvent.click(screen.getByTestId("second-confirm"));
  await screen.findByText(/command was saved, but/);
  expect(screen.queryByTestId("second-recover")).toBeNull();
  fireEvent.click(screen.getByText("Refresh"));
  await waitFor(() =>
    expect(mockCall.mock.calls.filter(([m]) => m === "secondReviewDetail").length).toBeGreaterThan(2),
  );
  expect(captured).toHaveLength(1);
  view.finish();
});

test("D10 invalid ACK is UNKNOWN; it does not clear the intent", async () => {
  post = (m, o) => ({ ...ack(m, o), receipt: { ...ack(m, o).receipt, task_id: instanceId } });
  const view = mount();
  await openTask();
  fireEvent.click(screen.getByTestId("second-confirm"));
  await screen.findByTestId("second-recover");
  expect(screen.queryByText(/Command saved/)).toBeNull();
  post = null;
  fireEvent.click(screen.getByTestId("second-recover"));
  await screen.findByText(/Command saved/);
  expect(captured[1].headers).toEqual(captured[0].headers);
  view.finish();
});

test("A10 B05 blur clears answers; source failures keep bounded metadata only", async () => {
  const view = mount();
  await openTask();
  fireEvent(window, new Event("blur"));
  expect(screen.queryByTestId("review-result-snapshot")).toBeNull();
  detail = { error: true };
  fireEvent.click(screen.getByText("Refresh"));
  await screen.findByText(/original source cannot currently be verified/);
  expect(screen.queryByTestId("second-confirm")).toBeNull();
  expect(screen.getByText(/History metadata/)).toBeTruthy();
  view.finish();
});

test.each(["project", "owner", "viewer", "subject", "version", "firstReview"])(
  "A10 source DTO rejects mismatched %s before caching answers",
  async (field) => {
    detail = source();
    if (field === "project") detail.result_snapshot.project.id = 99;
    if (field === "owner") detail.owner_hash = "e".repeat(64);
    if (field === "viewer") detail.viewer_hash = "e".repeat(64);
    if (field === "subject") detail.subject.submission_id = 99;
    if (field === "version") detail.task = { ...record, status: "cancelled", version: 2 };
    if (field === "firstReview") detail.first_review.reason = "forged reason";
    const view = mount();
    fireEvent.click(await screen.findByText(/#9 \/ 1/));
    await screen.findByText(/original source cannot currently be verified/);
    expect(screen.queryByTestId("review-result-snapshot")).toBeNull();
    view.finish();
  },
);

test("C10 strict DTOs reject fake effects and incoherent terminal opinions", () => {
  expect(capabilitySchema.safeParse({ ...cap, effect: "blocking" }).success).toBe(false);
  expect(taskSchema.safeParse({ ...record, status: "completed", version: 2 }).success).toBe(false);
  expect(sourceSchema.safeParse({ ...source(), metric_integrity: "verified" }).success).toBe(false);
});

test("B06 Manager safe cancellation survives READ_WORK revocation without new source reads", async () => {
  cap = { ...cap, can_decide: false, can_organize: true, can_cancel: true };
  const view = mount();
  await openTask();
  const initialReads = sourceReads;
  cap = { ...cap, can_read: false, can_organize: false };
  fireEvent.click(screen.getByText("Refresh"));
  await screen.findByText(/Work reading has been revoked/);
  expect(screen.queryByTestId("review-result-snapshot")).toBeNull();
  fireEvent.change(screen.getByTestId("second-reason"), { target: { value: "Security stop" } });
  fireEvent.click(screen.getByTestId("second-cancel"));
  await screen.findByText(/Command saved/);
  expect(captured).toHaveLength(1);
  expect(captured[0].method).toBe("secondReviewCancel");
  expect(sourceReads).toBe(initialReads);
  view.finish();
});

test("A10 late source response cannot populate another project", async () => {
  let resolveOld;
  mockCall.mockImplementation((m, o) =>
    m === "secondReviewSource"
      ? new Promise((resolve) => {
          resolveOld = resolve;
        })
      : implementation(m, o),
  );
  const view = mount();
  fireEvent.click(await screen.findByText(/#9 \/ 1/));
  await waitFor(() => expect(resolveOld).toBeTruthy());
  const stale = source();
  record = task(5);
  view.rerender(
    <view.Provider>
      <SecondReviewWorkspace projectId={5} />
    </view.Provider>,
  );
  await screen.findByText(/#9 \/ 1/);
  await act(async () => resolveOld(stale));
  expect(screen.queryByTestId("review-result-snapshot")).toBeNull();
  view.finish();
});

test.each(["owner", "authority"])(
  "B05 command discovery of changed %s clears old answers and actions",
  async (change) => {
    const view = mount();
    await openTask();
    if (change === "owner") cap = { ...cap, owner_hash: "e".repeat(64), viewer_hash: "f".repeat(64) };
    else cap = { error: true, $meta: { ok: false, status: 403 } };
    fireEvent.click(screen.getByTestId("second-confirm"));
    await waitFor(() => expect(screen.queryByTestId("review-result-snapshot")).toBeNull());
    expect(screen.queryByTestId("second-confirm")).toBeNull();
    expect(captured).toHaveLength(0);
    view.finish();
  },
);

test("A10 D09 late old capability cannot clear a newer owner's UNKNOWN intent", async () => {
  cap = { ...cap, can_organize: true, can_decide: false, can_cancel: true };
  post = () => ({ error: true });
  const view = mount("en-US", { initialSource: { batch: 8, source: 9 } });
  fireEvent.change(await screen.findByTestId("second-assignee"), { target: { value: "7" } });
  fireEvent.click(screen.getByTestId("second-dispatch"));
  await screen.findByTestId("second-recover");
  let resolveOld;
  let delay = true;
  const old = { ...cap };
  mockCall.mockImplementation((m, o) => {
    if (m === "secondReviewCapability" && delay) {
      delay = false;
      return new Promise((resolve) => {
        resolveOld = resolve;
      });
    }
    return implementation(m, o);
  });
  fireEvent.click(screen.getByText("Refresh"));
  await waitFor(() => expect(resolveOld).toBeTruthy());
  fireEvent(window, new Event("blur"));
  cap = { ...cap, owner_hash: "e".repeat(64), viewer_hash: "f".repeat(64) };
  fireEvent.click(screen.getByText("Refresh"));
  await waitFor(() => expect(screen.queryByTestId("second-recover")).toBeNull());
  fireEvent.click(screen.getByText("Refresh"));
  fireEvent.change(await screen.findByTestId("second-assignee"), { target: { value: "7" } });
  fireEvent.click(screen.getByTestId("second-dispatch"));
  await screen.findByTestId("second-recover");
  const newer = captured[1];
  await act(async () => resolveOld(old));
  post = null;
  fireEvent.click(screen.getByTestId("second-recover"));
  await screen.findByText(/Command saved/);
  expect(captured).toHaveLength(3);
  expect(captured[2].headers).toEqual(newer.headers);
  expect(captured[2].body).toEqual(newer.body);
  view.finish();
});

test("D10 saved notice stays bound to its task and owner", async () => {
  const view = mount();
  await openTask();
  fireEvent.click(screen.getByTestId("second-confirm"));
  await screen.findByText(/Command saved/);
  fireEvent.click(screen.getByText("Return to assigned queue"));
  await screen.findByText(/#9 \/ 1/);
  expect(screen.queryByText(/Command saved/)).toBeNull();
  view.finish();
});

test("D10 a new same-task command capability failure cannot inherit an earlier saved notice", async () => {
  cap = { ...cap, can_organize: true, can_decide: false, can_cancel: true };
  const view = mount("en-US", { initialSource: { batch: 8, source: 9 } });
  fireEvent.change(await screen.findByTestId("second-assignee"), { target: { value: "7" } });
  fireEvent.click(screen.getByTestId("second-dispatch"));
  await screen.findByText(/Command saved/);
  fireEvent.change(await screen.findByTestId("second-reason"), { target: { value: "Cancel" } });
  cap = { error: true };
  fireEvent.click(screen.getByTestId("second-cancel"));
  await waitFor(() => expect(screen.queryByText(/Command saved/)).toBeNull());
  expect(screen.queryByText(/command was saved, but/)).toBeNull();
  expect(captured).toHaveLength(1);
  view.finish();
});

test("F05 explicit supervisory refresh moves focus before disabling its trigger", async () => {
  const view = mount();
  await screen.findByText(/#9 \/ 1/);
  let finish;
  mockCall.mockImplementation((method, options) =>
    method === "secondReviews"
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : implementation(method, options),
  );
  const refresh = screen.getByText("Refresh");
  refresh.focus();
  fireEvent.click(refresh);
  const focusedDuringRead = document.activeElement;
  await waitFor(() => expect(finish).toBeDefined());
  await act(async () => finish({ results: [record], count: 1, next: null, ...cap }));
  expect(focusedDuringRead).toBe(screen.getByRole("heading"));
  view.finish();
});

test("F05 supervisory refresh completion keeps the user's later outside focus", async () => {
  const view = mount();
  await screen.findByText(/#9 \/ 1/);
  let finish;
  mockCall.mockImplementation((method, options) =>
    method === "secondReviews"
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : implementation(method, options),
  );
  fireEvent.click(screen.getByText("Refresh"));
  await waitFor(() => expect(finish).toBeDefined());
  const outside = document.createElement("button");
  document.body.appendChild(outside);
  outside.focus();
  await act(async () => finish({ results: [record], count: 1, next: null, ...cap }));
  const settled = document.activeElement;
  outside.remove();
  view.finish();
  expect(settled).toBe(outside);
});
