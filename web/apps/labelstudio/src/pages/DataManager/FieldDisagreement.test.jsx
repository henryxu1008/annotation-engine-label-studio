import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TextEncoder } from "node:util";
import { createLocaleRuntime } from "@humansignal/i18n";
import { FieldDisagreement, FieldEntry, validateFieldReport } from "./FieldDisagreement";

const mockCallApi = jest.fn();
// jsdom lacks this browser standard; use Node's implementation without mocking hashing.
beforeAll(() => {
  globalThis.TextEncoder = TextEncoder;
});
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return {
    Button: React.forwardRef(({ children, look, size, ...props }, ref) =>
      React.createElement("button", { ...props, ref }, children),
    ),
  };
});
const hash = (c) => c.repeat(64);
const ratio = (n, d = 1) => ({ numerator: String(n), denominator: String(d) });
const pins = [1, 2, 3].map((id) => ({
  submission_id: id,
  assignment_id: id,
  assignee_id: id + 10,
  task_id: 8,
  revision: 1,
  submitter_id: id + 10,
  result_hash: hash("a"),
  config_hash: hash("b"),
  task_data_hash: hash("c"),
}));
const sources = () => ({
  schema: "field-disagreements/v1",
  viewer: hash("e"),
  max_inputs: 20,
  scope: { project_id: 4, task_id: 8 },
  sources: pins.map((p) => ({
    id: p.submission_id,
    assignment_id: p.assignment_id,
    assignment__assignee_id: p.assignee_id,
    assignment__task_id: p.task_id,
    submitted_by_id: p.submitter_id,
    revision: p.revision,
    result_hash: p.result_hash,
    status: "pending",
  })),
});
const response = (left = 1, right = 2) => {
  const contracts = {
    schema: "field-disagreements/v1",
    rule: "field-lf-ascii-trim/v1",
    explanation: "field-value-differences/v1",
    engine: "python-string-equality/v1",
  };
  return {
    ...contracts,
    viewer: hash("e"),
    scope: { project_id: 4, task_id: 8, input_ids: [1, 2, 3] },
    manifest: {
      ...contracts,
      project_id: 4,
      task_id: 8,
      viewer: hash("e"),
      inputs: pins.map((p) => ({ ...p })),
      configured_fields: 2,
      calculated_at: "2026-10-29T00:00:00+00:00",
      result_hash: hash("d"),
    },
    // Independent Python sorted-key JSON / SHA-256 fixture, including input arrays.
    manifest_hash: "fe9f3b4b3591230cfbc0e45bf268b2635ab6dd3faf149394b84a077ea9f0bf90",
    frozen: "field-receipt",
    calculated_at: "2026-10-29T00:00:00+00:00",
    observed_at: "2026-10-29T00:00:01+00:00",
    source_status: "verified",
    receipt_verified: true,
    result_hash: hash("d"),
    availability: "available",
    reason: null,
    input_count: 3,
    total_pairs: 3,
    comparable_pairs: 3,
    unavailable_pairs: 0,
    configured_fields: 2,
    cost_per_pass: { normalized_values: 6, field_equalities: 12 },
    pairs: [
      [1, 2],
      [1, 3],
      [2, 3],
    ].map(([a, b]) => ({
      left: a,
      right: b,
      denominator: 2,
      excluded_empty_fields: 0,
      raw_matches: 0,
      normalized_matches: 1,
      raw_rate: ratio(0),
      normalized_rate: ratio(1, 2),
      availability: "available",
      reason: null,
    })),
    selected_pair: {
      left,
      right,
      fields: [
        {
          name: "name",
          left: { raw: " 张三 ", normalized: "张三" },
          right: { raw: "张三", normalized: "张三" },
          informative: true,
          raw_equal: false,
          normalized_equal: true,
          kind: "format_only",
        },
        {
          name: "code",
          left: { raw: '<img src="x">001', normalized: '<img src="x">001' },
          right: { raw: "1", normalized: "1" },
          informative: true,
          raw_equal: false,
          normalized_equal: false,
          kind: "value_difference",
        },
      ],
    },
  };
};
const mount = (locale = "en-US", entry = false) => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  return {
    ...render(
      <Provider>
        {entry ? <FieldEntry projectId={4} /> : <FieldDisagreement projectId={4} initialTaskId={8} />}
      </Provider>,
    ),
    runtime,
    Provider,
  };
};
beforeEach(() =>
  mockCallApi
    .mockReset()
    .mockImplementation(async (method, args) =>
      method === "fieldSources"
        ? sources()
        : method === "fieldCapability"
          ? { enabled: true, schema: "field-disagreements/v1", viewer: hash("e") }
          : response(args.params.left ?? 1, args.params.right ?? 2),
    ),
);
const compare = async () => {
  fireEvent.click(screen.getByTestId("field-load"));
  await screen.findAllByRole("checkbox");
  screen.getAllByRole("checkbox").forEach((box) => fireEvent.click(box));
  fireEvent.click(screen.getByTestId("field-compare"));
  await screen.findByTestId("field-detail");
};

it("accepts the independently hashed Python manifest fixture", () => {
  expect(() =>
    validateFieldReport(response(), {
      project: 4,
      task: 8,
      ids: [1, 2, 3],
      viewer: hash("e"),
      sources: sources().sources,
    }),
  ).not.toThrow();
});

it.each(["en-US", "zh-CN"])(
  "renders raw/normalized values, denominators and safe differences in %s",
  async (locale) => {
    const view = mount(locale);
    await compare();
    expect(screen.getByTestId("field-pair-1-2").textContent).toContain("50.00%");
    expect(screen.getByTestId("field-pair-1-2").textContent).toContain("0.00%");
    expect(screen.getAllByTestId("field-difference")[0].textContent).toContain(
      locale === "zh-CN" ? "仅格式不同" : "Format only",
    );
    expect(view.container.querySelector("img")).toBeNull();
    expect(mockCallApi.mock.calls[1][1].params.submissions).toBe("1,2,3");
    view.unmount();
    view.runtime.destroy();
  },
);

it("keeps pair, receipt and focus on frozen refresh", async () => {
  const view = mount();
  await compare();
  fireEvent.click(screen.getByTestId("field-pair-1-3"));
  await waitFor(() => expect(screen.getByTestId("field-detail").textContent).toContain("#3"));
  fireEvent.click(screen.getByTestId("field-read"));
  await screen.findByTestId("field-detail");
  expect(mockCallApi.mock.calls.at(-1)[1].params).toMatchObject({ frozen: "field-receipt", left: 1, right: 3 });
  fireEvent.click(screen.getByTestId("field-back-matrix"));
  expect(document.activeElement).toBe(screen.getByTestId("field-pair-1-3"));
  view.unmount();
  view.runtime.destroy();
});

it("masks source loss, retains frozen scope and recovers the original GET", async () => {
  const view = mount();
  await compare();
  mockCallApi.mockResolvedValue({
    ...response(),
    source_status: "unavailable",
    receipt_verified: false,
    result_hash: null,
    availability: "unavailable",
    reason: "source_unavailable",
    pairs: null,
    selected_pair: null,
  });
  fireEvent.click(screen.getByTestId("field-read"));
  await screen.findByTestId("field-unavailable");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  expect(screen.queryByTestId("field-matrix")).toBeNull();
  expect(view.container.textContent).toContain("Frozen inputs: 3");
  mockCallApi.mockResolvedValue(response());
  fireEvent.click(screen.getByTestId("field-read"));
  await screen.findByTestId("field-detail");
  expect(mockCallApi.mock.calls.at(-1)[1].params.frozen).toBe("field-receipt");
  view.unmount();
  view.runtime.destroy();
});

it.each([
  "scope",
  "viewer",
  "denominator",
  "ratio",
  "hash",
  "time",
  "unmasked",
  "normalization",
  "kind",
  "schema",
  "cost",
  "revoked",
])("clears old evidence after a malformed or revoked %s response", async (kind) => {
  const view = mount();
  await compare();
  const r = response();
  if (kind === "scope") r.scope.task_id = 99;
  if (kind === "viewer") r.viewer = hash("a");
  if (kind === "denominator") r.pairs[0].denominator = 1;
  if (kind === "ratio") r.pairs[0].raw_rate = ratio(1);
  if (kind === "hash") r.result_hash = hash("b");
  if (kind === "time") r.calculated_at = "different";
  if (kind === "unmasked") r.source_status = "unavailable";
  if (kind === "normalization") r.selected_pair.fields[0].left.normalized = "wrong";
  if (kind === "kind") r.selected_pair.fields[0].kind = "equal";
  if (kind === "schema") r.schema = "ner-disagreements/v1";
  if (kind === "cost") r.cost_per_pass.field_equalities++;
  mockCallApi.mockResolvedValue(kind === "revoked" ? { $meta: { ok: false } } : r);
  fireEvent.click(screen.getByTestId("field-read"));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  expect(screen.queryByTestId("field-matrix")).toBeNull();
  view.unmount();
  view.runtime.destroy();
});

it("discards late reads after blur and task change", async () => {
  const view = mount();
  await compare();
  let resolve;
  mockCallApi.mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  fireEvent.click(screen.getByTestId("field-read"));
  fireEvent(window, new Event("blur"));
  await act(async () => resolve(response()));
  expect(screen.queryByTestId("field-detail")).toBeNull();
  fireEvent.click(screen.getByTestId("field-read"));
  fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "99" } });
  await act(async () => resolve(response()));
  expect(screen.queryByTestId("field-detail")).toBeNull();
  view.unmount();
  view.runtime.destroy();
});

it("does not restore the previous project's frozen evidence after a delayed read", async () => {
  const view = mount();
  await compare();
  let resolve;
  mockCallApi.mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  fireEvent.click(screen.getByTestId("field-read"));
  view.rerender(
    <view.Provider>
      <FieldDisagreement projectId={99} initialTaskId={10} />
    </view.Provider>,
  );
  await act(async () => resolve(response()));
  expect(screen.getByRole("spinbutton").value).toBe("10");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  expect(screen.queryByTestId("field-matrix")).toBeNull();
  expect(screen.queryByTestId("field-read")).toBeNull();
  view.unmount();
  view.runtime.destroy();
});

it("selects one exact revision per participant", async () => {
  const view = mount();
  const r = sources();
  r.sources.push({ ...r.sources[0], id: 4, revision: 2 });
  mockCallApi.mockResolvedValue(r);
  fireEvent.click(screen.getByTestId("field-load"));
  await screen.findAllByRole("checkbox");
  const boxes = screen.getAllByRole("checkbox");
  fireEvent.click(boxes[0]);
  fireEvent.click(boxes[3]);
  expect(boxes[0].checked).toBe(false);
  expect(boxes[3].checked).toBe(true);
  expect(screen.getByTestId("field-compare").disabled).toBe(true);
  view.unmount();
  view.runtime.destroy();
});

it("drops late capability responses after blur", async () => {
  const view = mount("en-US", true);
  await screen.findByTestId("open-field-disagreement");
  let resolve;
  mockCallApi.mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  fireEvent(window, new Event("focus"));
  fireEvent(window, new Event("blur"));
  await act(async () => resolve({ enabled: true, schema: "field-disagreements/v1", viewer: hash("e") }));
  expect(screen.queryByTestId("open-field-disagreement")).toBeNull();
  view.unmount();
  view.runtime.destroy();
});

it.each(["revision", "assignment_id", "result_hash", "config_hash", "task_data_hash"])(
  "rejects a changed frozen manifest %s even when the claimed digest is retained",
  (key) => {
    const r = JSON.parse(JSON.stringify(response()));
    const old = response();
    r.manifest.inputs[0][key] = key.endsWith("hash") ? hash("0") : 99;
    expect(() =>
      validateFieldReport(r, {
        project: 4,
        task: 8,
        ids: [1, 2, 3],
        viewer: hash("e"),
        frozen: old.frozen,
        manifestHash: old.manifest_hash,
        time: old.calculated_at,
        resultHash: old.manifest.result_hash,
      }),
    ).toThrow();
  },
);

it.each([
  ["revision", "5cfe8d8b456b29956b6df83243f8d4f96cea68f039566d97c3895706cc57fc3f"],
  ["assignment_id", "2d107f47931179c28b11e9ccc40dc51d1ccf9d3a5656665d9debbab8ca1895c3"],
  ["result_hash", "fc54f7f895fef8872b965988352860b9ef41f667bbb44662e7677302b4edc8ad"],
  ["submitter_id", "3131dedd1f19c9b98eb10e246519ad8d6e5bf8b58b8eead4250c506a91de2b9b"],
])("rejects a correctly rehashed initial %s that differs from the selected source", async (key, digest) => {
  const view = mount();
  const r = response();
  r.manifest.inputs[0][key] = key.endsWith("hash") ? hash("0") : 99;
  if (key === "submitter_id") r.manifest.inputs[0].assignee_id = 99;
  r.manifest_hash = digest;
  mockCallApi.mockImplementation(async (method) => (method === "fieldSources" ? sources() : r));
  fireEvent.click(screen.getByTestId("field-load"));
  await screen.findAllByRole("checkbox");
  screen.getAllByRole("checkbox").forEach((box) => fireEvent.click(box));
  fireEvent.click(screen.getByTestId("field-compare"));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  expect(screen.queryByTestId("field-read")).toBeNull();
  view.unmount();
  view.runtime.destroy();
});

it.each(["\u00a0x", "x \u2028", "x \u2029", `x${" ".repeat(4093)}y`])(
  "preserves non-ASCII and bounded internal whitespace without business coercion",
  (text) => {
    const r = response();
    r.selected_pair.fields[0].left = { raw: `\t${text}`, normalized: text };
    r.selected_pair.fields[0].right = { raw: text, normalized: text };
    expect(() =>
      validateFieldReport(r, {
        project: 4,
        task: 8,
        ids: [1, 2, 3],
        viewer: hash("e"),
        sources: sources().sources,
      }),
    ).not.toThrow();
  },
);
