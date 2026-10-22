import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { TaskPackageEntry, TaskPackages } from "./TaskPackages";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
const list = {
  results: [{ id: 7, name: "Trial 试用" }],
  role: "manager",
  can_create: true,
  previous: null,
  next: null,
};
const detail = {
  package: { id: 7, name: "Trial 试用", instructions: "规则 v0.1", source_note: "synthetic" },
  role: "manager",
  counts: { fixed_tasks: 2, unavailable_tasks: 1, assignments: 1, rework: 1 },
  as_of: "2026-10-22T00:00Z",
  results: [
    { task_id: 9, available: true, states: { rework: 1 } },
    { task_id: 10, available: false, states: {} },
  ],
  next: null,
  previous: null,
};
const own = {
  ...detail,
  role: "annotator",
  counts: { assignments: 1, rework: 1 },
  results: [{ assignment_id: 3, task_id: 9, state: "rework", editor_url: "/projects/4/data?task=9" }],
};

const mount = (locale = "en-US", props = {}) => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const view = render(
    <Provider>
      <TaskPackages projectId={4} {...props} />
    </Provider>,
  );
  return { runtime, ...view };
};

describe("fixed task packages", () => {
  beforeEach(() => {
    mockCallApi.mockReset();
    Object.defineProperty(global.crypto, "randomUUID", {
      configurable: true,
      value: () => "c7a8479c-a46e-4cfa-a80b-df323135117b",
    });
  });

  it.each(["en-US", "zh-CN"])(
    "creates an explicit package without dispatch in %s, retaining its key on retry",
    async (locale) => {
      mockCallApi
        .mockResolvedValueOnce({ ...list, results: [] })
        .mockResolvedValueOnce({ error: true })
        .mockResolvedValueOnce({ id: 7 })
        .mockResolvedValue(detail);
      const { runtime, unmount } = mount(locale, { initialTaskIds: [9, 10] });
      const zh = locale === "zh-CN";
      fireEvent.change(await screen.findByLabelText(zh ? "任务包名称" : "Package name"), {
        target: { value: "Trial 试用" },
      });
      fireEvent.click(screen.getByRole("button", { name: zh ? "预览固定清单" : "Preview fixed selection" }));
      expect(screen.getByText("9, 10", { selector: "p" })).not.toBeNull();
      const confirm = zh ? "确认创建／重试" : "Confirm creation / retry";
      fireEvent.click(screen.getByRole("button", { name: confirm }));
      await screen.findByRole("alert");
      fireEvent.click(screen.getByRole("button", { name: confirm }));
      await screen.findByRole("heading", { name: "Trial 试用" });
      const calls = mockCallApi.mock.calls.filter(([method]) => method === "createTaskPackage");
      expect(calls).toHaveLength(2);
      expect(calls[1][1].body).toEqual(calls[0][1].body);
      expect(calls[1][1].headers).toEqual(calls[0][1].headers);
      expect(calls[0][1].body.task_ids).toEqual([9, 10]);
      expect(calls[0][1].headers["Idempotency-Key"]).toBeTruthy();
      expect(mockCallApi.mock.calls.map(([method]) => method)).toEqual([
        "taskPackages",
        "createTaskPackage",
        "createTaskPackage",
        "taskPackageDetail",
      ]);
      expect(
        screen.getByText(zh ? "不可用（已删除或已移出项目）" : "Unavailable (deleted or outside this project)"),
      ).not.toBeNull();
      unmount();
      runtime.destroy();
    },
  );

  it("rejects duplicate IDs before sending a write, then preserves a preview during language change", async () => {
    mockCallApi.mockResolvedValue({ ...list, results: [] });
    const { runtime, unmount } = mount();
    fireEvent.change(await screen.findByLabelText("Package name"), { target: { value: "A" } });
    fireEvent.change(screen.getByLabelText(/Task IDs/), { target: { value: "9,9" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed selection" }));
    await screen.findByRole("alert");
    expect(mockCallApi).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText(/Task IDs/), { target: { value: "9，10" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed selection" }));
    act(() => runtime.updateLocale("zh-CN"));
    expect(screen.getByRole("button", { name: "确认创建／重试" })).not.toBeNull();
    expect(screen.getByText("9, 10", { selector: "p" })).not.toBeNull();
    expect(mockCallApi).toHaveBeenCalledTimes(1);
    unmount();
    runtime.destroy();
  });

  it.each(["en-US", "zh-CN"])("clears stale personal rows and links after revocation in %s", async (locale) => {
    const remaining = { assignment_id: 4, task_id: 10, state: "assigned", editor_url: "/projects/4/data?task=10" };
    mockCallApi
      .mockResolvedValueOnce({ ...list, role: "annotator", can_create: false })
      .mockResolvedValueOnce({ ...own, results: [...own.results, remaining] })
      .mockResolvedValueOnce({ error: true, status: 403, response: { display_context: { reason: "PACKAGE_ACCESS_CHANGED" } } })
      .mockResolvedValueOnce({ ...own, results: [remaining] });
    const { runtime, unmount } = mount(locale);
    fireEvent.click(await screen.findByRole("button", { name: "Trial 试用" }));
    expect(await screen.findAllByRole("link")).toHaveLength(2);
    expect(screen.queryByLabelText(locale === "zh-CN" ? "任务包名称" : "Package name")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: locale === "zh-CN" ? "刷新进度" : "Refresh progress" }));
    expect(screen.queryByRole("link")).toBeNull();
    expect((await screen.findByRole("alert")).textContent).toBe(locale === "zh-CN"
      ? "无法加载当前工作，权限可能已变化。旧记录已清除，请刷新重试。"
      : "Unable to load current work. Access may have changed. Refresh to retry; old rows have been cleared.");
    expect(screen.queryByRole("table")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: locale === "zh-CN" ? "刷新进度" : "Refresh progress" }));
    expect((await screen.findByRole("link")).getAttribute("href")).toBe(remaining.editor_url);
    expect(screen.getAllByRole("link")).toHaveLength(1);
    unmount();
    runtime.destroy();
  });

  it("uses the existing review queue, never opens an Editor for a reviewer", async () => {
    mockCallApi.mockResolvedValueOnce({ ...list, role: "reviewer", can_create: false }).mockResolvedValueOnce({
      ...detail,
      role: "reviewer",
      can_review: true,
      counts: { pending_review: 1 },
      results: [{ task_id: 9, submission_id: 22, revision: 2, state: "pending", source_cancelled: true }],
    });
    const onOpenReviewQueue = jest.fn();
    const { runtime, unmount } = mount("en-US", { onOpenReviewQueue });
    fireEvent.click(await screen.findByRole("button", { name: "Trial 试用" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open existing review queue" }));
    expect(onOpenReviewQueue).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("link")).toBeNull();
    unmount();
    runtime.destroy();
  });

  it.each(["Invalid page.", "无效页面。"])(
    "recovers a disappearing page by code, independent of %s",
    async (message) => {
      mockCallApi
        .mockResolvedValueOnce({ ...list, next: "next" })
        .mockResolvedValueOnce({
          error: true,
          response: { detail: message, display_context: { reason: "PACKAGE_PAGE_OUT_OF_RANGE" } },
        })
        .mockResolvedValueOnce(list);
      const { runtime, unmount } = mount();
      fireEvent.click(await screen.findByRole("button", { name: "Next" }));
      await waitFor(() => expect(mockCallApi).toHaveBeenCalledTimes(3));
      expect(mockCallApi.mock.calls[2][1].params.page).toBe(1);
      await screen.findByRole("button", { name: "Trial 试用" });
      unmount();
      runtime.destroy();
    },
  );

  it("does not treat a denied page as an empty pagination result", async () => {
    mockCallApi.mockResolvedValueOnce({ ...list, next: "next" }).mockResolvedValueOnce({ error: true, status: 403 });
    const { runtime, unmount } = mount();
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    await screen.findByRole("alert");
    expect(mockCallApi).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Trial 试用" })).toBeNull();
    unmount();
    runtime.destroy();
  });

  it("ignores a response after the workspace closes", async () => {
    let resolve;
    mockCallApi.mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const { runtime, unmount } = mount();
    unmount();
    await act(async () => resolve(list));
    expect(screen.queryByText("Trial 试用")).toBeNull();
    runtime.destroy();
  });

  it("hides its entry when the feature is disabled", async () => {
    mockCallApi.mockResolvedValue({ enabled: false });
    const runtime = createLocaleRuntime("en-US");
    const Provider = runtime.provider;
    const { unmount } = render(
      <Provider>
        <TaskPackageEntry projectId={4} onOpen={jest.fn()} />
      </Provider>,
    );
    await act(async () => {});
    expect(screen.queryByRole("button")).toBeNull();
    expect(mockCallApi).toHaveBeenCalledTimes(1);
    unmount();
    runtime.destroy();
  });

  it.each(["en-US", "zh-CN"])("previews and retries on HTTP without native randomUUID in %s", async (locale) => {
    Object.defineProperty(global.crypto, "randomUUID", { configurable: true, value: undefined });
    mockCallApi
      .mockResolvedValueOnce({ ...list, results: [] })
      .mockResolvedValueOnce({ error: true })
      .mockResolvedValueOnce({ id: 7 })
      .mockResolvedValue(detail);
    const { runtime, unmount } = mount(locale, { initialTaskIds: [9] });
    const zh = locale === "zh-CN";
    fireEvent.change(await screen.findByLabelText(zh ? "任务包名称" : "Package name"), { target: { value: "HTTP" } });
    fireEvent.click(screen.getByRole("button", { name: zh ? "预览固定清单" : "Preview fixed selection" }));
    const confirm = zh ? "确认创建／重试" : "Confirm creation / retry";
    fireEvent.click(screen.getByRole("button", { name: confirm }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: confirm }));
    await screen.findByRole("heading", { name: "Trial 试用" });
    const calls = mockCallApi.mock.calls.filter(([method]) => method === "createTaskPackage");
    expect(calls[0][1].headers["Idempotency-Key"]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(calls[1][1].headers).toEqual(calls[0][1].headers);
    unmount();
    runtime.destroy();
  });

  it.each(["en-US", "zh-CN"])(
    "shows the real creation failure reason and clears revoked forms in %s",
    async (locale) => {
      const zh = locale === "zh-CN";
      const invalid = {
        error: true,
        status: 400,
        response: { display_context: { reason: "PACKAGE_TASKS_UNAVAILABLE" } },
      };
      const conflict = { error: true, status: 409, response: { display_context: { reason: "PACKAGE_KEY_CONFLICT" } } };
      mockCallApi
        .mockResolvedValueOnce({ ...list, results: [] })
        .mockResolvedValueOnce(invalid)
        .mockResolvedValueOnce(conflict)
        .mockResolvedValueOnce({ error: true, status: 403 });
      const { runtime, unmount } = mount(locale, { initialTaskIds: [9] });
      fireEvent.change(await screen.findByLabelText(zh ? "任务包名称" : "Package name"), {
        target: { value: "Errors" },
      });
      fireEvent.click(screen.getByRole("button", { name: zh ? "预览固定清单" : "Preview fixed selection" }));
      const confirm = zh ? "确认创建／重试" : "Confirm creation / retry";
      fireEvent.click(screen.getByRole("button", { name: confirm }));
      await waitFor(() =>
        expect(screen.getByRole("alert").textContent).toContain(
          zh ? "部分任务已不在当前项目中" : "Some tasks are no longer available",
        ),
      );
      fireEvent.click(screen.getByRole("button", { name: confirm }));
      await waitFor(() =>
        expect(screen.getByRole("alert").textContent).toContain(
          zh ? "该请求编号已对应其他内容" : "This request key identifies different content",
        ),
      );
      fireEvent.click(screen.getByRole("button", { name: confirm }));
      await waitFor(() => expect(screen.queryByLabelText(zh ? "任务包名称" : "Package name")).toBeNull());
      expect(screen.getByRole("alert").textContent).toContain(zh ? "访问权限已变化" : "Access has changed");
      unmount();
      runtime.destroy();
    },
  );
});
