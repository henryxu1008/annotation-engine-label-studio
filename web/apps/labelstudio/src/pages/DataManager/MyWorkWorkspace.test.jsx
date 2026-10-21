import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { MyWorkWorkspace } from "./MyWorkWorkspace";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});

const row = {
  assignment_id: 7,
  task_id: 9,
  state: "rework",
  has_draft: true,
  latest_status: "rejected",
  latest_revision: 2,
  rejection_reason: "Keep the outer punctuation.",
  source: "direct_assignment",
  allowed_actions: ["open_editor"],
  editor_url: "/projects/4/data?task=9",
};
const data = {
  kind: "annotation",
  available_kinds: ["annotation"],
  counts: { assigned: 0, rework: 1 },
  results: [row],
  next: null,
  previous: null,
};

const mount = (props = {}) => {
  const runtime = createLocaleRuntime("en-US");
  const Provider = runtime.provider;
  const view = render(
    <Provider>
      <MyWorkWorkspace projectId={4} {...props} />
    </Provider>,
  );
  return { runtime, ...view };
};

describe("personal work projection", () => {
  beforeEach(() => mockCallApi.mockReset());

  it("switches display language without changing work, filters or making writes", async () => {
    mockCallApi.mockResolvedValue(data);
    const { runtime, unmount } = mount();
    const link = await screen.findByRole("link", { name: "Open annotation" });
    expect(link.getAttribute("href")).toBe("/projects/4/data?task=9");
    expect(screen.getByText("Keep the outer punctuation.")).not.toBeNull();
    act(() => runtime.updateLocale("zh-CN"));
    expect(screen.getByRole("link", { name: "打开标注" }).getAttribute("href")).toBe(link.getAttribute("href"));
    expect(screen.getByText("已有保存的草稿")).not.toBeNull();
    expect(screen.getByText("最新提交：已驳回")).not.toBeNull();
    expect(mockCallApi).toHaveBeenCalledTimes(1);
    expect(mockCallApi.mock.calls[0][0]).toBe("myWork");
    unmount();
    runtime.destroy();
  });

  it("removes stale links and rows when refresh discovers revocation", async () => {
    mockCallApi.mockResolvedValueOnce(data).mockResolvedValueOnce({ error: true, status: 403 });
    const { runtime, unmount } = mount();
    await screen.findByRole("link", { name: "Open annotation" });
    fireEvent.click(screen.getByRole("button", { name: "Refresh list" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.queryByText("Keep the outer punctuation.")).toBeNull();
    act(() => runtime.updateLocale("zh-CN"));
    expect(screen.getByRole("alert").textContent).toBeTruthy();
    unmount();
    runtime.destroy();
  });

  it("filters on the server and retains the filter during pagination", async () => {
    mockCallApi
      .mockResolvedValueOnce(data)
      .mockResolvedValueOnce({ ...data, next: "next-page" })
      .mockResolvedValue(data);
    const { runtime, unmount } = mount();
    fireEvent.click(await screen.findByRole("button", { name: "Changes requested (1)" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Next" }).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(mockCallApi).toHaveBeenCalledTimes(3));
    expect(mockCallApi.mock.calls[2][1].params).toEqual({ pk: 4, page: 2, page_size: 50, state: "rework" });
    unmount();
    runtime.destroy();
  });

  it("keeps blocked work visible without editor links", async () => {
    mockCallApi.mockResolvedValue({
      ...data,
      results: [{ ...row, allowed_actions: [], editor_url: null, blocked_reason: "WORK_SOURCE_CONFLICT" }],
    });
    const { runtime, unmount } = mount();
    await screen.findByText(/work records are inconsistent/);
    expect(screen.queryByRole("link")).toBeNull();
    unmount();
    runtime.destroy();
  });

  it("returns to page one when work disappeared from the requested page", async () => {
    mockCallApi
      .mockResolvedValueOnce({ ...data, next: "page-two" })
      .mockResolvedValueOnce({ error: true, status: 404, response: { detail: "Invalid page." } })
      .mockResolvedValueOnce(data);
    const { runtime, unmount } = mount();
    await screen.findByRole("link");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(mockCallApi).toHaveBeenCalledTimes(3));
    await screen.findByRole("link");
    expect(mockCallApi.mock.calls[2][1].params.page).toBe(1);
    expect(screen.queryByRole("alert")).toBeNull();
    unmount();
    runtime.destroy();
  });

  it("reviewers enter the existing review queue without raw task links", async () => {
    const onOpenReviewQueue = jest.fn();
    mockCallApi.mockResolvedValue({
      kind: "review",
      counts: { pending: 1 },
      results: [
        {
          submission_id: 21,
          task_id: 9,
          revision: 2,
          state: "pending",
          source_cancelled: true,
          allowed_actions: ["open_review_queue"],
        },
      ],
    });
    const { runtime, unmount } = mount({ onOpenReviewQueue });
    fireEvent.click(await screen.findByRole("button", { name: "Open review workspace" }));
    expect(onOpenReviewQueue).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/Source assignment cancelled/)).not.toBeNull();
    unmount();
    runtime.destroy();
  });

  it("ignores a response from an unmounted workspace", async () => {
    let complete;
    mockCallApi.mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    const { runtime, unmount } = mount();
    unmount();
    await act(async () => complete(data));
    expect(screen.queryByRole("link")).toBeNull();
    runtime.destroy();
  });
});
