import { TextEncoder } from "node:util";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { FieldBatches, FieldBatchEntry } from "./FieldBatches";
import { fieldDigest } from "./fieldEvidence";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return {
    Button: React.forwardRef(({ children, look, size, ...props }, ref) =>
      React.createElement("button", { ...props, ref }, children),
    ),
  };
});
const h = (s) => s.repeat(64);
const zero = { normalized_values: 0, field_equalities: 0 };
const ops = { normalized_values: 2, field_equalities: 2 };
const process = (per = zero) => ({
  validation_passes: 2,
  per_pass: per,
  total: {
    normalized_values: per.normalized_values * 2,
    field_equalities: per.field_equalities * 2,
  },
  scorer_calls: 0,
});
const versions = {
  schema: "field-batches/v1",
  rule: "field-lf-ascii-trim/v1",
  explanation: "field-value-differences/v1",
  engine: "python-string-equality/v1",
  selection: "active-assignment-latest/v1",
  queue_rule: "field-raw-difference-queue/v1",
};
const meta = {
  ...versions,
  id: 7,
  project_id: 4,
  name: "Fields synthetic",
  viewer: h("e"),
  visible_task_count: 1,
  plan_cost_per_pass: ops,
  visibility: "project",
  manifest_hash: h("a"),
  created_at: "created",
  calculated_at: "time",
  scope_enumerated_at: "preview",
};
const capability = {
  ...versions,
  enabled: true,
  can_create: true,
  can_view_quality_overview: false,
  project_id: 4,
  viewer: h("e"),
  processing_cost: process(),
};
const counts = { agreement: 0, disagreement: 1, unavailable: 0 };
const pins = [1, 2].map((id) => ({
  submission_id: id,
  assignment_id: id + 20,
  assignee_id: id + 10,
  task_id: 8,
  revision: 1,
  submitter_id: id + 10,
  result_hash: h("b"),
  config_hash: h("c"),
  task_data_hash: h("d"),
}));
const row = {
  task_id: 8,
  outcome: "disagreement",
  original_outcome: "disagreement",
  reason: null,
  source_status: "verified",
  input_count: 2,
  total_pairs: 1,
  configured_fields: 1,
  frozen_comparable_pairs: 1,
  comparable_pairs: 1,
  has_value_difference: false,
  has_format_difference: true,
  calculation_hash: h("f"),
};
const comparison = {
  availability: "available",
  reason: null,
  input_count: 2,
  total_pairs: 1,
  configured_fields: 1,
  comparable_pairs: 1,
  unavailable_pairs: 0,
  cost_per_pass: ops,
  pairs: [
    {
      left: 1,
      right: 2,
      denominator: 1,
      excluded_empty_fields: 0,
      raw_matches: 0,
      normalized_matches: 1,
      raw_rate: { numerator: "0", denominator: "1" },
      normalized_rate: { numerator: "1", denominator: "1" },
      availability: "available",
      reason: null,
    },
  ],
  selected_pair: {
    left: 1,
    right: 2,
    fields: [
      {
        name: "code",
        informative: true,
        raw_equal: false,
        normalized_equal: true,
        kind: "format_only",
        left: { raw: " A ", normalized: "A" },
        right: { raw: "A", normalized: "A" },
      },
    ],
  },
};
const item = () => {
  const sources = JSON.parse(JSON.stringify(pins));
  const evidence = {
    schema: "field-batch-item-evidence/v1",
    project_id: 4,
    batch_id: 7,
    task_id: 8,
    viewer: h("e"),
    reference: h("a"),
    rule: versions.rule,
    explanation: versions.explanation,
    engine: versions.engine,
    queue_rule: versions.queue_rule,
    calculated_at: "time",
    cohort: sources.map((p) => ({ assignment_id: p.assignment_id, assignee_id: p.assignee_id, source: p })),
    result: {
      outcome: row.outcome,
      reason: null,
      input_count: 2,
      configured_fields: 1,
      total_pairs: 1,
      comparable_pairs: 1,
      unavailable_pairs: 0,
      has_value_difference: false,
      has_format_difference: true,
      cost_per_pass: ops,
      calculation_hash: h("f"),
    },
  };
  return {
    ...meta,
    observed_at: "now",
    processing_cost: process(ops),
    item: {
      ...row,
      inputs: sources,
      comparison: JSON.parse(JSON.stringify(comparison)),
      evidence,
      evidence_hash: fieldDigest(evidence),
    },
  };
};
const respond = async (m, o) => {
  switch (m) {
    case "fieldBatchCapability":
      return capability;
    case "fieldBatches":
      return {
        project_id: 4,
        viewer: h("e"),
        count: 1,
        next: null,
        previous: null,
        results: [meta],
        processing_cost: process(),
      };
    case "fieldBatchPreview":
      return {
        schema: versions.schema,
        project_id: 4,
        viewer: h("e"),
        preview_token: "signed",
        plan_hash: h("d"),
        task_ids: [8],
        display_counts: counts,
        scope_enumerated_at: "preview",
        processing_cost: process(ops),
      };
    case "createFieldBatch":
      return {
        ...meta,
        replayed: false,
        idempotency_key: o.headers["Idempotency-Key"],
        plan_hash: h("d"),
        processing_cost: process(ops),
      };
    case "fieldBatchDetail":
      return { ...meta, display_counts: counts, processing_cost: process(ops) };
    case "fieldBatchItems":
      return {
        ...meta,
        batch_id: 7,
        count: 1,
        next: null,
        previous: null,
        display_counts: counts,
        results: [row],
        processing_cost: process(ops),
      };
    case "fieldBatchItem":
      return item();
    default:
      throw new Error(`Forbidden transport: ${m}`);
  }
};
beforeAll(() => {
  globalThis.TextEncoder = TextEncoder;
});
beforeEach(() => mockCallApi.mockReset().mockImplementation(respond));
const mount = (locale = "en-US", entry = false) => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  return {
    ...render(<Provider>{entry ? <FieldBatchEntry projectId={4} /> : <FieldBatches projectId={4} />}</Provider>),
    runtime,
    Provider,
  };
};
const create = async (zh = false) => {
  fireEvent.click(await screen.findByRole("button", { name: zh ? "新建比较批次" : "New comparison batch" }));
  fireEvent.change(screen.getByLabelText(zh ? "批次名称" : "Batch name"), { target: { value: "Fields synthetic" } });
  fireEvent.click(screen.getByRole("button", { name: zh ? "预览比较" : "Preview comparison" }));
  fireEvent.click(await screen.findByRole("button", { name: zh ? "创建冻结批次" : "Create frozen batch" }));
};
const open = async (zh = false) => {
  fireEvent.click(await screen.findByRole("button", { name: "Fields synthetic" }));
  fireEvent.click(await screen.findByRole("button", { name: zh ? "查看冻结对照" : "View frozen comparison" }));
};

it.each(["en-US", "zh-CN"])(
  "creates and navigates a frozen field queue using only field transport in %s",
  async (locale) => {
    const { runtime, unmount } = mount(locale);
    const zh = locale === "zh-CN";
    await create(zh);
    fireEvent.click(await screen.findByRole("button", { name: zh ? "查看冻结对照" : "View frozen comparison" }));
    expect((await screen.findByTestId("field-detail")).textContent).toContain(zh ? "仅格式不同" : "Format only");
    expect(screen.getByTestId("field-matrix").textContent).toContain("100.00%");
    expect(screen.queryByTestId("classification-partial")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: zh ? "返回队列" : "Back to queue" }));
    await screen.findByLabelText(zh ? "比较状态" : "Comparison state");
    expect(screen.getByRole("combobox").value).toBe("disagreement");
    expect(mockCallApi.mock.calls.every(([m]) => m.startsWith("field") || m === "createFieldBatch")).toBe(true);
    expect(mockCallApi.mock.calls.filter(([m]) => m === "createFieldBatch")).toHaveLength(1);
    unmount();
    runtime.destroy();
  },
);

it("recovers the exact committed body and UUID without POST on read refresh", async () => {
  let attempts = 0;
  mockCallApi.mockImplementation(async (m, o) => {
    if (m === "createFieldBatch" && ++attempts === 1) throw new Error("lost response");
    const r = await respond(m, o);
    return m === "createFieldBatch" ? { ...r, replayed: true } : r;
  });
  const { runtime, unmount } = mount();
  await create();
  await screen.findByRole("button", { name: "Recover original request" });
  fireEvent.click(screen.getByRole("button", { name: "Back to batches" }));
  await screen.findByRole("button", { name: "Fields synthetic" });
  fireEvent.click(screen.getByTestId("field-batch-refresh"));
  await screen.findByRole("button", { name: "Fields synthetic" });
  expect(attempts).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Recover original request" }));
  await screen.findByText(/Original batch #7 was recovered/);
  const calls = mockCallApi.mock.calls.filter(([m]) => m === "createFieldBatch");
  expect(calls[0][1].body).toEqual(calls[1][1].body);
  expect(calls[0][1].headers).toEqual(calls[1][1].headers);
  unmount();
  runtime.destroy();
});

it.each(["evidence", "project", "values"])("clears malformed %s evidence", async (kind) => {
  mockCallApi.mockImplementation(async (m, o) => {
    const r = await respond(m, o);
    if (m === "fieldBatchItem") {
      if (kind === "evidence") r.item.evidence.cohort[0].source.revision++;
      if (kind === "project") {
        r.item.evidence.project_id = 99;
        r.item.evidence_hash = fieldDigest(r.item.evidence);
      }
      if (kind === "values") r.item.comparison.selected_pair.fields[0].left.normalized = "wrong";
    }
    return r;
  });
  const { runtime, unmount } = mount();
  await open();
  await screen.findByRole("alert");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  unmount();
  runtime.destroy();
});

it("keeps a matching-key but wrong-plan acknowledgement unknown", async () => {
  mockCallApi.mockImplementation(async (m, o) => {
    const r = await respond(m, o);
    return m === "createFieldBatch" ? { ...r, plan_hash: h("0") } : r;
  });
  const { runtime, unmount } = mount();
  await create();
  await screen.findByRole("button", { name: "Recover original request" });
  expect(screen.queryByText(/was saved/)).toBeNull();
  unmount();
  runtime.destroy();
});

it("hides pinned values when the frozen source disappears", async () => {
  const { runtime, unmount } = mount();
  await open();
  await screen.findByTestId("field-detail");
  mockCallApi.mockImplementation(async (m, o) => {
    const r = await respond(m, o);
    if (m === "fieldBatchItem")
      r.item = {
        ...r.item,
        outcome: "unavailable",
        reason: "source_unavailable",
        source_status: "unavailable",
        comparable_pairs: null,
        has_value_difference: null,
        has_format_difference: null,
        calculation_hash: null,
        inputs: [],
        evidence: null,
        evidence_hash: null,
        comparison: {
          availability: "unavailable",
          reason: "source_unavailable",
          input_count: 2,
          total_pairs: 1,
          configured_fields: 1,
          pairs: null,
          selected_pair: null,
        },
      };
    return r;
  });
  fireEvent.click(screen.getByTestId("field-batch-refresh"));
  fireEvent.click(await screen.findByRole("button", { name: "View frozen comparison" }));
  await screen.findByTestId("field-unavailable");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  unmount();
  runtime.destroy();
});

it("discards a late item after project change", async () => {
  let release;
  mockCallApi.mockImplementation((m, o) =>
    m === "fieldBatchItem"
      ? new Promise((resolve) => {
          release = () => resolve(item());
        })
      : respond(m, o),
  );
  const { runtime, unmount, rerender, Provider } = mount();
  await open();
  await waitFor(() => expect(release).toBeDefined());
  rerender(
    <Provider>
      <FieldBatches projectId={99} />
    </Provider>,
  );
  await act(async () => release());
  await screen.findByRole("alert");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  unmount();
  runtime.destroy();
});

it("hides the entry for denied capability", async () => {
  mockCallApi.mockResolvedValue({ error: true });
  const { runtime, unmount } = mount("en-US", true);
  await waitFor(() => expect(mockCallApi).toHaveBeenCalled());
  expect(screen.queryByTestId("open-field-batches")).toBeNull();
  unmount();
  runtime.destroy();
});

it("rejects a rehashed revision change within the same frozen batch", async () => {
  let reads = 0;
  mockCallApi.mockImplementation(async (m, o) => {
    const r = await respond(m, o);
    if (m === "fieldBatchItem" && ++reads > 1) {
      r.item.evidence.cohort[0].source.revision++;
      r.item.evidence_hash = fieldDigest(r.item.evidence);
    }
    return r;
  });
  const { runtime, unmount } = mount();
  await open();
  await screen.findByTestId("field-detail");
  fireEvent.click(screen.getByTestId("field-pair-1-2"));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("field-detail")).toBeNull();
  unmount();
  runtime.destroy();
});

it("does not POST a pending original intent after its viewer changes", async () => {
  let changed = false;
  let attempts = 0;
  mockCallApi.mockImplementation(async (m, o) => {
    if (m === "createFieldBatch") {
      attempts++;
      throw new Error("lost acknowledgement");
    }
    const r = await respond(m, o);
    return m === "fieldBatchCapability" && changed ? { ...r, viewer: h("c") } : r;
  });
  const { runtime, unmount } = mount();
  await create();
  await screen.findByRole("button", { name: "Recover original request" });
  changed = true;
  fireEvent.click(screen.getByRole("button", { name: "Recover original request" }));
  await screen.findByRole("button", { name: "Recover original request" });
  expect(attempts).toBe(1);
  unmount();
  runtime.destroy();
});

it.each(["comparison availability", "frozen input count", "frozen pair count", "cohort order", "assignment collision"])(
  "rejects contradictory %s even with a valid evidence digest",
  async (kind) => {
    mockCallApi.mockImplementation(async (m, o) => {
      const r = await respond(m, o);
      if (m === "fieldBatchItem") {
        if (kind === "comparison availability") {
          r.item.comparison = {
            ...r.item.comparison,
            availability: "unavailable",
            reason: "missing_field",
            pairs: null,
            selected_pair: null,
          };
        }
        if (kind === "frozen input count") r.item.evidence.result.input_count++;
        if (kind === "frozen pair count") r.item.evidence.result.unavailable_pairs++;
        if (kind === "cohort order") r.item.evidence.cohort.reverse();
        if (kind === "assignment collision") {
          const member = r.item.evidence.cohort[1];
          member.assignment_id = r.item.evidence.cohort[0].assignment_id;
          member.source.assignment_id = member.assignment_id;
        }
        r.item.evidence_hash = fieldDigest(r.item.evidence);
      }
      return r;
    });
    const { runtime, unmount } = mount();
    await open();
    await screen.findByRole("alert");
    expect(screen.queryByTestId("field-detail")).toBeNull();
    unmount();
    runtime.destroy();
  },
);

it("keeps a matching-key acknowledgement from a different viewer unknown", async () => {
  mockCallApi.mockImplementation(async (m, o) => {
    const r = await respond(m, o);
    return m === "createFieldBatch" ? { ...r, viewer: h("c") } : r;
  });
  const { runtime, unmount } = mount();
  await create();
  await screen.findByRole("button", { name: "Recover original request" });
  expect(screen.queryByText(/was saved/)).toBeNull();
  unmount();
  runtime.destroy();
});
