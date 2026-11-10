import { fireEvent, render, screen } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { BboxBatches } from "./BboxBatches";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: React.forwardRef(({ children, look, size, ...props }, ref) => React.createElement("button", { ...props, ref }, children)) };
});
const hash = (value) => value.repeat(64);
const counts = { agreement: 0, disagreement: 1, unavailable: 0 };
const meta = { id: 7, project_id: 4, viewer: hash("e"), name: "Synthetic bbox", schema: "bbox-quality-batch/v1",
  rule: "rule", selection: "selection", created_at: "created", calculated_at: "frozen", scope_enumerated_at: "preview",
  visible_task_count: 1, visibility: "project", manifest_hash: hash("a") };
const cap = { enabled: true, can_create: true, project_id: 4, viewer: hash("e"), schema: meta.schema };
const row = { task_id: 8, outcome: "disagreement", reason: null };
const pin = (id) => ({ submission_id: id, assignment_id: id, assignee_id: id + 10, task_id: 8, revision: 1,
  submitter_id: id + 10, result_hash: hash("b"), config_hash: hash("c"), task_data_hash: hash("d"),
  media: { token: "b03bb7b2-8fa0-4ebf-9e4c-0cc4d5b70406", sha256: hash("a"), proof_hash: hash("f") } });
const item = { ...row, cohort: [1, 2].map((id) => ({ assignment_id: id, assignee_id: id + 10, source: pin(id) })),
  comparison: { availability: "available", reason: null, input_count: 2,
    image: { sha256: hash("a"), width: 10, height: 20 },
    boxes: [[['Car', 0, 0, 20000000, 20000000]], [['Car', 10000000, 0, 20000000, 20000000]]],
    pairs: [{ left: 1, right: 2, f1: { numerator: "0", denominator: "1" }, matches: [], left_only: [0], right_only: [0] }] } };
const respond = async (method, options) => {
  switch (method) {
    case "bboxBatchCapability": return cap;
    case "bboxBatches": return { project_id: 4, viewer: hash("e"), count: 1, next: null, previous: null, results: [meta] };
    case "bboxBatchPreview": return { schema: meta.schema, project_id: 4, viewer: hash("e"), preview_token: "signed", plan_hash: hash("d"),
      task_ids: [8], display_counts: counts, scope_enumerated_at: "preview" };
    case "createBboxBatch": return { ...meta, replayed: false, idempotency_key: options.headers["Idempotency-Key"], plan_hash: hash("d") };
    case "bboxBatchDetail": return { ...meta, display_counts: counts };
    case "bboxBatchItems": return { ...meta, batch_id: 7, count: 1, next: null, previous: null, display_counts: counts, results: [row] };
    case "bboxBatchItem": return { ...meta, observed_at: "now", item };
    default: throw new Error(`Unexpected transport ${method}`);
  }
};
beforeEach(() => { window.APP_SETTINGS = { hostname: "" }; mockCallApi.mockReset().mockImplementation(respond); });

it.each(["en-US", "zh-CN"])("reuses the frozen batch workflow and renders the selected fixed image in %s", async (locale) => {
  const runtime = createLocaleRuntime(locale), Provider = runtime.provider, zh = locale === "zh-CN";
  const { unmount } = render(<Provider><BboxBatches projectId={4} /></Provider>);
  fireEvent.click(await screen.findByRole("button", { name: "Synthetic bbox" }));
  fireEvent.click(await screen.findByRole("button", { name: zh ? "查看冻结对照" : "View frozen comparison" }));
  const image = await screen.findByRole("img", { name: zh ? "冻结图片上的两组边界框" : "Frozen image with both sets of bounding boxes" });
  expect(image.querySelector("image").getAttribute("href")).toBe("/api/projects/4/bbox-quality/batches/7/items/8/image/");
  expect(image.querySelectorAll("rect")).toHaveLength(0);
  fireEvent.load(image.querySelector("image"));
  expect(image.querySelectorAll("rect")).toHaveLength(2);
  fireEvent.error(image.querySelector("image"));
  expect(image.querySelectorAll("rect")).toHaveLength(0);
  expect(screen.getByRole("alert")).toBeTruthy();
  expect(mockCallApi.mock.calls.every(([method]) => method.startsWith("bbox"))).toBe(true);
  unmount(); runtime.destroy();
});

it("rejects a comparison whose pair references an absent submission", async () => {
  mockCallApi.mockImplementation(async (method, options) => method === "bboxBatchItem"
    ? { ...meta, observed_at: "now", item: { ...item, comparison: { ...item.comparison,
      pairs: [{ ...item.comparison.pairs[0], right: 999 }] } } } : respond(method, options));
  const runtime = createLocaleRuntime("en-US"), Provider = runtime.provider;
  const { unmount } = render(<Provider><BboxBatches projectId={4} /></Provider>);
  fireEvent.click(await screen.findByRole("button", { name: "Synthetic bbox" }));
  fireEvent.click(await screen.findByRole("button", { name: "View frozen comparison" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("img")).toBeNull();
  unmount(); runtime.destroy();
});
