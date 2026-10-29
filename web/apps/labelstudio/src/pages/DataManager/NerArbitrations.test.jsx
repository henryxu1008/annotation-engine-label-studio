import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { NerArbitrations } from "./NerArbitrations";

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
const h = (c) => c.repeat(64);
const origin = { ner_batch_id: 3, task_id: 8, expected_manifest_hash: h("a") };
const meta = {
  id: 7,
  schema: "ner-arbitrations/v1",
  decision_rule: "adopt-exact-ner-submission/v1",
  ner_batch_id: 3,
  task_id: 8,
  batch_name: "NER synthetic",
  assignee_id: 30,
  assignee_id_frozen: 30,
  input_set_hash: h("b"),
  task_evidence_hash: h("d"),
  viewer_hash: h("c"),
  status: "pending",
  version: 1,
  created_at: "created",
  calculated_at: "calculated",
  can_decide: true,
  can_cancel: false,
};
const pins = [11, 12].map((id) => ({
  submission_id: id,
  assignment_id: id,
  assignee_id: id + 30,
  task_id: 8,
  revision: 1,
  submitter_id: id + 30,
  result_hash: h("e"),
  config_hash: h("f"),
  task_data_hash: h("a"),
}));
const entity = { start: 0, end: 5, label: "PER" };
const comparison = {
  availability: "available",
  reason: null,
  input_count: 2,
  total_pairs: 1,
  comparable_pairs: 1,
  unavailable_pairs: 0,
  pairs: [
    {
      left: 11,
      right: 12,
      left_entities: 1,
      right_entities: 1,
      matching_entities: 0,
      f1: { numerator: "0", denominator: "1" },
      availability: "available",
      reason: null,
    },
  ],
  selected_pair: {
    left: 11,
    right: 12,
    text: "Alice 😀 李",
    control: { from_name: "ner", to_name: "text", field: "text", labels: ["PER"] },
    left_entities: [entity],
    right_entities: [{ ...entity, end: 4 }],
    matches: [],
    groups: [{ start: 0, end: 5, left: [0], right: [0], kind: "boundary_difference" }],
  },
};
let manager;
let state;
const cap = () => ({
  enabled: true,
  schema: meta.schema,
  decision_rule: meta.decision_rule,
  actor_id: manager ? 1 : 30,
  viewer_hash: meta.viewer_hash,
  can_read: true,
  can_create: manager,
  can_decide: !manager,
  can_cancel: manager,
});
const metadata = () => ({
  ...meta,
  status: state,
  version: state === "pending" ? 1 : 2,
  can_decide: !manager && state === "pending",
  can_cancel: manager && state === "pending",
});
const detail = () => ({
  ...metadata(),
  observed_at: "now",
  source_status: "verified",
  reason: null,
  inputs: pins,
  comparison,
  frozen_counts: {
    outcome: "disagreement",
    reason: null,
    input_count: 2,
    total_pairs: 1,
    comparable_pairs: 1,
    calculation_hash: h("a"),
  },
  decision:
    state === "resolved"
      ? {
          id: 9,
          actor_id: 30,
          created_at: "saved",
          winner_submission_id: 11,
          winner_revision: 1,
          reason: "Human reason",
          rule: meta.decision_rule,
        }
      : null,
  cancellation: state === "cancelled" ? { actor_id: 1, created_at: "saved", reason: "Human reason" } : null,
});
const receipt = (action, options) => ({
  schema: meta.schema,
  operation: action,
  project_id: 4,
  arbitration_id: 7,
  principal: "local",
  source: "label_studio_local",
  actor_id: manager ? 1 : 30,
  key: options.headers["Idempotency-Key"],
  created_at: "now",
  request_hash: h("a"),
  payload_hash: h("b"),
  input_set_hash: meta.input_set_hash,
  status_at_commit: action === "create" ? "pending" : state,
  version_at_commit: action === "create" ? 1 : 2,
});

const three = (pair = [11, 12]) => ({
  ...detail(),
  inputs: [...pins, { ...pins[1], submission_id: 13, assignment_id: 13, assignee_id: 43, submitter_id: 43 }],
  frozen_counts: { ...detail().frozen_counts, input_count: 3, total_pairs: 3, comparable_pairs: 3 },
  comparison: {
    ...comparison,
    input_count: 3,
    total_pairs: 3,
    comparable_pairs: 3,
    pairs: [
      [11, 12],
      [11, 13],
      [12, 13],
    ].map(([left, right]) => ({ ...comparison.pairs[0], left, right })),
    selected_pair: { ...comparison.selected_pair, left: pair[0], right: pair[1] },
  },
});
const respond = async (method, options) => {
  switch (method) {
    case "nerArbitrationCapability":
      return cap();
    case "nerArbitrations":
      return { count: 1, next: null, previous: null, results: [metadata()], viewer_hash: meta.viewer_hash };
    case "nerArbitrationDetail":
      return detail();
    case "nerArbitrationAssignees":
      return {
        ...origin,
        count: 1,
        next: null,
        previous: null,
        results: [{ id: 30, name: "Independent reviewer" }],
        batch_name: meta.batch_name,
        origin_manifest_hash: origin.expected_manifest_hash,
        input_set_hash: meta.input_set_hash,
        viewer_hash: meta.viewer_hash,
      };
    case "createNerArbitration":
      return {
        ...metadata(),
        origin_manifest_hash: h("a"),
        action: "create",
        replayed: false,
        current_status: state,
        current_version: state === "pending" ? 1 : 2,
        receipt: receipt("create", options),
      };
    case "resolveNerArbitration":
    case "cancelNerArbitration": {
      const action = method === "resolveNerArbitration" ? "decision" : "cancel";
      state = action === "decision" ? "resolved" : "cancelled";
      return {
        id: 7,
        schema: meta.schema,
        action,
        input_set_hash: meta.input_set_hash,
        viewer_hash: meta.viewer_hash,
        status: state,
        version: 2,
        current_status: state,
        current_version: 2,
        replayed: false,
        receipt: receipt(action, options),
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
  return {
    ...render(
      <Provider>
        <NerArbitrations projectId={4} initialOrigin={initialOrigin} {...props} />
      </Provider>,
    ),
    runtime,
    Provider,
  };
};
beforeEach(() => {
  manager = false;
  state = "pending";
  mockCall.mockReset().mockImplementation(respond);
});

it.each(["en-US", "zh-CN"])("adopts a whole frozen NER revision in %s", async (locale) => {
  const zh = locale === "zh-CN";
  const view = mount(locale);
  fireEvent.click(await screen.findByRole("button", { name: /NER synthetic/ }));
  expect((await screen.findByTestId("ner-left-text")).textContent).toContain("Alice");
  expect(screen.getByTestId("ner-matrix").textContent).toContain("0.00%");
  fireEvent.click(screen.getAllByRole("radio")[0]);
  fireEvent.change(screen.getByLabelText(zh ? "裁决或取消依据" : "Decision or cancellation reason"), {
    target: { value: "Human reason" },
  });
  fireEvent.click(screen.getByRole("button", { name: zh ? "采纳此冻结修订" : "Adopt this frozen revision" }));
  await screen.findByTestId("arbitration-saved");
  expect((await screen.findByTestId("arbitration-decision")).textContent).toContain("Human reason");
  expect(mockCall.mock.calls.filter(([m]) => m === "resolveNerArbitration")).toHaveLength(1);
  expect(mockCall.mock.calls.every(([m]) => m.startsWith("ner") || m.endsWith("NerArbitration"))).toBe(true);
  view.unmount();
  view.runtime.destroy();
});

it("dispatches with ner_batch_id and an independent actor", async () => {
  manager = true;
  const view = mount("en-US", origin);
  fireEvent.change(await screen.findByLabelText("Independent reviewer"), { target: { value: "30" } });
  fireEvent.click(screen.getByRole("button", { name: "Confirm dispatch" }));
  await screen.findByTestId("arbitration-saved");
  const request = mockCall.mock.calls.find(([m]) => m === "createNerArbitration")[1];
  expect(request.body).toEqual({ ...origin, assignee_id: 30 });
  expect(request.body.batch_id).toBeUndefined();
  view.unmount();
  view.runtime.destroy();
});

it("recovers only the same key/body after a lost acknowledgement", async () => {
  let calls = 0;
  mockCall.mockImplementation(async (m, o) => {
    if (m === "resolveNerArbitration" && ++calls === 1) throw new Error("lost response");
    return respond(m, o);
  });
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /NER synthetic/ }));
  fireEvent.click((await screen.findAllByRole("radio"))[0]);
  fireEvent.change(screen.getByLabelText("Decision or cancellation reason"), { target: { value: "Human reason" } });
  fireEvent.click(screen.getByRole("button", { name: "Adopt this frozen revision" }));
  await screen.findByRole("button", { name: "Recover original command" });
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findByTestId("ner-left-text");
  expect(calls).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Recover original command" }));
  await screen.findByTestId("arbitration-saved");
  const requests = mockCall.mock.calls.filter(([m]) => m === "resolveNerArbitration");
  expect(requests[1][1].body).toEqual(requests[0][1].body);
  expect(requests[1][1].headers).toEqual(requests[0][1].headers);
  view.unmount();
  view.runtime.destroy();
});

it("rejects classification-shaped and mismatched source replies", async () => {
  mockCall.mockImplementation(async (m, o) =>
    m === "nerArbitrationDetail" ? { ...detail(), schema: "classification-arbitrations/v1" } : respond(m, o),
  );
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /NER synthetic/ }));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("ner-left-text")).toBeNull();
  expect(screen.queryByRole("radio")).toBeNull();
  view.unmount();
  view.runtime.destroy();
});

it("hides unavailable evidence without inventing an empty-cohort score", async () => {
  mockCall.mockImplementation(async (m, o) =>
    m === "nerArbitrationDetail"
      ? {
          ...detail(),
          source_status: "unavailable",
          comparison: null,
          inputs: [],
          reason: "source_unavailable",
          can_decide: false,
        }
      : respond(m, o),
  );
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /NER synthetic/ }));
  await screen.findByTestId("arbitration-unavailable");
  expect(screen.queryByTestId("ner-matrix")).toBeNull();
  expect(screen.queryByRole("radio")).toBeNull();
  view.unmount();
  view.runtime.destroy();
});

it("discards late answers on blur and never posts on refresh", async () => {
  let release;
  mockCall.mockImplementation((m, o) =>
    m === "nerArbitrationDetail"
      ? new Promise((r) => {
          release = r;
        })
      : respond(m, o),
  );
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /NER synthetic/ }));
  await waitFor(() => expect(release).toBeDefined());
  fireEvent.blur(window);
  await act(async () => release(detail()));
  expect(screen.queryByTestId("ner-left-text")).toBeNull();
  expect(mockCall.mock.calls.some(([m]) => m === "resolveNerArbitration")).toBe(false);
  view.unmount();
  view.runtime.destroy();
});

it.each(["actor", "viewer", "input", "winner", "project"])(
  "does not acknowledge a mismatched %s receipt",
  async (field) => {
    mockCall.mockImplementation(async (m, o) => {
      const value = await respond(m, o);
      if (m === "resolveNerArbitration") {
        if (field === "actor") value.receipt.actor_id++;
        if (field === "viewer") value.viewer_hash = h("f");
        if (field === "input") value.receipt.input_set_hash = h("f");
        if (field === "winner") value.winner_revision++;
        if (field === "project") value.receipt.project_id++;
      }
      return value;
    });
    const view = mount();
    fireEvent.click(await screen.findByRole("button", { name: /NER synthetic/ }));
    fireEvent.click((await screen.findAllByRole("radio"))[0]);
    fireEvent.change(screen.getByLabelText("Decision or cancellation reason"), { target: { value: "Human reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Adopt this frozen revision" }));
    await screen.findByRole("button", { name: "Recover original command" });
    expect(screen.queryByTestId("arbitration-saved")).toBeNull();
    view.unmount();
    view.runtime.destroy();
  },
);

it("requires the current identity before posting a recovered command", async () => {
  let changed = false;
  mockCall.mockImplementation(async (m, o) => {
    if (m === "resolveNerArbitration") throw new Error("unknown");
    if (m === "nerArbitrationCapability" && changed) return { ...cap(), actor_id: 31, viewer_hash: h("f") };
    return respond(m, o);
  });
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /NER synthetic/ }));
  fireEvent.click((await screen.findAllByRole("radio"))[0]);
  fireEvent.change(screen.getByLabelText("Decision or cancellation reason"), { target: { value: "Human reason" } });
  fireEvent.click(screen.getByRole("button", { name: "Adopt this frozen revision" }));
  await screen.findByRole("button", { name: "Recover original command" });
  changed = true;
  fireEvent.click(screen.getByRole("button", { name: "Recover original command" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Recover original command" })).toBeNull());
  expect(mockCall.mock.calls.filter(([m]) => m === "resolveNerArbitration")).toHaveLength(1);
  expect(screen.queryByTestId("ner-left-text")).toBeNull();
  view.unmount();
  view.runtime.destroy();
});

it("loads a verified pair containing a third winner before adoption", async () => {
  let release;
  mockCall.mockImplementation((m, o) => {
    if (m !== "nerArbitrationDetail") return respond(m, o);
    if (o.params.left)
      return new Promise((r) => {
        release = () => r(three([o.params.left, o.params.right]));
      });
    return Promise.resolve(three());
  });
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /NER synthetic/ }));
  await screen.findAllByRole("radio");
  fireEvent.change(screen.getByLabelText("Decision or cancellation reason"), { target: { value: "Human reason" } });
  fireEvent.click(screen.getAllByRole("radio")[2]);
  await waitFor(() => expect(release).toBeDefined());
  expect(screen.queryByRole("button", { name: "Adopt this frozen revision" })).toBeNull();
  expect(mockCall.mock.calls.some(([m]) => m === "resolveNerArbitration")).toBe(false);
  await act(async () => release());
  expect(screen.getAllByRole("radio")[2].checked).toBe(true);
  expect(screen.getByRole("button", { name: "Adopt this frozen revision" }).disabled).toBe(false);
  const read = mockCall.mock.calls.filter(([m]) => m === "nerArbitrationDetail").at(-1)[1];
  expect([read.params.left, read.params.right]).toEqual([11, 13]);
  view.unmount();
  view.runtime.destroy();
});

it.each(["identity", "input"])("discards retained form inputs when paired reads change %s", async (boundary) => {
  let changed = false;
  mockCall.mockImplementation(async (method, options) => {
    if (method === "nerArbitrationCapability")
      return { ...cap(), ...(changed && boundary === "identity" ? { viewer_hash: h("f") } : {}) };
    if (method === "nerArbitrationDetail")
      return {
        ...three(options.params.left ? [options.params.left, options.params.right] : undefined),
        ...(changed ? { [boundary === "identity" ? "viewer_hash" : "input_set_hash"]: h("f") } : {}),
      };
    return respond(method, options);
  });
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /NER synthetic/ }));
  await screen.findAllByRole("radio");
  fireEvent.change(screen.getByLabelText("Decision or cancellation reason"), {
    target: { value: "Prior identity's private reason" },
  });
  changed = true;
  fireEvent.click(screen.getAllByRole("radio")[2]);
  await screen.findByRole("alert");
  expect(screen.queryByTestId("ner-left-text")).toBeNull();
  expect(screen.queryByDisplayValue("Prior identity's private reason")).toBeNull();
  expect(mockCall.mock.calls.some(([method]) => method === "resolveNerArbitration")).toBe(false);
  view.unmount();
  view.runtime.destroy();
});

it("drops the prior identity's unknown command on read refresh", async () => {
  let changed = false;
  mockCall.mockImplementation(async (method, options) => {
    if (method === "resolveNerArbitration") throw new Error("unknown commit result");
    const value = await respond(method, options);
    return changed ? { ...value, viewer_hash: h("f") } : value;
  });
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /NER synthetic/ }));
  fireEvent.click((await screen.findAllByRole("radio"))[0]);
  fireEvent.change(screen.getByLabelText("Decision or cancellation reason"), {
    target: { value: "Prior identity's reason" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Adopt this frozen revision" }));
  await screen.findByRole("button", { name: "Recover original command" });
  changed = true;
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Recover original command" })).toBeNull();
  expect(screen.queryByTestId("ner-left-text")).toBeNull();
  expect(mockCall.mock.calls.filter(([method]) => method === "resolveNerArbitration")).toHaveLength(1);
  view.unmount();
  view.runtime.destroy();
});

it("keeps an acknowledged command saved when the following read fails", async () => {
  mockCall.mockImplementation(async (m, o) => {
    if (m === "nerArbitrationDetail" && state === "resolved") throw new Error("read unavailable");
    return respond(m, o);
  });
  const view = mount();
  fireEvent.click(await screen.findByRole("button", { name: /NER synthetic/ }));
  fireEvent.click((await screen.findAllByRole("radio"))[0]);
  fireEvent.change(screen.getByLabelText("Decision or cancellation reason"), { target: { value: "Human reason" } });
  fireEvent.click(screen.getByRole("button", { name: "Adopt this frozen revision" }));
  await screen.findByRole("alert");
  expect(screen.getByTestId("arbitration-saved")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Recover original command" })).toBeNull();
  expect(mockCall.mock.calls.filter(([m]) => m === "resolveNerArbitration")).toHaveLength(1);
  view.unmount();
  view.runtime.destroy();
});
