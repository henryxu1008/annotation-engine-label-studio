import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ReworkComparison, snapshotDiff } from "./ReworkComparison";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
const row = (id, revision, answer, status = "pending") => ({
  id, revision, status, result_hash: `hash-${id}`,
  result_snapshot: { annotation: { result: [{ value: answer }] }, task: { id: 5 }, project: { id: 4 } },
  review: status === "rejected" ? { id: 10 + id, reason: "Fix 修正", reviewer: { id: 8 }, created_at: "2026-10-23T10:00:00Z" } : null,
});
const response = () => ({ results: { source: row(1, 1, "A", "rejected"),
  revisions: [row(3, 3, "B"), row(2, 2, "A", "rejected")] }, next: null });
const mount = (locale = "en-US", props = {}) => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const view = render(<Provider><ReworkComparison projectId={4} batchId={7} sourceId={1} onClose={() => {}} {...props} /></Provider>);
  return { ...view, runtime, Provider };
};
beforeEach(() => mockCallApi.mockReset().mockImplementation(async () => response()));

it.each(["en-US", "zh-CN"])("compares multiple reworks and explicitly marks unchanged answers in %s", async (locale) => {
  const zh = locale === "zh-CN";
  const { unmount, runtime } = mount(locale);
  const select = await screen.findByLabelText(zh ? "选择对照修订" : "Compare with revision");
  expect(screen.getByTestId("rework-original").textContent).toContain("hash-1");
  expect(screen.getByTestId("rework-selected").textContent).toContain("hash-3");
  expect(screen.getByTestId("rework-diff").textContent).toContain('+');
  fireEvent.change(select, { target: { value: "2" } });
  expect(screen.getByText(zh ? "标注结果无变化。新修订不代表已完成纠正。" : "Annotation results unchanged. A new revision does not prove a correction.")).toBeTruthy();
  expect(screen.getByTestId("rework-selected").textContent).toContain("hash-2");
  expect(mockCallApi.mock.calls.every(([method]) => method === "reviewBatchRework")).toBe(true);
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("clears evidence on revoked or unavailable read, retries only GET in %s", async (locale) => {
  const zh = locale === "zh-CN";
  const { unmount, runtime } = mount(locale);
  await screen.findByTestId("rework-original");
  mockCallApi.mockResolvedValue({ error: true, $meta: { status: 403 } });
  fireEvent.click(screen.getByRole("button", { name: zh ? "刷新对照" : "Refresh comparison" }));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("rework-original")).toBeNull();
  expect(screen.queryByTestId("rework-selected")).toBeNull();
  mockCallApi.mockResolvedValue(response());
  fireEvent.click(screen.getByRole("button", { name: zh ? "刷新对照" : "Refresh comparison" }));
  await screen.findByTestId("rework-original");
  expect(mockCallApi.mock.calls).toHaveLength(3);
  unmount(); runtime.destroy();
});

it("ignores late source responses after context changes", async () => {
  let resolve;
  mockCallApi.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
  const { rerender, Provider, unmount, runtime } = mount();
  rerender(<Provider><ReworkComparison projectId={4} batchId={8} sourceId={9} onClose={() => {}} /></Provider>);
  await screen.findByTestId("rework-original");
  await act(async () => resolve({ results: { source: row(99, 1, "STALE"), revisions: [] } }));
  expect(screen.getByTestId("rework-original").textContent).not.toContain("hash-99");
  unmount(); runtime.destroy();
});

it("clears on blur and cancels a pending response", async () => {
  let resolve;
  mockCallApi.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
  const { unmount, runtime } = mount();
  fireEvent(window, new Event("blur"));
  await act(async () => resolve(response()));
  expect(screen.queryByTestId("rework-original")).toBeNull();
  expect(screen.getByRole("button", { name: "Refresh comparison" }).disabled).toBe(false);
  unmount(); runtime.destroy();
});

it("pages revision history rather than silently truncating it", async () => {
  mockCallApi.mockResolvedValueOnce({ ...response(), next: "?page=2" });
  const { unmount, runtime } = mount();
  await screen.findByTestId("rework-original");
  fireEvent.click(screen.getByRole("button", { name: "Older revisions" }));
  await waitFor(() => expect(mockCallApi.mock.calls[1][1].params.page).toBe(2));
  await screen.findByTestId("rework-original");
  unmount(); runtime.destroy();
});

it("ignores object key order without ignoring array order or changed values", () => {
  expect(snapshotDiff({ a: 1, b: 2 }, { b: 2, a: 1 }).some((p) => p.added || p.removed)).toBe(false);
  expect(snapshotDiff([1, 2], [2, 1]).some((p) => p.added || p.removed)).toBe(true);
});

it.each(["en-US", "zh-CN"])("a pre-blur response cannot replace a later refresh in %s", async (locale) => {
  let resolveOld;
  let resolveNew;
  mockCallApi.mockImplementationOnce(() => new Promise((done) => { resolveOld = done; }))
    .mockImplementationOnce(() => new Promise((done) => { resolveNew = done; }));
  const { unmount, runtime } = mount(locale);
  fireEvent(window, new Event("blur"));
  fireEvent.click(screen.getByRole("button", { name: locale === "zh-CN" ? "刷新对照" : "Refresh comparison" }));
  await act(async () => resolveNew(response()));
  await screen.findByTestId("rework-original");
  await act(async () => resolveOld({ error: true, $meta: { status: 403 } }));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByTestId("rework-selected").textContent).toContain("hash-3");
  unmount(); runtime.destroy();
});
