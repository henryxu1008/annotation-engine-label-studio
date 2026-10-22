import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ReleaseSnapshots } from "./ReleaseSnapshots";
import { downloadFile } from "../../utils/download";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("../../utils/download", () => ({ downloadFile: jest.fn() }));
const snapshot = { snapshot_id: "source-snapshot", state: "ready", content_digest: "a".repeat(64), assets: [] };
const raw = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });
const defaults = (name) => Promise.resolve(raw(name === "releaseSnapshotListRaw" ? { results: [snapshot] } : snapshot));
const mount = (locale = "en-US") => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  return { runtime, Provider, ...render(<Provider><ReleaseSnapshots projectId={4} releaseId="release" /></Provider>) };
};
beforeEach(() => { mockCallApi.mockReset(); downloadFile.mockReset(); });

it("reuses the same request key and keeps Host publication separate", async () => {
  mockCallApi.mockImplementation(defaults);
  const { unmount, runtime } = mount();
  fireEvent.click(await screen.findByTestId("snapshot-create"));
  await screen.findByTestId("snapshot-manifest");
  fireEvent.click(screen.getByTestId("snapshot-create"));
  await waitFor(() => expect(mockCallApi.mock.calls.filter(([name]) => name === "releaseSnapshotCreateRaw")).toHaveLength(2));
  const calls = mockCallApi.mock.calls.filter(([name]) => name === "releaseSnapshotCreateRaw");
  expect(calls[0][1].headers["Idempotency-Key"]).toBe(calls[1][1].headers["Idempotency-Key"]);
  expect(screen.getByText(/Host dataset version, review/)).not.toBeNull();
  unmount(); runtime.destroy();
});

it("does not download a late body after changing the project", async () => {
  let complete;
  mockCallApi.mockImplementation((name) => name === "releaseSnapshotManifestRaw"
    ? Promise.resolve({ ok: true, blob: () => new Promise((resolve) => { complete = resolve; }) }) : defaults(name));
  const { Provider, rerender, unmount, runtime } = mount();
  fireEvent.click(await screen.findByTestId("snapshot-create"));
  fireEvent.click(await screen.findByTestId("snapshot-manifest"));
  await waitFor(() => expect(complete).toBeDefined());
  rerender(<Provider><ReleaseSnapshots projectId={5} releaseId="another-release" /></Provider>);
  await act(async () => complete(new Blob(["stale content"])));
  expect(downloadFile).not.toHaveBeenCalled();
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("clears old download controls after denial in %s", async (locale) => {
  mockCallApi.mockImplementation((name) => name === "releaseSnapshotManifestRaw"
    ? Promise.resolve(raw({ detail: "denied" }, 403)) : defaults(name));
  const { unmount, runtime } = mount(locale);
  fireEvent.click(await screen.findByTestId("snapshot-create"));
  fireEvent.click(await screen.findByTestId("snapshot-manifest"));
  await waitFor(() => expect(screen.queryByTestId("snapshot-manifest")).toBeNull());
  expect(screen.getByTestId("snapshot-error").textContent).toContain(locale === "zh-CN" ? "权限已撤销" : "Access revoked");
  expect(downloadFile).not.toHaveBeenCalled();
  unmount(); runtime.destroy();
});
