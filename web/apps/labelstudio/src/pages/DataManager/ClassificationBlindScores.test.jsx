import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ClassificationBlindScores } from "./ClassificationBlindScores";

const mockCall = jest.fn(), hash = "a".repeat(64);
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCall }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", { type: "button", ...props }, children) };
});

const row = (id) => ({ id, submission_id: id + 100, manifest_hash: hash, created_at: "now" });
beforeEach(() => {
  mockCall.mockReset().mockImplementation(async (method, { params }) => {
    if (method === "classificationBlindScores") return params.before
      ? { items: [row(2)], has_more: false, next_before: null }
      : { items: [row(3)], has_more: true, next_before: 3 };
    if (method === "classificationBlindScoreDetail") return { id: params.score, manifest_hash: hash,
      manifest: { schema: "classification-blind-score/v1", project_id: params.pk, submission_id: params.score + 100,
        revision: 1, result: { status: "scored", matched_controls: 1, total_controls: 2, controls: [] } } };
    throw new Error(method);
  });
});
const mount = (locale) => {
  const runtime = createLocaleRuntime(locale), Provider = runtime.provider;
  const view = render(<Provider><ClassificationBlindScores projectId={4} /></Provider>);
  return () => { view.unmount(); runtime.destroy(); };
};

it.each(["en-US", "zh-CN"])("loads, pages and opens a manager score in %s", async (locale) => {
  const zh = locale === "zh-CN", unmount = mount(locale);
  fireEvent.click(screen.getByRole("button", { name: zh ? "加载盲投评分" : "Load blind scores" }));
  fireEvent.click(await screen.findByRole("button", { name: zh ? "评分 #3" : "Score #3" }));
  await screen.findByText(zh ? "匹配的控件：1 / 2" : "Matched controls: 1 of 2");
  fireEvent.click(screen.getByRole("button", { name: zh ? "加载更多评分" : "Load more scores" }));
  await screen.findByRole("button", { name: zh ? "评分 #2" : "Score #2" });
  expect(mockCall).toHaveBeenCalledWith("classificationBlindScores", expect.objectContaining({ params: { pk: 4, before: 3 } }));
  expect(screen.queryByText(zh ? "匹配的控件：1 / 2" : "Matched controls: 1 of 2")).toBeNull();
  unmount();
});

it("clears displayed results after access is revoked", async () => {
  const unmount = mount("en-US");
  fireEvent.click(screen.getByRole("button", { name: "Load blind scores" }));
  fireEvent.click(await screen.findByRole("button", { name: "Score #3" }));
  await screen.findByText("Matched controls: 1 of 2");
  mockCall.mockResolvedValue({ error: true, $meta: { ok: false, status: 404 } });
  fireEvent.click(screen.getByRole("button", { name: "Load blind scores" }));
  await screen.findByRole("alert");
  expect(screen.queryByText("Matched controls: 1 of 2")).toBeNull();
  expect(screen.queryByRole("button", { name: "Score #3" })).toBeNull();
  unmount();
});

it("rejects a detail from another project", async () => {
  const unmount = mount("en-US");
  fireEvent.click(screen.getByRole("button", { name: "Load blind scores" }));
  await screen.findByRole("button", { name: "Score #3" });
  mockCall.mockResolvedValue({ id: 3, manifest_hash: hash,
    manifest: { schema: "classification-blind-score/v1", project_id: 5, submission_id: 103, revision: 1,
      result: { status: "scored", matched_controls: 1, total_controls: 2, controls: [] } } });
  fireEvent.click(screen.getByRole("button", { name: "Score #3" }));
  await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  expect(screen.queryByText("Matched controls: 1 of 2")).toBeNull();
  unmount();
});

it("discards a delayed response after switching projects", async () => {
  let resolve;
  mockCall.mockReturnValue(new Promise((done) => { resolve = done; }));
  const runtime = createLocaleRuntime("en-US"), Provider = runtime.provider;
  const view = render(<Provider><ClassificationBlindScores projectId={4} /></Provider>);
  fireEvent.click(screen.getByRole("button", { name: "Load blind scores" }));
  view.rerender(<Provider><ClassificationBlindScores projectId={5} /></Provider>);
  await act(async () => { resolve({ items: [row(3)], has_more: false, next_before: null }); });
  expect(screen.queryByRole("button", { name: "Score #3" })).toBeNull();
  view.unmount(); runtime.destroy();
});
