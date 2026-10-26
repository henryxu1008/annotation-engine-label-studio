import { act, fireEvent, render, screen } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ClassificationPartial, partialPercent } from "./ClassificationPartial";
import { ClassificationResult } from "./ClassificationDisagreement";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
const inputs = [1, 2].map((id) => ({ submission_id: id, revision: 1, submitter_id: id + 10, result_hash: `hash-${id}`, config_hash: "config" }));
const report = { manifest_hash: "base-hash", manifest: { rule: "classification-exact-sets/v1", calculated_at: "base-time", inputs },
  observed_at: "now", status: "comparable", reason: null, matching_pairs: 0, total_pairs: 1, agreement: 0,
  locations: [{ from_name: "sentiment", to_name: "text", matching_pairs: 0, total_pairs: 1, agreement: 0, disagrees: true,
    values: [{ submission_id: 1, choices: ["A", "B"] }, { submission_id: 2, choices: ["B", "C"] }] }] };
const response = (batch = false) => {
  const origin = { kind: batch ? "batch" : "manual", project_id: 4, task_id: 8, ...(batch ? { batch_id: 6 } : {}),
    rule: "classification-exact-sets/v1", calculated_at: "base-time", origin_hash: "base-hash" };
  return { schema: "classification-partial-match/v1", rule: "classification-jaccard-nonempty/v1", viewer: "a".repeat(64),
    origin, base: report, frozen: "partial-receipt", observed_at: "read-time",
    receipt: { schema: "classification-partial-match/v1", rule: "classification-jaccard-nonempty/v1",
      explained_at: "explain-time", result_hash: "result-hash", origin },
    partial: { status: "comparable", reason: null, score: { numerator: "1", denominator: "3" },
      controls: [{ from_name: "sentiment", to_name: "text", score: { numerator: "1", denominator: "3" }, pairs: [{
        left_submission_id: 1, right_submission_id: 2, intersection_count: 1, union_count: 3,
        shared: ["B"], only_left: ["A"], only_right: ["C"],
      }] }] } };
};
const mount = (locale = "en-US", batch = false) => {
  const runtime = createLocaleRuntime(locale), Provider = runtime.provider, failed = jest.fn();
  const props = { projectId: 4, taskId: 8, ...(batch ? { batchId: 6 } : { sourceFrozen: "base-receipt" }),
    enabled: true, report, onFailure: failed };
  const view = render(<Provider><ClassificationPartial {...props}>
    {(value) => <ClassificationResult report={value} />}
  </ClassificationPartial></Provider>);
  return { ...view, runtime, failed, Provider, props };
};
beforeEach(() => mockCallApi.mockReset().mockResolvedValue(response()));

it.each(["en-US", "zh-CN"])("explains differences and retains exact agreement in %s", async (locale) => {
  const view = mount(locale);
  fireEvent.click(screen.getByTestId("classification-partial-load"));
  await screen.findByTestId("classification-partial-result");
  expect(screen.getByTestId("classification-partial-score").textContent).toContain("33.33%");
  expect(screen.getByTestId("classification-agreement").textContent).toContain("0.00%");
  expect(screen.getByTestId("classification-partial-pair").textContent).toContain(locale === "zh-CN" ? "共同标签: B" : "Shared labels: B");
  expect(mockCallApi.mock.calls[0][1].params).toEqual({ pk: 4, task: 8, source_frozen: "base-receipt" });
  fireEvent.click(screen.getByTestId("classification-partial-load"));
  await screen.findByTestId("classification-partial-result");
  expect(mockCallApi.mock.calls[1][1].params).toEqual({ pk: 4, task: 8, frozen: "partial-receipt" });
  expect(mockCallApi.mock.calls.every(([method]) => method === "classificationPartial")).toBe(true);
  view.unmount(); view.runtime.destroy();
});

it("uses only the frozen batch transport and no creation command", async () => {
  mockCallApi.mockResolvedValue(response(true));
  const view = mount("en-US", true);
  fireEvent.click(screen.getByTestId("classification-partial-load"));
  await screen.findByTestId("classification-partial-result");
  expect(mockCallApi.mock.calls[0]).toEqual(["classificationBatchPartial", expect.objectContaining({ params: { pk: 4, task: 8, batch: 6 } })]);
  view.unmount(); view.runtime.destroy();
});

it("masks unavailable sources without a score or answer fallback", async () => {
  const data = response();
  data.base = { ...report, status: "unavailable", reason: "source_unavailable", agreement: null, matching_pairs: null, total_pairs: null, locations: [] };
  data.partial = { status: "unavailable", reason: "source_unavailable", score: null, controls: [] };
  mockCallApi.mockResolvedValue(data);
  const view = mount();
  fireEvent.click(screen.getByTestId("classification-partial-load"));
  await screen.findByTestId("classification-partial-result");
  expect(screen.queryByTestId("classification-agreement")).toBeNull();
  expect(screen.queryByTestId("classification-partial-score")).toBeNull();
  expect(screen.queryByTestId("classification-location")).toBeNull();
  view.unmount(); view.runtime.destroy();
});

it.each(["wrong scope", "malformed", "denied", "budget"])("clears joint evidence for %s", async (mode) => {
  const data = response();
  if (mode === "wrong scope") data.origin.task_id = 9;
  if (mode === "malformed") data.partial.score.denominator = "0";
  mockCallApi.mockResolvedValue(["denied", "budget"].includes(mode) ? { $meta: { ok: false, status: mode === "denied" ? 403 : 422 } } : data);
  const view = mount();
  fireEvent.click(screen.getByTestId("classification-partial-load"));
  await screen.findByRole("alert");
  expect(view.failed).toHaveBeenCalledTimes(1);
  expect(screen.queryByTestId("classification-report")).toBeNull();
  expect(screen.queryByTestId("classification-partial-result")).toBeNull();
  view.unmount(); view.runtime.destroy();
});

it("discards late answers after blur and permits an explicit GET retry", async () => {
  let finish;
  mockCallApi.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const view = mount();
  fireEvent.click(screen.getByTestId("classification-partial-load"));
  fireEvent(window, new Event("blur"));
  await act(async () => finish(response()));
  expect(screen.queryByTestId("classification-report")).toBeNull();
  expect(screen.queryByTestId("classification-partial-result")).toBeNull();
  fireEvent.click(screen.getByTestId("classification-partial-load"));
  await screen.findByTestId("classification-partial-result");
  view.unmount(); view.runtime.destroy();
});

it("rounds only presentation with safe exact integers", () => {
  expect(partialPercent({ numerator: "4", denominator: "9" })).toBe("44.44%");
  expect(partialPercent({ numerator: "1", denominator: "32" })).toBe("3.13%");
  expect(partialPercent({ numerator: "100000000000000000000", denominator: "300000000000000000000" })).toBe("33.33%");
});
