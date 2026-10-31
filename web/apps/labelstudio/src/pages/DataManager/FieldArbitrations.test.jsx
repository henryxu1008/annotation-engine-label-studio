import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { FieldArbitrations, fieldArbitrationAdapter } from "./FieldArbitrations";
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
beforeEach(() => {
  manager = false;
  state = "pending";
  viewer = hash("c");
  mockCall.mockReset().mockImplementation(respond);
});
it("validates zero-cost metadata before any answer request", async () => {
  const c = fieldArbitrationAdapter.parse("classificationArbitrationCapability", cap(), { projectId: 4 });
  expect(c.processing_cost.hydration_passes).toBe(0);
  const raw = await respond("fieldArbitrations", {});
  const result = fieldArbitrationAdapter.parse("classificationArbitrations", raw, { projectId: 4 });
  expect(result.results[0].batch_id).toBe(3);
});
const open = async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Field synthetic/ }));
  await screen.findByTestId("field-detail");
};
const choose = () => {
  fireEvent.click(screen.getAllByRole("radio")[0]);
  fireEvent.change(screen.getByLabelText("Decision or cancellation reason"), { target: { value: "Human reason" } });
};

it.each(["en-US", "zh-CN"])("adopts an entire frozen field revision in %s", async (locale) => {
  const view = mount(locale);
  const zh = locale === "zh-CN";
  await open();
  expect(screen.getByTestId("field-detail").textContent).toContain(" A ");
  expect(screen.getAllByRole("radio")[0].parentElement.textContent).toContain(zh ? "字段数：1" : "Fields: 1");
  fireEvent.click(screen.getAllByRole("radio")[0]);
  fireEvent.change(screen.getByLabelText(zh ? "裁决或取消依据" : "Decision or cancellation reason"), {
    target: { value: "Human reason" },
  });
  fireEvent.click(screen.getByRole("button", { name: zh ? "采纳此冻结修订" : "Adopt this frozen revision" }));
  await screen.findByTestId("arbitration-saved");
  expect((await screen.findByTestId("arbitration-decision")).textContent).toContain("Human reason");
  expect(mockCall.mock.calls.filter(([m]) => m === "resolveFieldArbitration")).toHaveLength(1);
  expect(mockCall.mock.calls.every(([m]) => m.startsWith("field") || m.endsWith("FieldArbitration"))).toBe(true);
  view.unmount();
  view.runtime.destroy();
});

it("dispatches through the existing Manager form with a field-only origin", async () => {
  manager = true;
  const view = mount("en-US", origin);
  const select = await screen.findByRole("combobox");
  fireEvent.change(select, { target: { value: "30" } });
  fireEvent.click(screen.getByRole("button", { name: "Confirm dispatch" }));
  await screen.findByTestId("arbitration-saved");
  const command = mockCall.mock.calls.find(([m]) => m === "createFieldArbitration");
  expect(command[1].body).toEqual({ ...origin, assignee_id: 30 });
  view.unmount();
  view.runtime.destroy();
});

it.each([
  ["changed revision", (v) => v.inputs[0].revision++],
  ["recomputed provenance with wrong task", (v) => (v.task_id = 9)],
  ["wrong task evidence hash", (v) => (v.task_evidence_hash = hash("f"))],
  ["fake denominator", (v) => (v.comparison.pairs[0].denominator = 2)],
  ["normalization mismatch", (v) => (v.comparison.selected_pair.fields[0].left.normalized = " A ")],
  ["incorrect actual cost", (v) => v.processing_cost.total.field_equalities++],
  ["hidden third-party call", (v) => (v.processing_cost.scorer_calls = 1)],
  ["lost configured field", (v) => (v.frozen_counts.configured_fields = 2)],
])("rejects %s without exposing answers", async (_name, change) => {
  mockCall.mockImplementation(async (m, o) => {
    const v = await respond(m, o);
    if (m === "fieldArbitrationDetail") change(v);
    return v;
  });
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /Field synthetic/ }));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  expect(screen.queryAllByRole("radio")).toHaveLength(0);
  view.unmount();
  view.runtime.destroy();
});

it("masks all field values and adoption on unavailable originals", async () => {
  mockCall.mockImplementation(async (m, o) => {
    if (m !== "fieldArbitrationDetail") return respond(m, o);
    return {
      ...detail(),
      source_status: "unavailable",
      reason: "source_unavailable",
      comparison: null,
      inputs: [],
      can_decide: false,
      processing_cost: cost(),
    };
  });
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /Field synthetic/ }));
  await screen.findByTestId("arbitration-unavailable");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  expect(screen.queryAllByRole("radio")).toHaveLength(0);
  view.unmount();
  view.runtime.destroy();
});

it("retains the original body/key after an invalid ACK and refresh never POSTs", async () => {
  const commands = [];
  let attempt = 0;
  mockCall.mockImplementation(async (m, o) => {
    const v = await respond(m, o);
    if (m === "resolveFieldArbitration") {
      commands.push(o);
      if (++attempt === 1) v.receipt.payload_hash = hash("a");
    }
    return v;
  });
  const view = mount();
  await open();
  choose();
  fireEvent.click(screen.getByRole("button", { name: "Adopt this frozen revision" }));
  await screen.findByRole("button", { name: "Recover original command" });
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findByTestId("arbitration-decision");
  expect(commands).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Recover original command" }));
  await screen.findByTestId("arbitration-saved");
  expect(commands).toHaveLength(2);
  expect(commands[1].body).toEqual(commands[0].body);
  expect(commands[1].headers).toEqual(commands[0].headers);
  view.unmount();
  view.runtime.destroy();
});

it("rejects a rehashed changed cohort in the same retained arbitration", async () => {
  const view = mount();
  await open();
  let modified = false;
  mockCall.mockImplementation(async (m, o) => {
    const v = await respond(m, o);
    if (m === "fieldArbitrationDetail" && modified) {
      v.inputs[0].revision = 2;
      v.task_evidence_hash = evidenceHash(v.inputs, v.frozen_counts);
    }
    return v;
  });
  modified = true;
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  view.unmount();
  view.runtime.destroy();
});

it("clears answers on revocation and does not recover with stale authority", async () => {
  const view = mount();
  await open();
  mockCall.mockImplementation(async () => ({ status: 403, $meta: { ok: false }, error: "revoked" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  expect(screen.queryAllByRole("radio")).toHaveLength(0);
  view.unmount();
  view.runtime.destroy();
});

it("drops a late detail after blur and fences the newer viewer", async () => {
  let resolveOld;
  const old = new Promise((r) => {
    resolveOld = r;
  });
  mockCall.mockImplementation(async (m, o) => (m === "fieldArbitrationDetail" ? old : respond(m, o)));
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /Field synthetic/ }));
  const oldDetail = detail();
  act(() => window.dispatchEvent(new Event("blur")));
  viewer = hash("f");
  mockCall.mockImplementation(respond);
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findByTestId("field-detail");
  await act(async () => resolveOld(oldDetail));
  expect(screen.getByTestId("field-detail")).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  view.unmount();
  view.runtime.destroy();
});

it("does not retain a late old cohort over a newer accepted detail", async () => {
  const view = mount();
  await open();
  let release;
  const held = new Promise((r) => {
    release = r;
  });
  const oldDetail = detail();
  oldDetail.task_evidence_hash = hash("a");
  mockCall.mockImplementation(async (m, o) => (m === "fieldArbitrationDetail" ? held : respond(m, o)));
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  act(() => window.dispatchEvent(new Event("blur")));
  mockCall.mockImplementation(respond);
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findByTestId("field-detail");
  await act(async () => release(oldDetail));
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(await screen.findByTestId("field-detail")).toBeTruthy();
  view.unmount();
  view.runtime.destroy();
});

it.each([
  ["winner outside the frozen cohort", (v) => (v.decision.winner_submission_id = 99)],
  ["winner at a different revision", (v) => v.decision.winner_revision++],
  ["decision by a different actor", (v) => v.decision.actor_id++],
  ["terminal adoption permission", (v) => (v.can_decide = true)],
  ["terminal cancellation permission", (v) => (v.can_cancel = true)],
])("masks a resolved projection with %s", async (_name, change) => {
  state = "resolved";
  mockCall.mockImplementation(async (m, o) => {
    const v = await respond(m, o);
    if (m === "fieldArbitrationDetail") change(v);
    return v;
  });
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /Field synthetic/ }));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  expect(screen.queryByTestId("arbitration-decision")).toBeNull();
  expect(screen.queryAllByRole("radio")).toHaveLength(0);
  view.unmount();
  view.runtime.destroy();
});

it.each(["create", "decision"])("rejects source-free cost on a fresh %s acknowledgement", async (action) => {
  const method = action === "create" ? "createFieldArbitration" : "resolveFieldArbitration";
  const body =
    action === "create"
      ? { ...origin, assignee_id: 30 }
      : {
          expected_version: 1,
          expected_input_set_hash: hash("b"),
          winner_submission_id: 11,
          reason: "Complete frozen revision",
        };
  const options = { body, headers: { "Idempotency-Key": "a1b2c3d4-1234-4234-8234-123456789abc" } };
  const raw = await respond(method, options);
  raw.processing_cost = cost();
  expect(() =>
    fieldArbitrationAdapter.parse(
      action === "create" ? "createClassificationArbitration" : "resolveClassificationArbitration",
      raw,
      { projectId: 4, arbitration: 7 },
    ),
  ).toThrow();
});

it("recovers the original command after contradictory fresh cost without losing its body/key", async () => {
  const commands = [];
  mockCall.mockImplementation(async (m, o) => {
    const v = await respond(m, o);
    if (m === "resolveFieldArbitration") {
      commands.push(o);
      if (commands.length === 1) v.processing_cost = cost();
    }
    return v;
  });
  const view = mount();
  await open();
  choose();
  fireEvent.click(screen.getByRole("button", { name: "Adopt this frozen revision" }));
  await screen.findByRole("button", { name: "Recover original command" });
  expect(screen.queryByTestId("arbitration-saved")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Recover original command" }));
  await screen.findByTestId("arbitration-saved");
  expect(commands).toHaveLength(2);
  expect(commands[1].body).toEqual(commands[0].body);
  expect(commands[1].headers).toEqual(commands[0].headers);
  view.unmount();
  view.runtime.destroy();
});

it("keeps authorized resolved metadata when the original source is unavailable", async () => {
  state = "resolved";
  mockCall.mockImplementation(async (m, o) => {
    const v = await respond(m, o);
    if (m === "fieldArbitrationDetail")
      Object.assign(v, {
        source_status: "unavailable",
        reason: "source_unavailable",
        comparison: null,
        inputs: [],
        processing_cost: cost(),
      });
    return v;
  });
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /Field synthetic/ }));
  await screen.findByTestId("arbitration-unavailable");
  expect(screen.getByTestId("arbitration-decision").textContent).toContain("Human reason");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  expect(screen.queryAllByRole("radio")).toHaveLength(0);
  view.unmount();
  view.runtime.destroy();
});
