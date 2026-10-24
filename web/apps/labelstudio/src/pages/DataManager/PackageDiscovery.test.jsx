import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { MyWorkWorkspace } from "./MyWorkWorkspace";
import { TaskPackages } from "./TaskPackages";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});

const counts = {
  assigned: 1,
  in_progress: 0,
  rework: 1,
  submitted: 0,
  approved: 0,
  conflict: 0,
  assignments: 2,
  pending_review: 0,
};
const packages = {
  role: "annotator",
  can_create: false,
  as_of: "2026-10-24T10:00Z",
  previous: null,
  next: null,
  results: [
    { id: 7, name: "Package A", counts },
    { id: 8, name: "Package B", counts: { ...counts, assignments: 1 } },
  ],
};
const detail = {
  role: "annotator",
  package: { id: 7, name: "Package A", instructions: "规则 v0.1" },
  counts,
  count: 2,
  state: "",
  filter_states: ["assigned", "rework"],
  as_of: packages.as_of,
  continue_url: "/projects/4/data?labeling=1&task_package=7",
  previous: null,
  next: null,
  results: [{ task_id: 9, assignment_id: 3, state: "rework", editor_url: "/projects/4/data?task=9&task_package=7" }],
};
const work = {
  kind: "annotation",
  packages_enabled: true,
  task_package: null,
  counts: { assigned: 1, rework: 1 },
  previous: null,
  next: null,
  results: [
    {
      assignment_id: 3,
      task_id: 9,
      state: "rework",
      allowed_actions: ["open_editor"],
      editor_url: "/projects/4/data?task=9",
    },
  ],
};
const scoped = {
  ...work,
  task_package: { id: 7, name: "Package A" },
  continue_url: detail.continue_url,
  results: [{ ...work.results[0], editor_url: detail.results[0].editor_url }],
};
const mount = (Component, props = {}, locale = "en-US") => {
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const view = render(
    <Provider>
      <Component projectId={4} {...props} />
    </Provider>,
  );
  return { ...view, runtime, Provider };
};
beforeEach(() => mockCallApi.mockReset());

it.each(["en-US", "zh-CN"])(
  "shows independent summaries and reuses a read-only package picker in %s",
  async (locale) => {
    const select = jest.fn();
    mockCallApi.mockResolvedValue({ ...packages, can_create: true });
    const { runtime, unmount } = mount(TaskPackages, { onSelectPackage: select }, locale);
    const row = await screen.findByTestId("package-summary-7");
    expect(within(row).getByText(locale === "zh-CN" ? "待返工" : "Changes requested")).not.toBeNull();
    expect(screen.queryByRole("heading", { name: /Create a fixed package|创建固定任务包/ })).toBeNull();
    fireEvent.click(within(row).getByRole("button", { name: "Package A" }));
    expect(select).toHaveBeenCalledWith(packages.results[0]);
    expect(mockCallApi.mock.calls.map(([name]) => name)).toEqual(["taskPackages"]);
    unmount();
    runtime.destroy();
  },
);

it.each(["en-US", "zh-CN"])(
  "retains the detail state on pagination, code-only recovery and language changes in %s",
  async (locale) => {
    const filtered = { ...detail, state: "rework", count: 1, continue_url: null, next: "next" };
    mockCallApi
      .mockResolvedValueOnce(packages)
      .mockResolvedValueOnce(detail)
      .mockResolvedValueOnce(filtered)
      .mockResolvedValueOnce({ error: true, response: { display_context: { reason: "PACKAGE_PAGE_OUT_OF_RANGE" } } })
      .mockResolvedValue(filtered);
    const { runtime, unmount } = mount(TaskPackages, {}, locale);
    fireEvent.click(await screen.findByRole("button", { name: "Package A" }));
    fireEvent.click(await screen.findByRole("button", { name: locale === "zh-CN" ? "待返工" : "Changes requested" }));
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: /Continue annotation|在此包内连续标注/ })).toBeNull(),
    );
    fireEvent.click(await screen.findByRole("button", { name: locale === "zh-CN" ? "下一页" : "Next" }));
    await waitFor(() => expect(mockCallApi).toHaveBeenCalledTimes(5));
    expect(mockCallApi.mock.calls[3][1].params).toMatchObject({ package: 7, state: "rework", page: 2 });
    expect(mockCallApi.mock.calls[4][1].params).toMatchObject({ package: 7, state: "rework", page: 1 });
    await screen.findByRole("link", { name: locale === "zh-CN" ? "打开标注" : "Open annotation" });
    act(() => runtime.updateLocale(locale === "zh-CN" ? "en-US" : "zh-CN"));
    expect(mockCallApi).toHaveBeenCalledTimes(5);
    expect(
      screen
        .getByRole("button", { name: locale === "zh-CN" ? "Changes requested" : "待返工" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    unmount();
    runtime.destroy();
  },
);

it("returns from package detail to the previous list page", async () => {
  mockCallApi
    .mockResolvedValueOnce({ ...packages, next: "next" })
    .mockResolvedValueOnce(packages)
    .mockResolvedValueOnce(detail)
    .mockResolvedValue(packages);
  const { runtime, unmount } = mount(TaskPackages);
  fireEvent.click(await screen.findByRole("button", { name: "Next" }));
  await waitFor(() => expect(mockCallApi).toHaveBeenCalledTimes(2));
  fireEvent.click(await screen.findByRole("button", { name: "Package A" }));
  fireEvent.click(await screen.findByRole("button", { name: "Back to packages" }));
  await waitFor(() => expect(mockCallApi).toHaveBeenCalledTimes(4));
  expect(mockCallApi.mock.calls[3][1].params).toEqual({ pk: 4, page: 2, page_size: 50 });
  unmount();
  runtime.destroy();
});

it.each(["en-US", "zh-CN"])(
  "enters the explicitly selected package from My Work and keeps read-only retry scoped in %s",
  async (locale) => {
    let denied = false;
    mockCallApi.mockImplementation((name, options) => {
      if (name === "taskPackages") return Promise.resolve(packages);
      if (options.params.task_package === 7) return Promise.resolve(denied ? { error: true, status: 403 } : scoped);
      return Promise.resolve(work);
    });
    const { runtime, unmount } = mount(MyWorkWorkspace, {}, locale);
    fireEvent.click(
      await screen.findByRole("button", { name: locale === "zh-CN" ? "选择任务包" : "Choose a task package" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Package A" }));
    const link = await screen.findByRole("link", { name: locale === "zh-CN" ? "打开标注" : "Open annotation" });
    expect(link.getAttribute("href")).toBe(detail.results[0].editor_url);
    expect(link.hasAttribute("data-external")).toBe(true);
    expect(
      screen
        .getByRole("link", { name: locale === "zh-CN" ? "在此包内连续标注" : "Continue annotation in this package" })
        .getAttribute("href"),
    ).toBe(detail.continue_url);
    denied = true;
    fireEvent.click(screen.getByRole("button", { name: locale === "zh-CN" ? "刷新列表" : "Refresh list" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("link")).toBeNull();
    expect(mockCallApi.mock.calls.at(-1)[1].params.task_package).toBe(7);
    denied = false;
    fireEvent.click(screen.getByRole("button", { name: locale === "zh-CN" ? "刷新列表" : "Refresh list" }));
    await screen.findByRole("link", { name: locale === "zh-CN" ? "打开标注" : "Open annotation" });
    expect(
      mockCallApi.mock.calls.filter(([name, options]) => name === "myWork" && !options.params.task_package),
    ).toHaveLength(1);
    unmount();
    runtime.destroy();
  },
);

it("uses the existing selected-package reviewer route rather than the ordinary queue callback", async () => {
  const open = jest.fn();
  const review = {
    kind: "review",
    packages_enabled: true,
    task_package: null,
    counts: { pending: 1 },
    results: [{ submission_id: 20, task_id: 9, state: "pending", allowed_actions: ["open_review_queue"] }],
  };
  mockCallApi.mockImplementation((name, options) =>
    Promise.resolve(
      name === "taskPackages"
        ? { ...packages, role: "reviewer" }
        : { ...review, task_package: options.params.task_package ? scoped.task_package : null },
    ),
  );
  const { runtime, unmount } = mount(MyWorkWorkspace, { onOpenReviewQueue: open });
  fireEvent.click(await screen.findByRole("button", { name: "Choose a task package" }));
  fireEvent.click(await screen.findByRole("button", { name: "Package A" }));
  expect((await screen.findByRole("link", { name: "Open review workspace" })).getAttribute("href")).toBe(
    "/projects/4/data?task_package=7&package_review=1",
  );
  expect(open).not.toHaveBeenCalled();
  expect(screen.queryByRole("link", { name: "Open annotation" })).toBeNull();
  unmount();
  runtime.destroy();
});

it("discards a late package result after switching project", async () => {
  let complete;
  mockCallApi.mockImplementation((name, options) => {
    if (name === "taskPackages") return Promise.resolve(packages);
    if (options.params.task_package)
      return new Promise((resolve) => {
        complete = resolve;
      });
    return Promise.resolve({ ...work, packages_enabled: options.params.pk === 4 });
  });
  const { runtime, unmount, rerender, Provider } = mount(MyWorkWorkspace);
  fireEvent.click(await screen.findByRole("button", { name: "Choose a task package" }));
  fireEvent.click(await screen.findByRole("button", { name: "Package A" }));
  rerender(
    <Provider>
      <MyWorkWorkspace projectId={5} />
    </Provider>,
  );
  await screen.findByRole("link", { name: "Open annotation" });
  await act(async () => complete(scoped));
  expect(screen.getByRole("link", { name: "Open annotation" }).getAttribute("href")).toBe(work.results[0].editor_url);
  expect(screen.queryByText("My Work in Package A")).toBeNull();
  unmount();
  runtime.destroy();
});
