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

it.each(["network", "server", "body"])("continues polling after a transient %s failure", async (failure) => {
  let details = 0;
  mockCallApi.mockImplementation((name) => {
    if (name === "releaseSnapshotCreateRaw") return Promise.resolve(raw({ ...snapshot, state: "queued" }));
    if (name === "releaseSnapshotDetailRaw" && ++details === 1) {
      if (failure === "network") return Promise.reject(new Error("temporary connection loss"));
      if (failure === "body") return Promise.resolve({ ok: true, json: async () => { throw new Error("truncated body"); } });
      return Promise.resolve(raw({}, 503));
    }
    return defaults(name);
  });
  const { unmount, runtime } = mount();
  try {
    fireEvent.click(await screen.findByTestId("snapshot-create"));
    await screen.findByTestId("snapshot-manifest", {}, { timeout: 6000 });
    expect(mockCallApi.mock.calls.filter(([name]) => name === "releaseSnapshotDetailRaw")).toHaveLength(2);
  } finally { unmount(); runtime.destroy(); }
}, 10000);

it.each(["en-US", "zh-CN"])("stops polling after authority denial in %s", async (locale) => {
  mockCallApi.mockImplementation((name) => {
    if (name === "releaseSnapshotCreateRaw") return Promise.resolve(raw({ ...snapshot, state: "running" }));
    if (name === "releaseSnapshotDetailRaw") return Promise.resolve(raw({}, 403));
    return defaults(name);
  });
  const { unmount, runtime } = mount(locale);
  try {
    fireEvent.click(await screen.findByTestId("snapshot-create"));
    await screen.findByTestId("snapshot-error", {}, { timeout: 4000 });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1700)); });
    expect(mockCallApi.mock.calls.filter(([name]) => name === "releaseSnapshotDetailRaw")).toHaveLength(1);
    expect(screen.queryByTestId("snapshot-status")).toBeNull();
    expect(screen.queryByTestId("snapshot-manifest")).toBeNull();
  } finally { unmount(); runtime.destroy(); }
}, 10000);
