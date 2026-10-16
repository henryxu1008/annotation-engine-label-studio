import { act, fireEvent, render, screen } from "@testing-library/react";
import { observable, runInAction } from "mobx";
import { createLocaleRuntime } from "@humansignal/i18n";
import { DraftSaveStatus } from "./DraftSaveStatus";

jest.mock("@humansignal/ui", () => ({
  Button: ({ children, size, ...props }) => <button {...props}>{children}</button>,
  Tooltip: ({ children, title }) => <div title={title}>{children}</div>,
}));

it.each(["en-US", "zh-CN"].flatMap(locale => ["changed", "missing"].map(reason => [locale, reason])))("preserves local results before %s %s recovery", async (locale, reason) => {
  const annotation = observable({
    id: 'local', store: { task: { id: 42 } }, draftId: 7, draftRevision: 1,
    hasUnsavedDraft: true, draftConflict: true, draftConflictReason: reason, note: 'my offline edit',
    serializeAnnotation() { return [{ value: this.note }]; },
    get draftBackup() { return JSON.stringify({ task_id: 42, draft_id: 7, draft_revision: 1, result: this.serializeAnnotation() }, null, 2); },
    reloadLatestDraft: jest.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true),
  });
  URL.createObjectURL = jest.fn().mockReturnValue('blob:local-copy');
  URL.revokeObjectURL = jest.fn();
  const click = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const view = render(<Provider><DraftSaveStatus annotation={annotation} /></Provider>);
  const zh = locale === 'zh-CN';
  expect(screen.getByTestId('draft-conflict')).toHaveTextContent(reason === 'missing'
    ? (zh ? '草稿已不存在' : 'Draft is no longer available')
    : (zh ? '草稿已在另一页面更新' : 'Draft changed in another page'));
  if (reason === 'missing') expect(screen.getByTestId('draft-reload-latest')).toHaveTextContent(zh ? '载入任务最新状态' : 'Load latest task');
  expect(screen.getByRole('textbox').value).toContain('my offline edit');
  const reload = screen.getByTestId('draft-reload-latest');
  expect(reload).toBeDisabled();
  fireEvent.click(screen.getByTestId('draft-backup'));
  expect(click).toHaveBeenCalledTimes(1);
  expect(reload).toBeEnabled();
  act(() => runInAction(() => { annotation.note = 'new local edit'; }));
  expect(reload).toBeDisabled();
  fireEvent.click(screen.getByTestId('draft-backup'));
  await act(async () => { fireEvent.click(reload); });
  expect(screen.getAllByRole('alert').some(el => el.textContent.includes(locale === 'zh-CN' ? '本页修改仍然保留，请检查网络' : 'Your edits remain here; check'))).toBe(true);
  expect(screen.getByRole('textbox').value).toContain('new local edit');
  expect(reload).toBeEnabled();
  await act(async () => { fireEvent.click(reload); });
  expect(annotation.reloadLatestDraft).toHaveBeenCalledTimes(2);
  view.unmount();runtime.destroy();click.mockRestore();
});

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
