import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { PackageManagement, PackagePoolList } from "./TaskPackagePools";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});

const mount = (element, locale) => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const view = render(<Provider>{element}</Provider>);
  return { runtime, ...view };
};
beforeEach(() => mockCallApi.mockReset());

it.each(["en-US", "zh-CN"])("configures the pool using its current revision in %s", async (locale) => {
  mockCallApi.mockImplementation((name) =>
    Promise.resolve(
      name === "taskPackagePool"
        ? { revision: 3, enabled: true, candidate_ids: [8, 9], due_at: null, claimed: false, advanced_enabled: true }
        : { revision: 4, enabled: true, candidate_ids: [8, 9], due_at: null },
    ),
  );
  const { runtime, unmount } = mount(
    <PackageManagement projectId={4} packageId={7} canManagePool onChanged={jest.fn()} />,
    locale,
  );
  fireEvent.click(
    screen.getByRole("button", { name: locale === "zh-CN" ? "管理分配与领取" : "Manage assignment and claims" }),
  );
  expect(
    await screen.findByLabelText(
      locale === "zh-CN"
        ? "可领取的用户 ID，以逗号分隔（最多 100 人）"
        : "Eligible user IDs, separated by commas (up to 100)",
    ),
  ).not.toBeNull();
  fireEvent.click(screen.getByRole("button", { name: locale === "zh-CN" ? "保存领取设置" : "Save claim settings" }));
  await waitFor(() =>
    expect(mockCallApi).toHaveBeenCalledWith(
      "updateTaskPackagePool",
      expect.objectContaining({
        body: { expected_revision: 3, candidate_ids: [8, 9], enabled: true, due_at: null, priority: 0 },
      }),
    ),
  );
  unmount();
  runtime.destroy();
});

it.each(["en-US", "zh-CN"])("retries an ambiguous claim with the same key in %s", async (locale) => {
  mockCallApi.mockImplementation((name) => {
    if (name === "reconcilePackageOverdueNotices") return Promise.resolve({ results: [], next_after: null });
    if (name === "taskPackagePools")
      return Promise.resolve({
        results: [{ package_id: 7, name: "Package A", due_at: null, can_claim: true }],
        previous: null,
        next: null,
      });
    const claimCalls = mockCallApi.mock.calls.filter(([method]) => method === "claimTaskPackage");
    return Promise.resolve(claimCalls.length === 1 ? { error: true } : { assignment_count: 3 });
  });
  const onClaimed = jest.fn();
  const { runtime, unmount } = mount(<PackagePoolList projectId={4} onClaimed={onClaimed} />, locale);
  const button = await screen.findByRole("button", {
    name: locale === "zh-CN" ? "领取任务包／重试" : "Claim package / retry",
  });
  fireEvent.click(button);
  await screen.findByRole("alert");
  fireEvent.click(button);
  await waitFor(() => expect(onClaimed).toHaveBeenCalledWith(7));
  const calls = mockCallApi.mock.calls.filter(([method]) => method === "claimTaskPackage");
  expect(calls).toHaveLength(2);
  expect(calls[0][1].headers).toEqual(calls[1][1].headers);
  unmount();
  runtime.destroy();
});

it("retains the dispatch key on an ambiguous result and locks the target", async () => {
  let calls = 0;
  mockCallApi.mockImplementation((name) =>
    name === "projectMembers"
      ? Promise.resolve({ results: [], next: null, previous: null })
      : Promise.resolve(++calls === 1 ? { error: true } : { assignment_count: 3 }),
  );
  const onChanged = jest.fn();
  const { runtime, unmount } = mount(
    <PackageManagement projectId={4} packageId={7} canDispatch onChanged={onChanged} />,
    "en-US",
  );
  fireEvent.click(screen.getByRole("button", { name: "Manage assignment and claims" }));
  const input = screen.getByLabelText("Assignee user ID");
  fireEvent.change(input, { target: { value: "8" } });
  fireEvent.click(screen.getByRole("button", { name: "Assign package / retry" }));
  await screen.findByRole("alert");
  expect(input.disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Assign package / retry" }));
  await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  const dispatchCalls = mockCallApi.mock.calls.filter(([name]) => name === "taskPackageDispatch");
  expect(dispatchCalls[0][1].headers).toEqual(dispatchCalls[1][1].headers);
  unmount();
  runtime.destroy();
});

it("clears pool choices after a claim is denied", async () => {
  mockCallApi.mockImplementation((name) =>
    Promise.resolve(
      name === "reconcilePackageOverdueNotices"
        ? { results: [], next_after: null }
        : name === "taskPackagePools"
          ? { results: [{ package_id: 7, name: "Package A", can_claim: true }], previous: null, next: null }
          : { error: true, status: 403 },
    ),
  );
  const { runtime, unmount } = mount(<PackagePoolList projectId={4} onClaimed={jest.fn()} />, "en-US");
  fireEvent.click(await screen.findByRole("button", { name: "Claim package / retry" }));
  await screen.findByRole("alert");
  expect(screen.queryByText("Package A")).toBeNull();
  unmount();
  runtime.destroy();
});

it("preserves deadline precision when an unrelated setting is saved", async () => {
  const due = "2026-12-03T12:34:56.123Z";
  mockCallApi.mockImplementation((name) =>
    Promise.resolve(
      name === "taskPackagePool"
        ? { revision: 1, enabled: true, candidate_ids: [8], due_at: due, claimed: false }
        : { revision: 2, enabled: false, candidate_ids: [8], due_at: due },
    ),
  );
  const { runtime, unmount } = mount(
    <PackageManagement projectId={4} packageId={7} canManagePool onChanged={jest.fn()} />,
    "en-US",
  );
  fireEvent.click(screen.getByRole("button", { name: "Manage assignment and claims" }));
  fireEvent.click(await screen.findByLabelText("Open for claims"));
  fireEvent.click(screen.getByRole("button", { name: "Save claim settings" }));
  await waitFor(() =>
    expect(mockCallApi.mock.calls.find(([name]) => name === "updateTaskPackagePool")[1].body.due_at).toBe(due),
  );
  unmount();
  runtime.destroy();
});

it("selects project members and submits priority without exposing reviewers as candidates", async () => {
  mockCallApi.mockImplementation((name) => {
    if (name === "taskPackagePool")
      return Promise.resolve({ revision: 0, enabled: false, candidate_ids: [], due_at: null, advanced_enabled: true });
    if (name === "projectMembers")
      return Promise.resolve({
        results: [
          { user: { id: 8, email: "annotator@example.test" }, role: "annotator", enabled: true },
          { user: { id: 9, email: "reviewer@example.test" }, role: "reviewer", enabled: true },
        ],
        next: null,
        previous: null,
      });
    return Promise.resolve({ revision: 1, enabled: true, candidate_ids: [8], due_at: null, priority: 7 });
  });
  const { runtime, unmount } = mount(
    <PackageManagement projectId={4} packageId={7} canManagePool onChanged={jest.fn()} />,
    "en-US",
  );
  fireEvent.click(screen.getByRole("button", { name: "Manage assignment and claims" }));
  fireEvent.click(await screen.findByLabelText("annotator@example.test"));
  expect(screen.queryByLabelText("reviewer@example.test")).toBeNull();
  fireEvent.change(screen.getByLabelText("Priority (0–100)"), { target: { value: "7" } });
  fireEvent.click(screen.getByRole("button", { name: "Save claim settings" }));
  await waitFor(() =>
    expect(mockCallApi).toHaveBeenCalledWith(
      "updateTaskPackagePool",
      expect.objectContaining({
        body: expect.objectContaining({ candidate_ids: [8], priority: 7 }),
      }),
    ),
  );
  unmount();
  runtime.destroy();
});

it("retries reopening with the same revision, claim and key", async () => {
  let attempts = 0;
  mockCallApi.mockImplementation((name) => {
    if (name === "taskPackagePool")
      return Promise.resolve({
        revision: 3,
        claim_id: 22,
        claimed: true,
        candidate_ids: [8],
        due_at: null,
        advanced_enabled: true,
      });
    if (name === "projectMembers") return Promise.resolve({ results: [], next: null, previous: null });
    if (name === "reopenTaskPackage") return Promise.resolve(++attempts === 1 ? { error: true } : { replayed: true });
  });
  const changed = jest.fn();
  const { runtime, unmount } = mount(
    <PackageManagement projectId={4} packageId={7} canManagePool onChanged={changed} />,
    "en-US",
  );
  fireEvent.click(screen.getByRole("button", { name: "Manage assignment and claims" }));
  const button = await screen.findByRole("button", { name: "Reopen after cancellation / retry" });
  fireEvent.click(button);
  await screen.findByRole("alert");
  fireEvent.click(button);
  await waitFor(() => expect(changed).toHaveBeenCalled());
  const calls = mockCallApi.mock.calls.filter(([name]) => name === "reopenTaskPackage");
  expect(calls[0][1].body).toEqual({ expected_revision: 3, claim_id: 22 });
  expect(calls[0][1].headers).toEqual(calls[1][1].headers);
  unmount();
  runtime.destroy();
});

it("shows reconciled overdue notices and supports the next page", async () => {
  mockCallApi.mockImplementation((name, options) => {
    if (name === "taskPackagePools") return Promise.resolve({ results: [], previous: null, next: null });
    if (name === "reconcilePackageOverdueNotices")
      return Promise.resolve(
        options.params.after
          ? { results: [{ id: 2, name: "Second", overdue_count: 1 }], next_after: null }
          : { results: [{ id: 1, name: "First", overdue_count: 3 }], next_after: 4 },
      );
  });
  const { runtime, unmount } = mount(<PackagePoolList projectId={4} onClaimed={jest.fn()} />, "en-US");
  expect(await screen.findByText(/First: Overdue claimed tasks: 3/)).not.toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Load more overdue notices" }));
  expect(await screen.findByText(/Second: Overdue claimed tasks: 1/)).not.toBeNull();
  unmount();
  runtime.destroy();
});

it("saves an explicit background allocation choice", async () => {
  mockCallApi.mockImplementation((name) => {
    if (name === "taskPackagePool")
      return Promise.resolve({
        revision: 1,
        enabled: true,
        candidate_ids: [8],
        due_at: null,
        advanced_enabled: true,
        automation_available: true,
        automatic: false,
      });
    if (name === "projectMembers") return Promise.resolve({ results: [], next: null, previous: null });
    return Promise.resolve({
      revision: 2,
      enabled: true,
      candidate_ids: [8],
      due_at: null,
      priority: 0,
      automatic: true,
    });
  });
  const { runtime, unmount } = mount(
    <PackageManagement projectId={4} packageId={7} canManagePool onChanged={jest.fn()} />,
    "en-US",
  );
  fireEvent.click(screen.getByRole("button", { name: "Manage assignment and claims" }));
  fireEvent.click(await screen.findByLabelText("Automatically assign this package"));
  fireEvent.click(screen.getByRole("button", { name: "Save claim settings" }));
  await waitFor(() =>
    expect(mockCallApi).toHaveBeenCalledWith(
      "updateTaskPackagePool",
      expect.objectContaining({
        body: expect.objectContaining({ automatic: true, expected_revision: 1 }),
      }),
    ),
  );
  unmount();
  runtime.destroy();
});
