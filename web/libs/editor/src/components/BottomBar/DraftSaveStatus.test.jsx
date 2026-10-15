import { act, fireEvent, render, screen } from "@testing-library/react";
import { observable, runInAction } from "mobx";
import { createLocaleRuntime } from "@humansignal/i18n";
import { DraftSaveStatus } from "./DraftSaveStatus";

jest.mock("@humansignal/ui", () => ({
  Button: ({ children, size, ...props }) => <button {...props}>{children}</button>,
  Tooltip: ({ children, title }) => <div title={title}>{children}</div>,
}));

it.each(["en-US", "zh-CN"])("keeps %s draft failures actionable until a confirmed save", async (locale) => {
  const annotation = observable({
    hasUnsavedDraft: true,
    isDraftSaving: false,
    submissionStarted: 0,
    draftSaveError: false,
    saveDraftImmediatelyWithResults: jest.fn().mockRejectedValue(new Error("offline")),
  });
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const view = render(<Provider><DraftSaveStatus annotation={annotation} /></Provider>);
  const zh = locale === "zh-CN";
  expect(screen.getByRole("status")).toHaveTextContent(zh ? "草稿未保存" : "Unsaved draft");
  expect(screen.getByTitle(zh
    ? "刷新或关闭标签页前请先保存最新编辑。保存失败时，请留在此页重试。"
    : "Save your latest edits before refreshing or closing this tab. If saving fails, stay on this page and retry.")).toBeInTheDocument();
  act(() => runInAction(() => { annotation.isDraftSaving = true; }));
  expect(screen.getByRole("button")).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent(zh ? "正在保存草稿…" : "Saving draft…");
  act(() => runInAction(() => { annotation.isDraftSaving = false; annotation.draftSaveError = true; }));
  expect(screen.getByRole("status")).toHaveTextContent(zh ? "草稿保存失败" : "Draft not saved");
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: zh ? "重试保存" : "Retry save" })); });
  expect(annotation.saveDraftImmediatelyWithResults).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("status")).toBeInTheDocument();
  act(() => { runtime.updateLocale(zh ? "en-US" : "zh-CN"); });
  expect(screen.getByRole("button", { name: zh ? "Retry save" : "重试保存" })).toBeEnabled();
  act(() => runInAction(() => { annotation.hasUnsavedDraft = false; annotation.draftSaveError = false; }));
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  view.unmount();
  runtime.destroy();
});
