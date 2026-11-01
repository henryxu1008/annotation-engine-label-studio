import { fieldReviewHandoff } from "./fieldReviewHandoffResponses";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { FieldArbitrations } from "./FieldArbitrations";
import { fieldDigest } from "./fieldEvidence";
import { TextEncoder } from "node:util";

global.TextEncoder = TextEncoder;

const mockCall = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCall }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return {
    Button: React.forwardRef(({ children, look, size, ...props }, ref) =>
      React.createElement("button", { ...props, ref }, children),
    ),
  };
});
const hash = (c) => c.repeat(64);
const zero = { normalized_values: 0, field_equalities: 0 };
const cost = (scored = false) => ({
  validation_passes: 2,
  hydration_passes: scored ? 2 : 0,
  calculation_passes: scored ? 2 : 0,
  per_pass: scored ? { normalized_values: 2, field_equalities: 2 } : zero,
  total: scored ? { normalized_values: 4, field_equalities: 4 } : zero,
  scorer_calls: 0,
});
const schema = "field-arbitrations/v1";
const rule = "adopt-exact-field-submission/v1";
const origin = { field_batch_id: 3, task_id: 8, expected_manifest_hash: hash("a") };
const pins = [11, 12].map((id, i) => ({
  submission_id: id,
  assignment_id: 21 - i,
  assignee_id: 41 + i,
  submitter_id: 41 + i,
  task_id: 8,
  revision: 1,
  result_hash: hash("e"),
  config_hash: hash("f"),
  task_data_hash: hash("a"),
}));
const pair = {
  left: 11,
  right: 12,
  denominator: 1,
  excluded_empty_fields: 0,
  raw_matches: 0,
  normalized_matches: 0,
  raw_rate: { numerator: "0", denominator: "1" },
  normalized_rate: { numerator: "0", denominator: "1" },
  availability: "available",
  reason: null,
};
const comparison = {
  availability: "available",
  reason: null,
  input_count: 2,
  total_pairs: 1,
  configured_fields: 1,
  comparable_pairs: 1,
  unavailable_pairs: 0,
  cost_per_pass: { normalized_values: 2, field_equalities: 2 },
  pairs: [pair],
  selected_pair: {
    left: 11,
    right: 12,
    fields: [
      {
        name: "code",
        left: { raw: " A ", normalized: "A" },
        right: { raw: "B", normalized: "B" },
        informative: true,
        raw_equal: false,
        normalized_equal: false,
        kind: "value_difference",
      },
    ],
  },
};
const frozen = {
  outcome: "disagreement",
  reason: null,
  input_count: 2,
  total_pairs: 1,
  configured_fields: 1,
  comparable_pairs: 1,
  unavailable_pairs: 0,
  has_value_difference: true,
  has_format_difference: false,
  cost_per_pass: comparison.cost_per_pass,
  calculation_hash: hash("d"),
};
const evidenceHash = (inputs = pins, counts = frozen) =>
  fieldDigest({
    schema,
    item: {
      task_id: 8,
      cohort: inputs
        .map((p) => ({ assignment_id: p.assignment_id, assignee_id: p.assignee_id, source: p }))
        .sort((a, b) => a.assignment_id - b.assignment_id),
      selection_reason: null,
      result: counts,
    },
  });
let manager;
let state;
let viewer;
const meta = () => ({
  id: 7,
  schema,
  decision_rule: rule,
  field_batch_id: 3,
  task_id: 8,
  batch_name: "Field synthetic",
  assignee_id: 30,
  assignee_id_frozen: 30,
  input_set_hash: hash("b"),
  task_evidence_hash: evidenceHash(),
  viewer_hash: viewer,
  status: state,
  version: state === "pending" ? 1 : 2,
  created_at: "created",
  calculated_at: "calculated",
  can_decide: !manager && state === "pending",
  can_cancel: manager && state === "pending",
});
const cap = () => ({
  enabled: true,
  schema,
  decision_rule: rule,
  actor_id: manager ? 1 : 30,
  viewer_hash: viewer,
  can_read: true,
  can_create: manager,
  can_decide: !manager,
  can_cancel: manager,
  handoff_owner_hash: hash("e"),
  can_handoff: manager,
  processing_cost: cost(),
});
const detail = () => ({
  ...meta(),
  observed_at: "observed",
  source_status: "verified",
  reason: null,
  comparison: JSON.parse(JSON.stringify(comparison)),
  inputs: JSON.parse(JSON.stringify(pins)),
  frozen_counts: { ...frozen },
  decision:
    state === "resolved"
      ? {
          id: 9,
          actor_id: 30,
          created_at: "saved",
          winner_submission_id: 11,
          winner_revision: 1,
          reason: "Human reason",
          rule,
        }
      : null,
  cancellation: state === "cancelled" ? { actor_id: 1, created_at: "saved", reason: "Human reason" } : null,
  processing_cost: cost(true),
});
const receipt = (action, options) => {
  const body = options.body;
  const r = {
    schema,
    operation: action,
    project_id: 4,
    arbitration_id: 7,
    principal: "local",
    source: "label_studio_local",
    actor_id: manager ? 1 : 30,
    key: options.headers["Idempotency-Key"],
    created_at: "now",
    input_set_hash: hash("b"),
    status_at_commit: action === "create" ? "pending" : state,
    version_at_commit: action === "create" ? 1 : 2,
    request_hash: fieldDigest({ schema, action, project_id: 4, id: action === "create" ? null : 7, body }),
  };
  return { ...r, payload_hash: fieldDigest(r) };
};
const respond = async (method, options) => {
  switch (method) {
    case "fieldArbitrationCapability":
      return cap();
    case "fieldArbitrations":
      return { count: 1, next: null, previous: null, results: [meta()], viewer_hash: viewer, processing_cost: cost() };
    case "fieldArbitrationDetail":
      return detail();
    case "fieldArbitrationAssignees":
      return {
        ...origin,
        count: 1,
        next: null,
        previous: null,
        results: [{ id: 30, name: "Independent reviewer" }],
        batch_name: "Field synthetic",
        origin_manifest_hash: hash("a"),
        input_set_hash: hash("b"),
        viewer_hash: viewer,
        processing_cost: cost(),
      };
    case "createFieldArbitration":
      return {
        ...meta(),
        origin_manifest_hash: hash("a"),
        action: "create",
        replayed: false,
        current_status: state,
        current_version: state === "pending" ? 1 : 2,
        receipt: receipt("create", options),
        processing_cost: cost(true),
      };
    case "resolveFieldArbitration":
    case "cancelFieldArbitration": {
      const action = method === "resolveFieldArbitration" ? "decision" : "cancel";
      const replayed = state !== "pending";
      state = action === "decision" ? "resolved" : "cancelled";
      return {
        id: 7,
        schema,
        action,
        input_set_hash: hash("b"),
        viewer_hash: viewer,
        status: state,
        version: 2,
        current_status: state,
        current_version: 2,
        replayed,
        receipt: receipt(action, options),
        processing_cost: cost(!replayed && action === "decision"),
        ...(action === "decision"
          ? { decision_id: 9, winner_submission_id: options.body.winner_submission_id, winner_revision: 1 }
          : {}),
      };
    }
    default:
      throw new Error(`Forbidden transport ${method}`);
  }
};
const mount = (locale = "en-US", initialOrigin = null, props = {}) => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const rendered = render(
    <Provider>
      <FieldArbitrations projectId={4} initialOrigin={initialOrigin} {...props} />
    </Provider>,
  );
  return { ...rendered, runtime, Provider };
};

const handoffSource = { kind: "field", batch: 70, source: 11, pending: true };
const handoffScope = () => ({
  schema: "review-field-arbitration-handoff/v1",
  kind: "field",
  project_id: 4,
  viewer_hash: viewer,
  review_batch_id: 70,
  submission_id: 11,
});
const context = () => ({
  ...handoffScope(),
  actor_id: 1,
  eligible: true,
  reason: null,
  revision: 1,
  expected_plan_hash: hash("a"),
  expected_input_set_hash: hash("b"),
  can_handoff: true,
  dispatch_origin: origin,
  existing_arbitration: null,
  processing_cost: cost(true),
});
const history = () => ({
  ...handoffScope(),
  id: 9,
  actor_id: 1,
  revision: 1,
  arbitration_id: 7,
  input_set_hash: hash("b"),
  mode: "dispatch",
  created_at: "saved",
  available: true,
  source_integrity: "not_checked",
  source_status: "pending",
  arbitration_status: state,
  arbitration_version: state === "pending" ? 1 : 2,
  decision: null,
  processing_cost: cost(),
  target: {
    id: 7,
    input_set_hash: hash("b"),
    created_at: "created",
    assignee_id: 30,
    batch_id: 3,
    task_id: 8,
    status_at_link: "pending",
    version_at_link: 1,
  },
});
const provenance = () => ({
  ...handoffScope(),
  id: 9,
  arbitration_id: 7,
  snapshot_integrity: "not_checked",
  provenance_status: "verified",
  reason: null,
  target: meta(),
  processing_cost: cost(),
});
const reply = async (options, replayed = false) => ({
  ...handoffScope(),
  id: 9,
  actor_id: 1,
  arbitration_id: 7,
  input_set_hash: hash("b"),
  assignee_id: 30,
  replayed,
  processing_cost: cost(!replayed),
  receipt: {
    operation: "handoff",
    key: options.headers["Idempotency-Key"],
    request_hash: await fieldReviewHandoff.commandHash(4, handoffSource, options.body),
    mode: options.body.mode,
    plan_hash: options.body.expected_plan_hash,
    origin_hash: hash("a"),
    arbitration_status_at_commit: state,
    arbitration_version_at_commit: state === "pending" ? 1 : 2,
  },
});
const respondHandoff = async (method, options) => {
  if (method === "fieldReviewHandoffContext") return context();
  if (method === "fieldReviewHandoffDetail") return history();
  if (method === "fieldReviewHandoffProvenance") return provenance();
  if (method === "createFieldReviewHandoff") return reply(options);
  return respond(method, options);
};
const dispatch = async (locale = "en-US") => {
  fireEvent.change(await screen.findByRole("combobox"), { target: { value: "30" } });
  fireEvent.click(screen.getByRole("button", { name: locale === "zh-CN" ? "确认派发" : "Confirm dispatch" }));
};
beforeEach(() => {
  manager = true;
  state = "pending";
  viewer = hash("c");
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: require("crypto").webcrypto });
  mockCall.mockReset().mockImplementation(respondHandoff);
});

it.each(["en-US", "zh-CN"])("completes field handoff through the existing full workspace in %s", async (locale) => {
  const view = mount(locale, null, { handoffSource });
  await dispatch(locale);
  await screen.findByTestId("arbitration-saved");
  await screen.findByTestId("field-detail");
  const write = mockCall.mock.calls.find(([method]) => method === "createFieldReviewHandoff")[1];
  expect(write.body).toEqual({
    mode: "dispatch",
    expected_plan_hash: hash("a"),
    expected_input_set_hash: hash("b"),
    expected_viewer_hash: viewer,
    assignee_id: 30,
  });
  expect(mockCall.mock.calls.findIndex(([method]) => method === "fieldReviewHandoffProvenance")).toBeGreaterThan(
    mockCall.mock.calls.findIndex(([method]) => method === "fieldArbitrationDetail"),
  );
  expect(
    mockCall.mock.calls.some(
      ([method]) =>
        method.startsWith("classification") || method.startsWith("ner") || method === "createFieldArbitration",
    ),
  ).toBe(false);
  view.unmount();
  view.runtime.destroy();
});

it("links a resolved exact input without changing the existing field detail wire", async () => {
  state = "resolved";
  mockCall.mockImplementation((method, options) =>
    method === "fieldReviewHandoffContext"
      ? { ...context(), existing_arbitration: meta() }
      : respondHandoff(method, options),
  );
  const view = mount("en-US", null, { handoffSource });
  fireEvent.click(await screen.findByRole("button", { name: "Confirm association" }));
  await screen.findByTestId("arbitration-saved");
  await screen.findByTestId("arbitration-decision");
  const options = mockCall.mock.calls.find(([method]) => method === "createFieldReviewHandoff")[1];
  expect(options.body).toMatchObject({
    mode: "link_existing",
    field_arbitration_id: 7,
    expected_arbitration_version: 2,
  });
  expect(detail().origin_hash).toBeUndefined();
  view.unmount();
  view.runtime.destroy();
});

it("keeps B evidence invisible until the final provenance request completes", async () => {
  let finish;
  mockCall.mockImplementation((method, options) =>
    method === "fieldReviewHandoffProvenance"
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : respondHandoff(method, options),
  );
  const view = mount("en-US", null, { handoffSource, initialTaskId: 7, initialHandoffId: 9 });
  await waitFor(() => expect(finish).toBeDefined());
  expect(screen.queryByTestId("field-detail")).toBeNull();
  expect(screen.queryByText("Human reason")).toBeNull();
  await act(async () => finish(provenance()));
  await screen.findByTestId("field-detail");
  view.unmount();
  view.runtime.destroy();
});

it.each(["unavailable", "viewer", "handoff", "input", "evidence", "batch", "task", "version", "timeout"])(
  "masks B answers for failed final provenance: %s",
  async (failure) => {
    mockCall.mockImplementation((method, options) => {
      if (method !== "fieldReviewHandoffProvenance") return respondHandoff(method, options);
      const value = provenance();
      if (failure === "timeout") return Promise.reject(new Error("unknown read"));
      if (failure === "unavailable")
        return { ...value, provenance_status: "unavailable", reason: "source_unavailable", target: null };
      if (failure === "viewer") value.viewer_hash = hash("d");
      if (failure === "handoff") value.id += 1;
      if (failure === "input") value.target.input_set_hash = hash("d");
      if (failure === "evidence") value.target.task_evidence_hash = hash("d");
      if (failure === "batch") value.target.field_batch_id += 1;
      if (failure === "task") value.target.task_id += 1;
      if (failure === "version") value.target.version += 1;
      return value;
    });
    const view = mount("en-US", null, { handoffSource, initialTaskId: 7, initialHandoffId: 9 });
    await waitFor(() => expect(screen.getByTestId("field-arbitrations").getAttribute("aria-busy")).toBe("false"));
    expect(screen.queryByTestId("field-detail")).toBeNull();
    expect(screen.queryByTestId("arbitration-decision")).toBeNull();
    expect(screen.queryByRole("radio")).toBeNull();
    expect(mockCall.mock.calls.some(([method]) => method.startsWith("create") || method.startsWith("resolve"))).toBe(
      false,
    );
    view.unmount();
    view.runtime.destroy();
  },
);

it("does not poison field retention when B succeeds but provenance fails", async () => {
  let accepted = false;
  mockCall.mockImplementation((method, options) => {
    if (method === "fieldArbitrationDetail")
      return { ...detail(), calculated_at: accepted ? "new verified" : "unverified" };
    if (method === "fieldReviewHandoffProvenance" && !accepted) throw new Error("A unavailable");
    return respondHandoff(method, options);
  });
  const view = mount("en-US", null, { handoffSource, initialTaskId: 7, initialHandoffId: 9 });
  await screen.findByRole("alert");
  accepted = true;
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findByTestId("field-detail");
  view.unmount();
  view.runtime.destroy();
});

it("drops a late provenance response after blur without rendering or retaining evidence", async () => {
  let finish;
  mockCall.mockImplementation((method, options) =>
    method === "fieldReviewHandoffProvenance"
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : respondHandoff(method, options),
  );
  const view = mount("en-US", null, { handoffSource, initialTaskId: 7, initialHandoffId: 9 });
  await waitFor(() => expect(finish).toBeDefined());
  fireEvent.blur(window);
  await act(async () => finish(provenance()));
  expect(screen.queryByTestId("field-detail")).toBeNull();
  view.unmount();
  view.runtime.destroy();
});

it.each(["schema", "key", "actor", "viewer", "request", "assignee", "cost", "origin", "version"])(
  "recovers the original command after a contradictory %s ACK",
  async (failure) => {
    let writes = 0;
    mockCall.mockImplementation(async (method, options) => {
      if (method !== "createFieldReviewHandoff") return respondHandoff(method, options);
      const value = await reply(options, writes++ > 0);
      if (writes > 1) return value;
      if (failure === "schema") value.schema = "review-ner-arbitration-handoff/v1";
      if (failure === "key") value.receipt.key = "00000000-0000-4000-8000-000000000000";
      if (failure === "actor") value.actor_id += 1;
      if (failure === "viewer") value.viewer_hash = hash("d");
      if (failure === "request") value.receipt.request_hash = hash("d");
      if (failure === "assignee") value.assignee_id += 1;
      if (failure === "cost") value.processing_cost = cost(false);
      if (failure === "origin") value.receipt.origin_hash = "bad";
      if (failure === "version") value.receipt.arbitration_version_at_commit = 2;
      return value;
    });
    const view = mount("en-US", null, { handoffSource });
    await dispatch();
    await screen.findByRole("button", { name: "Recover original command" });
    expect(screen.queryByTestId("arbitration-saved")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
    await waitFor(() => expect(screen.getByTestId("field-arbitrations").getAttribute("aria-busy")).toBe("false"));
    expect(writes).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Recover original command" }));
    await screen.findByTestId("arbitration-saved");
    await screen.findByTestId("field-detail");
    const calls = mockCall.mock.calls.filter(([method]) => method === "createFieldReviewHandoff");
    expect(calls[0][1].body).toEqual(calls[1][1].body);
    expect(calls[0][1].headers).toEqual(calls[1][1].headers);
    view.unmount();
    view.runtime.destroy();
  },
);

it("keeps command hashes compatible with server canonical JSON without SubtleCrypto", async () => {
  const nodeCrypto = require("crypto");
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {
      getRandomValues: nodeCrypto.webcrypto.getRandomValues.bind(nodeCrypto.webcrypto),
    },
  });
  const body = {
    mode: "dispatch",
    expected_viewer_hash: viewer,
    expected_plan_hash: hash("a"),
    expected_input_set_hash: hash("b"),
    assignee_id: 30,
  };
  const canonicalBody = Object.fromEntries(Object.entries(body).sort(([a], [b]) => a.localeCompare(b)));
  const expected = nodeCrypto
    .createHash("sha256")
    .update(
      JSON.stringify({
        action: "handoff",
        batch_id: 70,
        body: canonicalBody,
        project_id: 4,
        schema: "review-field-arbitration-handoff/v1",
        source_id: 11,
      }),
    )
    .digest("hex");
  expect(await fieldReviewHandoff.commandHash(4, handoffSource, body)).toBe(expected);
});

it("recovers the original body and key after a same-owner viewer rotation and read refresh", async () => {
  let writes = 0;
  mockCall.mockImplementation((method, options) => {
    if (method !== "createFieldReviewHandoff") return respondHandoff(method, options);
    if (++writes === 1) return { error: true, $meta: { ok: false, status: 503 } };
    return reply(options, true);
  });
  const view = mount("en-US", null, { handoffSource });
  await dispatch();
  await screen.findByRole("button", { name: "Recover original command" });
  const original = mockCall.mock.calls.find(([method]) => method === "createFieldReviewHandoff")[1];
  viewer = hash("d"); // Same stable owner; only the server's viewer salt changed.
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await waitFor(() => expect(screen.getByTestId("field-arbitrations").getAttribute("aria-busy")).toBe("false"));
  expect(writes).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Recover original command" }));
  await screen.findByTestId("arbitration-saved");
  await screen.findByTestId("field-detail");
  const recovered = mockCall.mock.calls.filter(([method]) => method === "createFieldReviewHandoff")[1][1];
  expect(recovered.body).toEqual(original.body);
  expect(recovered.headers).toEqual(original.headers);
  expect(recovered.body.expected_viewer_hash).not.toBe(viewer);
  view.unmount();
  view.runtime.destroy();
});

it.each(["owner", "actor"])("clears an unknown field command when the actual %s changes", async (changed) => {
  let switched = false;
  let writes = 0;
  mockCall.mockImplementation((method, options) => {
    if (method === "fieldArbitrationCapability" && switched)
      return {
        ...cap(),
        ...(changed === "owner" ? { handoff_owner_hash: hash("f") } : { actor_id: 2 }),
      };
    if (method === "createFieldReviewHandoff") {
      writes += 1;
      return { error: true, $meta: { ok: false, status: 503 } };
    }
    return respondHandoff(method, options);
  });
  const view = mount("en-US", null, { handoffSource });
  await dispatch();
  await screen.findByRole("button", { name: "Recover original command" });
  switched = true;
  viewer = hash("d");
  fireEvent.click(screen.getByRole("button", { name: "Recover original command" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Recover original command" })).toBeNull();
  expect(writes).toBe(1);
  view.unmount();
  view.runtime.destroy();
});

it("keeps standalone field commands on their original viewer-change contract", async () => {
  mockCall.mockImplementation((method, options) =>
    method === "createFieldArbitration"
      ? { error: true, $meta: { ok: false, status: 503 } }
      : respondHandoff(method, options),
  );
  const view = mount("en-US", origin);
  await dispatch();
  await screen.findByRole("button", { name: "Recover original command" });
  viewer = hash("d");
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Recover original command" })).toBeNull();
  expect(mockCall.mock.calls.filter(([method]) => method === "createFieldArbitration")).toHaveLength(1);
  view.unmount();
  view.runtime.destroy();
});
