import { fireEvent, render, screen } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ClassificationGold, ClassificationGoldEntry } from "./ClassificationGold";

const mockCall = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCall }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", { type: "button", ...props }, children) };
});
const manifest = (kind) => ({ schema: "classification-gold-evidence/v1", project_id: 4, kind, contents: { result: "Synthetic" } });
let rows;
beforeEach(() => {
  rows = [];
  mockCall.mockReset().mockImplementation(async (method, options) => {
    if (method === "classificationGold") return { items: rows, has_more: false };
    if (method === "classificationGoldSources") return { task_id: 8, sources: [
      { id: 11, revision: 1, submitted_by_id: 2, result_hash: "a".repeat(64) },
      { id: 12, revision: 1, submitted_by_id: 3, result_hash: "b".repeat(64) },
    ] };
    if (method === "createClassificationGold") {
      const result = { id: options.body.kind === "reference" ? 7 : 9, manifest: manifest(options.body.kind), manifest_hash: "a".repeat(64) };
      rows = [...rows, { id: result.id, kind: options.body.kind, created_at: "now", manifest_hash: "a".repeat(64) }];
      return result;
    }
    if (method === "classificationGoldDetail") return { id: options.params.evidence, manifest: manifest("reference"), manifest_hash: "a".repeat(64) };
    throw new Error(method);
  });
});
const mount = (locale, entry = false) => {
  const runtime = createLocaleRuntime(locale), Provider = runtime.provider;
  const view = render(<Provider>{entry ? <ClassificationGoldEntry projectId={4} /> : <ClassificationGold projectId={4} />}</Provider>);
  return () => { view.unmount(); runtime.destroy(); };
};

it.each(["en-US", "zh-CN"])("creates a reference and scores an independent revision in %s", async (locale) => {
  const zh = locale === "zh-CN", unmount = mount(locale);
  fireEvent.change(screen.getByLabelText(zh ? "任务 ID" : "Task ID"), { target: { value: "8" } });
  fireEvent.click(screen.getByRole("button", { name: zh ? "加载修订" : "Load revisions" }));
  await screen.findByRole("option", { name: /#11/ });
  fireEvent.change(screen.getByLabelText(zh ? "提交修订" : "Submission revision"), { target: { value: "11" } });
  fireEvent.click(screen.getByRole("button", { name: zh ? "创建参考答案版本" : "Create reference version" }));
  await screen.findByRole("option", { name: "#7" });
  fireEvent.change(screen.getByLabelText(zh ? "黄金参考答案" : "Gold reference"), { target: { value: "7" } });
  fireEvent.change(screen.getByLabelText(zh ? "提交修订" : "Submission revision"), { target: { value: "12" } });
  fireEvent.click(screen.getByRole("button", { name: zh ? "评分修订" : "Evaluate revision" }));
  await screen.findByRole("heading", { name: zh ? "证据 #9" : "Evidence #9" });
  const writes = mockCall.mock.calls.filter(([method]) => method === "createClassificationGold");
  expect(writes[1][1].body).toEqual({ kind: "score", submission_id: 12, reference_id: 7,
    expected_result_hash: "b".repeat(64), expected_reference_hash: "a".repeat(64) });
  unmount();
});

it("hides the entry when the server rejects access", async () => {
  mockCall.mockResolvedValue({ error: true, $meta: { ok: false, status: 404 } });
  const unmount = mount("en-US", true);
  await Promise.resolve();
  expect(screen.queryByTestId("open-classification-gold")).toBeNull();
  unmount();
});
