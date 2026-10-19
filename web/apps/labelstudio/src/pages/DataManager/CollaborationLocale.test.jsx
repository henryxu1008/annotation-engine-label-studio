import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { AssignmentManager } from "./AssignmentManager";
import { ReviewerWorkspace } from "./ReviewerWorkspace";
import { SubmissionReleaseWorkspace } from "./SubmissionReleaseWorkspace";
import { MembersSettings } from "../Settings/MembersSettings";
import { downloadFile } from "../../utils/download";

const mockCallApi = jest.fn();
const mockReplace = jest.fn();
const mockHistory = { replace: mockReplace };
jest.mock("../../utils/download", () => ({ downloadFile: jest.fn() }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return {
    Button: ({ children, disabled, onClick, type, ...props }) => React.createElement("button", {
      disabled, onClick, type, "data-testid": props["data-testid"],
    }, children),
    Typography: ({ children }) => React.createElement("div", null, children),
  };
});
jest.mock("../../components/Spinner/Spinner", () => ({ Spinner: () => null }));
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("../../providers/ProjectProvider", () => ({
  useProject: () => ({ project: { id: 4, created_by: { id: 1, email: "owner@example.test" } } }),
}));
jest.mock("react-router", () => ({
  ...jest.requireActual("react-router"),
  useHistory: () => mockHistory,
}));

const submission = {
  id: 21,
  revision: 3,
  status: "pending",
  submitted_by: { email: "author@example.test" },
  submitted_at: "2026-09-30T10:00:00Z",
  result_hash: "immutable-hash-21",
  result_snapshot: { task: { id: 9 }, result: [{ value: "original" }] },
};

function mount(node) {
  const runtime = createLocaleRuntime("en-US");
  const Provider = runtime.provider;
  const view = render(<Provider>{node}</Provider>);
  return { runtime, ...view };
}

describe("collaboration locale projections", () => {
  beforeEach(() => {
    mockCallApi.mockReset();
    mockReplace.mockReset();
    downloadFile.mockReset();
  });

  it("changes member labels while preserving role code and member identity", async () => {
    mockCallApi.mockImplementation((method) => {
      if (method === "projectMembers") return Promise.resolve({ results: [{ id: 7, user: { id: 8, email: "member@example.test" }, role: "annotator", enabled: true }] });
      if (method === "projectMemberCandidates") return Promise.resolve({ results: [] });
      throw new Error(`unexpected ${method}`);
    });
    const { runtime, unmount } = mount(<MembersSettings />);
    const role = await screen.findByTestId("member-role-8");
    expect(role.value).toBe("annotator");
    await waitFor(() => expect(screen.getAllByText("Enabled").length).toBeGreaterThan(0));
    const count = mockCallApi.mock.calls.length;
    act(() => runtime.updateLocale("zh-CN"));
    expect(role.value).toBe("annotator");
    expect(screen.getAllByText("已启用").length).toBeGreaterThan(0);
    expect(screen.getByText("member@example.test")).not.toBeNull();
    expect(mockCallApi).toHaveBeenCalledTimes(count);
    unmount();
    runtime.destroy();
  });

  it("changes assignment status without changing selected assignee or writing", async () => {
    mockCallApi.mockImplementation((method) => {
      if (method === "taskAssignments") return Promise.resolve({ results: [{ id: 12, assignee: 8, assignee_identity: { email: "member@example.test" }, status: "in_progress", version: 4 }] });
      if (method === "eligibleTaskAssignees") return Promise.resolve({ results: [{ id: 9, email: "other@example.test" }] });
      throw new Error(`unexpected ${method}`);
    });
    const { runtime, unmount } = mount(<AssignmentManager projectId={4} taskId={9} />);
    const select = await screen.findByTestId("assignment-assignee-select");
    fireEvent.change(select, { target: { value: "9" } });
    expect(screen.getByText("In progress")).not.toBeNull();
    const count = mockCallApi.mock.calls.length;
    act(() => runtime.updateLocale("zh-CN"));
    expect(select.value).toBe("9");
    expect(screen.getByText("进行中")).not.toBeNull();
    expect(screen.getByText("v4")).not.toBeNull();
    expect(mockCallApi).toHaveBeenCalledTimes(count);
    unmount();
    runtime.destroy();
  });

  it("localizes user ID fallbacks when assignment identities lack names and email", async () => {
    mockCallApi.mockImplementation((method) => {
      if (method === "taskAssignments") return Promise.resolve({ results: [{ id: 12, assignee: 8, status: "in_progress", version: 4 }] });
      if (method === "eligibleTaskAssignees") return Promise.resolve({ results: [{ id: 9, email: "", first_name: "", last_name: "" }] });
      throw new Error(`unexpected ${method}`);
    });
    const { runtime, unmount } = mount(<AssignmentManager projectId={4} taskId={9} />);
    await screen.findByRole("option", { name: "User 9" });
    expect(screen.getByTestId("assignment-row-12").textContent).toContain("User 8");
    const count = mockCallApi.mock.calls.length;

    act(() => runtime.updateLocale("zh-CN"));
    expect(screen.getByRole("option", { name: "用户 9" })).not.toBeNull();
    expect(screen.getByTestId("assignment-row-12").textContent).toContain("用户 8");
    expect(mockCallApi).toHaveBeenCalledTimes(count);
    unmount();
    runtime.destroy();
  });

  it("preserves typed rejection reason and immutable revision/hash on switch", async () => {
    mockCallApi.mockImplementation((method) => {
      if (method === "reviewableSubmissions") return Promise.resolve({ results: [submission] });
      if (method === "projectSubmissions") return Promise.resolve({ results: [] });
      throw new Error(`unexpected ${method}`);
    });
    const { runtime, unmount } = mount(<ReviewerWorkspace projectId={4} />);
    const reason = await screen.findByTestId("review-reject-reason");
    fireEvent.change(reason, { target: { value: "Needs correction" } });
    const hash = screen.getByTestId("review-result-hash").textContent;
    const snapshot = screen.getByTestId("review-result-snapshot").textContent;
    const count = mockCallApi.mock.calls.length;
    act(() => runtime.updateLocale("zh-CN"));
    expect(screen.getByTestId("review-status").textContent).toBe("待复核");
    expect(reason.value).toBe("Needs correction");
    expect(screen.getByTestId("review-result-hash").textContent).toBe(hash);
    expect(screen.getByTestId("review-result-snapshot").textContent).toBe(snapshot);
    expect(mockCallApi).toHaveBeenCalledTimes(count);
    unmount();
    runtime.destroy();
  });

  it("keeps release authority and snapshot bound to approved revision", async () => {
    mockCallApi.mockImplementation((method) => {
      if (method === "projectSubmissions") return Promise.resolve({ results: [{ ...submission, status: "approved" }] });
      throw new Error(`unexpected ${method}`);
    });
    const { runtime, unmount } = mount(<SubmissionReleaseWorkspace projectId={4} />);
    await screen.findByTestId("release-approved-submission");
    const hash = screen.getByTestId("release-result-hash").textContent;
    const snapshot = screen.getByTestId("release-result-snapshot").textContent;
    const count = mockCallApi.mock.calls.length;
    act(() => runtime.updateLocale("zh-CN"));
    expect(screen.getByTestId("release-status").textContent).toBe("已通过");
    expect(screen.getByTestId("release-result-hash").textContent).toBe(hash);
    expect(screen.getByTestId("release-result-snapshot").textContent).toBe(snapshot);
    expect(screen.getByTestId("release-approved-submission").textContent).toBe("发布并下载已通过的修订版");
    expect(mockCallApi).toHaveBeenCalledTimes(count);
    unmount();
    runtime.destroy();
  });

  it("shows revoked review access in the current language without a success notice", async () => {
    mockCallApi.mockImplementation((method) => {
      if (method === "reviewableSubmissions") return Promise.resolve({ results: [submission] });
      if (method === "projectSubmissions") return Promise.resolve({ results: [] });
      if (method === "reviewSubmission") return Promise.resolve({ status: 403, $meta: { ok: false }, response: { detail: "Forbidden" } });
      throw new Error(`unexpected ${method}`);
    });
    const { runtime, unmount } = mount(<ReviewerWorkspace projectId={4} />);
    const reason = await screen.findByTestId("review-reject-reason");
    fireEvent.change(reason, { target: { value: "Keep this reason" } });
    fireEvent.click(screen.getByTestId("review-reject"));
    const alert = await screen.findByTestId("review-error");
    await waitFor(() => expect(alert.textContent).toContain("Access was denied"));
    expect(screen.queryByRole("status")).toBeNull();
    act(() => runtime.updateLocale("zh-CN"));
    expect(alert.textContent).toContain("访问被拒绝");
    expect(mockCallApi.mock.calls.filter(([method]) => method === "reviewSubmission")).toHaveLength(1);
    unmount();
    runtime.destroy();
  });

  it("does not present a failed release as a success in either language", async () => {
    mockCallApi.mockImplementation((method) => {
      if (method === "projectSubmissions") return Promise.resolve({ results: [{ ...submission, status: "approved" }] });
      if (method === "releaseSubmissionRaw") return Promise.resolve({ status: 403, ok: false });
      throw new Error(`unexpected ${method}`);
    });
    const { runtime, unmount } = mount(<SubmissionReleaseWorkspace projectId={4} />);
    fireEvent.click(await screen.findByTestId("release-approved-submission"));
    const alert = await screen.findByTestId("release-error");
    await waitFor(() => expect(alert.textContent).toContain("Access was denied"));
    expect(screen.queryByTestId("release-success")).toBeNull();
    act(() => runtime.updateLocale("zh-CN"));
    expect(alert.textContent).toContain("访问被拒绝");
    expect(screen.queryByTestId("release-success")).toBeNull();
    expect(mockCallApi.mock.calls.filter(([method]) => method === "releaseSubmissionRaw")).toHaveLength(1);
    expect(downloadFile).not.toHaveBeenCalled();
    unmount();
    runtime.destroy();
  });

  for (const locale of ["en-US", "zh-CN"]) {
    it(`downloads the verified server blob without rounding snapshot integers in ${locale}`, async () => {
      const body = '{"submission_id":21,"revision":3,"result_hash":"immutable-hash-21","result_snapshot":{"project":{"label_config_hash":3917739503358554699},"annotation":{"result":[{"value":{"rectanglelabels":["物体"]}}]}}}';
      const result = JSON.parse(body);
      const blob = new Blob([body], { type: "application/json" });
      mockCallApi.mockImplementation((method) => {
        if (method === "projectSubmissions") return Promise.resolve({ results: [{ ...submission, status: "approved", result_snapshot: result.result_snapshot }] });
        if (method === "releaseSubmissionRaw") return Promise.resolve({ ok: true, clone: () => ({ json: async () => result }), blob: async () => blob });
        throw new Error(`unexpected ${method}`);
      });
      const { runtime, unmount } = mount(<SubmissionReleaseWorkspace projectId={4} />);
      act(() => runtime.updateLocale(locale));
      fireEvent.click(await screen.findByTestId("release-approved-submission"));
      await screen.findByTestId("release-success");
      expect(downloadFile).toHaveBeenCalledTimes(1);
      expect(downloadFile).toHaveBeenCalledWith(blob, "submission-21-revision-3.json");
      expect(mockCallApi.mock.calls.find(([method]) => method === "releaseSubmissionRaw")[1].params).toEqual({ submissionPk: 21 });
      // Re-serializing the parsed response would change this exact integer.
      expect(JSON.stringify(result)).not.toBe(body);
      unmount();
      runtime.destroy();
    });
  }

  it.each([
    { submission_id: 22 },
    { revision: 4 },
    { result_hash: "different-hash" },
    { result_snapshot: { changed: true } },
  ])("does not download a mismatched immutable response %j", async (change) => {
    const result = { submission_id: 21, revision: 3, result_hash: submission.result_hash, result_snapshot: submission.result_snapshot, ...change };
    const blob = jest.fn();
    mockCallApi.mockImplementation((method) => {
      if (method === "projectSubmissions") return Promise.resolve({ results: [{ ...submission, status: "approved" }] });
      if (method === "releaseSubmissionRaw") return Promise.resolve({ ok: true, clone: () => ({ json: async () => result }), blob });
      throw new Error(`unexpected ${method}`);
    });
    const { runtime, unmount } = mount(<SubmissionReleaseWorkspace projectId={4} />);
    fireEvent.click(await screen.findByTestId("release-approved-submission"));
    await waitFor(() => expect(screen.getByTestId("release-error").textContent).toContain("did not match"));
    expect(downloadFile).not.toHaveBeenCalled();
    expect(blob).not.toHaveBeenCalled();
    expect(screen.queryByTestId("release-success")).toBeNull();
    unmount();
    runtime.destroy();
  });

  it.each(["network", "json", "blob", "download"])("keeps %s failures retryable without a success notice", async (failure) => {
    const result = { submission_id: 21, revision: 3, result_hash: submission.result_hash, result_snapshot: submission.result_snapshot };
    const blob = new Blob(["server bytes"], { type: "application/json" });
    let attempts = 0;
    mockCallApi.mockImplementation((method) => {
      if (method === "projectSubmissions") return Promise.resolve({ results: [{ ...submission, status: "approved" }] });
      if (method === "releaseSubmissionRaw") {
        const fail = attempts++ === 0;
        if (fail && failure === "network") return Promise.reject(new Error("offline"));
        return Promise.resolve({ ok: true,
          clone: () => ({ json: async () => { if (fail && failure === "json") throw new Error("invalid JSON"); return result; } }),
          blob: async () => { if (fail && failure === "blob") throw new Error("interrupted body"); return blob; },
        });
      }
      throw new Error(`unexpected ${method}`);
    });
    if (failure === "download") downloadFile.mockImplementationOnce(() => { throw new Error("download failed"); });
    const { runtime, unmount } = mount(<SubmissionReleaseWorkspace projectId={4} />);
    fireEvent.click(await screen.findByTestId("release-approved-submission"));
    await waitFor(() => expect(screen.getByTestId("release-error").textContent).toContain("could not be released"));
    expect(screen.queryByTestId("release-success")).toBeNull();
    act(() => runtime.updateLocale("zh-CN"));
    expect(screen.getByTestId("release-error").textContent).toContain("无法发布");
    fireEvent.click(screen.getByTestId("release-approved-submission"));
    await screen.findByTestId("release-success");
    expect(downloadFile).toHaveBeenLastCalledWith(blob, "submission-21-revision-3.json");
    expect(attempts).toBe(2);
    unmount();
    runtime.destroy();
  });

  it.each(["selection", "unmount"])("does not download a stale body after %s", async (change) => {
    const result = { submission_id: 21, revision: 3, result_hash: submission.result_hash, result_snapshot: submission.result_snapshot };
    let finishBody;
    const body = new Promise((resolve) => { finishBody = resolve; });
    const blob = jest.fn(() => body);
    mockCallApi.mockImplementation((method) => {
      if (method === "projectSubmissions") return Promise.resolve({ results: [{ ...submission, status: "approved" }, { ...submission, id: 22, status: "approved" }] });
      if (method === "releaseSubmissionRaw") return Promise.resolve({ ok: true, clone: () => ({ json: async () => result }), blob });
      throw new Error(`unexpected ${method}`);
    });
    const { runtime, unmount } = mount(<SubmissionReleaseWorkspace projectId={4} />);
    fireEvent.click(await screen.findByTestId("release-approved-submission"));
    await waitFor(() => expect(blob).toHaveBeenCalledTimes(1));
    if (change === "selection") fireEvent.click(screen.getByTestId("release-submission-22"));
    else unmount();
    await act(async () => { finishBody(new Blob(["late response"])); await body; });
    expect(downloadFile).not.toHaveBeenCalled();
    expect(screen.queryByTestId("release-success")).toBeNull();
    if (change === "selection") unmount();
    runtime.destroy();
  });
});
