import { act, fireEvent, render, screen } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { FieldQualityOverview } from "./FieldQualityOverview";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
const f = (n, d = 1) => ({ numerator: String(n), denominator: String(d) });
const h = (s) => s.repeat(64);
const reasons = [
  { reason: "missing_submission", count: 1 },
  { reason: "no_values", count: 1 },
];
const batch = {
  id: 6,
  viewer: h("c"),
  visibility: "project",
  manifest_hash: h("a"),
  calculated_at: "batch-time",
  visible_task_count: 4,
  plan_cost_per_pass: { normalized_values: 21, field_equalities: 30 },
};
const person = (actor_id) => ({
  actor_id,
  tasks: 4,
  comparable_tasks: 2,
  unavailable_tasks: 2,
  peer_pairs: 5,
  comparable_peer_pairs: 3,
  unavailable_peer_pairs: 2,
  unavailable_reasons: reasons,
  unavailable_peer_pair_reasons: reasons,
  task_coverage: f(1, 2),
  pair_coverage: f(3, 5),
  raw_agreement: f(5, 8),
  normalized_agreement: f(3, 4),
  task_ids: [8, 9, 10, 11],
});
const response = () => ({
  schema: "field-quality-overview/v1",
  rules: {
    aggregation: "field-batch-quality-task-macro/v1",
    normalization: "field-lf-ascii-trim/v1",
    explanation: "field-value-differences/v1",
    engine: "python-string-equality/v1",
    selection: "active-assignment-latest/v1",
    queue: "field-raw-difference-queue/v1",
  },
  scope: {
    kind: "batch",
    organization_id: 1,
    project_id: 4,
    batch_id: 6,
    visibility: "project",
    reference_kind: "manifest_hash",
    reference_hash: h("a"),
  },
  source_set_hash: h("b"),
  viewer: h("c"),
  scope_counts: {
    tasks: 4,
    comparable_tasks: 2,
    unavailable_tasks: 2,
    unavailable_reasons: reasons,
    total_pairs: 6,
    comparable_pairs: 4,
    unavailable_pairs: 2,
    unavailable_pair_reasons: reasons,
  },
  source_status: "verified",
  current_source_issues: { task_count: 0, reasons: [] },
  availability: "available",
  reason: null,
  summary: {
    task_coverage: f(1, 2),
    pair_coverage: f(2, 3),
    raw_agreement: f(5, 12),
    normalized_agreement: f(7, 12),
    agreement_tasks: 1,
    disagreement_tasks: 1,
  },
  annotators: [person(11), person(12)],
  pagination: { count: 2, page: 1, page_size: 50, next_page: null, previous_page: null },
  processing_cost: {
    validation_passes: 2,
    scorer_calls: 0,
    per_pass: { normalized_values: 21, field_equalities: 30, aggregation_pair_visits: 5, annotator_pair_updates: 8 },
    total: { normalized_values: 42, field_equalities: 60, aggregation_pair_visits: 10, annotator_pair_updates: 16 },
  },
  frozen: "field-overview-receipt",
  result_hash: h("d"),
  receipt_verified: true,
  batch_calculated_at: "batch-time",
  summarized_at: "first-time",
  observed_at: "now",
});
const mount = (locale = "en-US", navigation = { current: {} }, currentBatch = batch) => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const onOpenTask = jest.fn();
  const props = { projectId: 4, batch: currentBatch, navigation, onOpenTask };
  return {
    ...render(
      <Provider>
        <FieldQualityOverview {...props} />
      </Provider>,
    ),
    runtime,
    Provider,
    props,
  };
};
beforeEach(() => mockCallApi.mockReset().mockResolvedValue(response()));

it("accepts the core API proxy non-enumerable transport metadata without widening the JSON schema", async () => {
  const data = response();
  Object.defineProperty(data, "$meta", { value: { ok: true, status: 200 }, enumerable: false });
  mockCallApi.mockResolvedValue(data);
  const v = mount();
  fireEvent.click(screen.getByTestId("field-quality-read"));
  await screen.findByTestId("field-quality-summary");
  v.unmount();
  v.runtime.destroy();
});

it.each(["en-US", "zh-CN"])(
  "shows distinct field task and pair denominators, peer weights and readonly replay in %s",
  async (locale) => {
    const v = mount(locale);
    fireEvent.click(screen.getByTestId("field-quality-read"));
    expect((await screen.findByTestId("field-quality-summary")).textContent).toContain("58.33%");
    expect(screen.getByTestId("field-quality-summary").textContent).toContain("66.67%");
    expect(v.container.querySelector('[data-quality-actor="11"]').textContent).toContain("62.50%");
    expect(v.container.textContent).toContain(locale === "zh-CN" ? "没有可评分的有信息值" : "no informative values");
    fireEvent.click(v.container.querySelector('[data-quality-task="8"]'));
    expect(v.props.onOpenTask).toHaveBeenCalledWith(8);
    fireEvent.click(screen.getByTestId("field-quality-read"));
    await screen.findByTestId("field-quality-summary");
    expect(mockCallApi.mock.calls[1][1].params.frozen).toBe("field-overview-receipt");
    expect(mockCallApi.mock.calls.every(([m]) => m === "fieldQualityOverview")).toBe(true);
    v.unmount();
    v.runtime.destroy();
  },
);

it("masks the whole overview on source failure, retains the old receipt and recovers exactly", async () => {
  const v = mount();
  fireEvent.click(screen.getByTestId("field-quality-read"));
  await screen.findByTestId("field-quality-summary");
  mockCallApi.mockResolvedValue({
    ...response(),
    source_status: "unavailable",
    availability: "unavailable",
    reason: "source_unavailable",
    summary: null,
    annotators: null,
    pagination: null,
    result_hash: null,
    receipt_verified: false,
    current_source_issues: { task_count: 1, reasons: [{ reason: "source_unavailable", count: 1 }] },
    processing_cost: {
      validation_passes: 2,
      scorer_calls: 0,
      per_pass: { normalized_values: 21, field_equalities: 30, aggregation_pair_visits: 0, annotator_pair_updates: 0 },
      total: { normalized_values: 42, field_equalities: 60, aggregation_pair_visits: 0, annotator_pair_updates: 0 },
    },
  });
  fireEvent.click(screen.getByTestId("field-quality-read"));
  await screen.findByText(/denominator is unchanged/);
  expect(screen.queryByTestId("field-quality-summary")).toBeNull();
  expect(v.container.querySelector("[data-quality-task]")).toBeNull();
  expect(v.props.navigation.current.frozen).toBe("field-overview-receipt");
  mockCallApi.mockResolvedValue(response());
  fireEvent.click(screen.getByTestId("field-quality-read"));
  await screen.findByTestId("field-quality-summary");
  expect(mockCallApi.mock.calls[2][1].params.frozen).toBe("field-overview-receipt");
  v.unmount();
  v.runtime.destroy();
});

it.each(["en-US", "zh-CN"])(
  "labels unavailable tasks and undefined pairs with distinct units in %s",
  async (locale) => {
    const data = response();
    data.scope_counts = {
      tasks: 1,
      comparable_tasks: 0,
      unavailable_tasks: 1,
      unavailable_reasons: [{ reason: "no_values", count: 1 }],
      total_pairs: 190,
      comparable_pairs: 0,
      unavailable_pairs: 190,
      unavailable_pair_reasons: [{ reason: "no_values", count: 190 }],
    };
    data.summary = {
      task_coverage: f(0),
      pair_coverage: f(0),
      raw_agreement: null,
      normalized_agreement: null,
      agreement_tasks: 0,
      disagreement_tasks: 0,
    };
    data.availability = "unavailable";
    data.reason = "no_comparable_tasks";
    data.annotators = Array.from({ length: 20 }, (_, index) => ({
      actor_id: index + 1,
      tasks: 1,
      comparable_tasks: 0,
      unavailable_tasks: 1,
      peer_pairs: 19,
      comparable_peer_pairs: 0,
      unavailable_peer_pairs: 19,
      task_ids: [8],
      task_coverage: f(0),
      pair_coverage: f(0),
      raw_agreement: null,
      normalized_agreement: null,
      unavailable_reasons: [{ reason: "no_values", count: 1 }],
      unavailable_peer_pair_reasons: [{ reason: "no_values", count: 19 }],
    }));
    data.pagination.count = 20;
    data.processing_cost = {
      validation_passes: 2,
      scorer_calls: 0,
      per_pass: {
        normalized_values: 20,
        field_equalities: 380,
        aggregation_pair_visits: 190,
        annotator_pair_updates: 0,
      },
      total: { normalized_values: 40, field_equalities: 760, aggregation_pair_visits: 380, annotator_pair_updates: 0 },
    };
    mockCallApi.mockResolvedValue(data);
    const v = mount(locale, undefined, {
      ...batch,
      visible_task_count: 1,
      plan_cost_per_pass: { normalized_values: 20, field_equalities: 380 },
    });
    fireEvent.click(screen.getByTestId("field-quality-read"));
    const taskReasons = await screen.findByTestId("field-quality-task-reasons");
    const pairReasons = screen.getByTestId("field-quality-pair-reasons");
    expect(taskReasons.textContent).toContain(
      locale === "zh-CN" ? "冻结不可比较任务原因" : "Frozen unavailable task reasons",
    );
    expect(pairReasons.textContent).toContain(
      locale === "zh-CN" ? "冻结无分数配对原因" : "Frozen undefined pair reasons",
    );
    expect(taskReasons.textContent).toMatch(/× 1$/);
    expect(pairReasons.textContent).toMatch(/× 190$/);
    expect(screen.getByTestId("field-quality-summary").textContent).not.toContain("100.00%");
    v.unmount();
    v.runtime.destroy();
  },
);

it.each(["viewer", "pairs", "coverage", "person", "schema", "source", "receipt"])(
  "rejects mismatched %s without displaying evidence",
  async (kind) => {
    const data = response();
    if (kind === "viewer") data.viewer = h("e");
    if (kind === "pairs") data.scope_counts.total_pairs = 8;
    if (kind === "coverage") data.summary.pair_coverage = f(1);
    if (kind === "person") data.annotators[0].comparable_peer_pairs = 6;
    if (kind === "schema") data.schema = "classification-quality-overview/v1";
    if (kind === "source") data.source_status = "unavailable";
    if (kind === "receipt") data.receipt_verified = false;
    mockCallApi.mockResolvedValue(data);
    const v = mount();
    fireEvent.click(screen.getByTestId("field-quality-read"));
    await screen.findByRole("alert");
    expect(screen.queryByTestId("field-quality-summary")).toBeNull();
    expect(v.container.querySelector("[data-quality-task]")).toBeNull();
    v.unmount();
    v.runtime.destroy();
  },
);

it("keeps all-empty scores null and a zero pair denominator undefined", async () => {
  const data = response();
  data.scope_counts = {
    tasks: 4,
    comparable_tasks: 0,
    unavailable_tasks: 4,
    unavailable_reasons: [{ reason: "insufficient_inputs", count: 4 }],
    total_pairs: 0,
    comparable_pairs: 0,
    unavailable_pairs: 0,
    unavailable_pair_reasons: [],
  };
  data.summary = {
    task_coverage: f(0),
    pair_coverage: null,
    raw_agreement: null,
    normalized_agreement: null,
    agreement_tasks: 0,
    disagreement_tasks: 0,
  };
  data.availability = "unavailable";
  data.reason = "no_comparable_tasks";
  data.annotators = [];
  data.pagination.count = 0;
  data.processing_cost = {
    validation_passes: 2,
    scorer_calls: 0,
    per_pass: { normalized_values: 0, field_equalities: 0, aggregation_pair_visits: 0, annotator_pair_updates: 0 },
    total: { normalized_values: 0, field_equalities: 0, aggregation_pair_visits: 0, annotator_pair_updates: 0 },
  };
  mockCallApi.mockResolvedValue(data);
  const v = mount("en-US", undefined, { ...batch, plan_cost_per_pass: { normalized_values: 0, field_equalities: 0 } });
  fireEvent.click(screen.getByTestId("field-quality-read"));
  await screen.findByTestId("field-quality-summary");
  expect(v.container.textContent).not.toContain("100.00%");
  expect(v.container.textContent).not.toContain("NaN");
  v.unmount();
  v.runtime.destroy();
});

it("discards delayed answers after blur or viewer change", async () => {
  let release;
  mockCallApi.mockImplementation(
    () =>
      new Promise((r) => {
        release = r;
      }),
  );
  const v = mount();
  fireEvent.click(screen.getByTestId("field-quality-read"));
  fireEvent.blur(window);
  await act(async () => release(response()));
  expect(screen.queryByTestId("field-quality-summary")).toBeNull();
  mockCallApi.mockResolvedValue(response());
  v.rerender(
    <v.Provider>
      <FieldQualityOverview {...v.props} batch={{ ...batch, viewer: h("e") }} />
    </v.Provider>,
  );
  fireEvent.click(screen.getByTestId("field-quality-read"));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("field-quality-summary")).toBeNull();
  v.unmount();
  v.runtime.destroy();
});

it.each(["viewer", "batch"])("ignores an old %s response while a new context is still loading", async (changed) => {
  const replies = [];
  mockCallApi.mockImplementation(() => new Promise((resolve) => replies.push(resolve)));
  const v = mount();
  fireEvent.click(screen.getByTestId("field-quality-read"));
  const nextBatch =
    changed === "viewer"
      ? { ...batch, viewer: h("e") }
      : { ...batch, id: 7, manifest_hash: h("e"), calculated_at: "next-batch-time" };
  v.rerender(
    <v.Provider>
      <FieldQualityOverview {...v.props} batch={nextBatch} />
    </v.Provider>,
  );
  fireEvent.click(screen.getByTestId("field-quality-read"));
  expect(replies).toHaveLength(2);
  await act(async () => replies[0](response()));
  expect(screen.queryByTestId("field-quality-summary")).toBeNull();
  expect(screen.getByTestId("field-quality-overview").getAttribute("aria-busy")).toBe("true");
  const next = response();
  next.scope.batch_id = nextBatch.id;
  next.scope.reference_hash = nextBatch.manifest_hash;
  next.viewer = nextBatch.viewer;
  next.batch_calculated_at = nextBatch.calculated_at;
  next.frozen = "next-context-receipt";
  next.result_hash = h("f");
  next.annotators = [person(21), person(22)];
  await act(async () => replies[1](next));
  expect((await screen.findByTestId("field-quality-summary")).textContent).toContain("58.33%");
  expect(v.container.querySelector('[data-quality-actor="11"]')).toBeNull();
  expect(v.container.querySelector('[data-quality-actor="21"]')).not.toBeNull();
  expect(v.props.navigation.current.frozen).toBe("next-context-receipt");
  expect(v.props.navigation.current.viewer).toBe(nextBatch.viewer);
  v.unmount();
  v.runtime.destroy();
});

it.each([403, 404, 409, 422, 503])("clears prior scores on HTTP %s and never issues a write", async (status) => {
  const v = mount();
  fireEvent.click(screen.getByTestId("field-quality-read"));
  await screen.findByTestId("field-quality-summary");
  mockCallApi.mockResolvedValue({ $meta: { ok: false, status } });
  fireEvent.click(screen.getByTestId("field-quality-read"));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("field-quality-summary")).toBeNull();
  expect(mockCallApi.mock.calls.every(([m]) => m === "fieldQualityOverview")).toBe(true);
  v.unmount();
  v.runtime.destroy();
});

it.each(["cost", "raw-order", "normalized-missing", "pair-visits", "actor-updates", "old-rule", "unknown-field"])(
  "rejects inconsistent field evidence: %s",
  async (kind) => {
    const data = response();
    if (kind === "cost") data.processing_cost.total.normalized_values++;
    if (kind === "raw-order") data.summary.raw_agreement = f(1);
    if (kind === "normalized-missing") data.summary.normalized_agreement = null;
    if (kind === "pair-visits") data.processing_cost.per_pass.aggregation_pair_visits = 0;
    if (kind === "actor-updates") data.processing_cost.per_pass.annotator_pair_updates = 6;
    if (kind === "old-rule") data.rules.normalization = "field-other/v0";
    if (kind === "unknown-field") data.unexpected = true;
    mockCallApi.mockResolvedValue(data);
    const v = mount();
    fireEvent.click(screen.getByTestId("field-quality-read"));
    await screen.findByRole("alert");
    expect(screen.queryByTestId("field-quality-summary")).toBeNull();
    v.unmount();
    v.runtime.destroy();
  },
);
