import { act, render } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { DraftGuard, draftGuardCallback, DRAFT_GUARD_KEY } from "./DraftGuard";
import { ToastContext } from "@humansignal/ui";
import { destroy, types } from "mobx-state-tree";

const mockBlock = jest.fn();
const mockHistory = { block: mockBlock };
jest.mock("react-router-dom", () => ({ useHistory: () => mockHistory }));
jest.mock("@humansignal/ui", () => ({ ToastContext: require("react").createContext(null) }));

afterEach(() => { window.Htx = undefined; draftGuardCallback.current = null; mockBlock.mockReset(); });

it.each(["en-US", "zh-CN"])("protects browser refresh/close only while the current %s draft is unconfirmed", (locale) => {
  const selected = { hasUnsavedDraft: true };
  window.Htx = { annotationStore: { selected } };
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  mockBlock.mockReturnValue(jest.fn());
  const view = render(<Provider><ToastContext.Provider value={{ show: jest.fn() }}><DraftGuard /></ToastContext.Provider></Provider>);
  act(() => runtime.updateLocale(locale === "en-US" ? "zh-CN" : "en-US"));
  const pending = new Event("beforeunload", { cancelable: true });
  expect(window.dispatchEvent(pending)).toBe(false);
  expect(pending.defaultPrevented).toBe(true);
  selected.hasUnsavedDraft = false;
  expect(window.dispatchEvent(new Event("beforeunload", { cancelable: true }))).toBe(true);
  selected.hasUnsavedDraft = true;
  view.unmount();
  expect(window.dispatchEvent(new Event("beforeunload", { cancelable: true }))).toBe(true);
  runtime.destroy();
});

it.each([200, 400])("keeps navigation blocked across a locale change until draft response %i", async (status) => {
  let complete;
  const save = jest.fn(() => new Promise((resolve) => { complete = resolve; }));
  window.Htx = { annotationStore: { selected: { hasUnsavedDraft: true, history: { undoIdx: 1, hasChanges: true }, saveDraftImmediatelyWithResults: save } } };
  const runtime = createLocaleRuntime("en-US");
  const Provider = runtime.provider;
  const toast = { show: jest.fn() };
  const unsubscribe = jest.fn();
  mockBlock.mockReturnValue(unsubscribe);
  const view = render(<Provider><ToastContext.Provider value={toast}><DraftGuard /></ToastContext.Provider></Provider>);
  expect(mockBlock.mock.calls[0][0]()).toBe(DRAFT_GUARD_KEY);
  const confirmNavigation = jest.fn();
  draftGuardCallback.current = confirmNavigation;
  act(() => { runtime.updateLocale("zh-CN"); });
  // The real ToastProvider replaces its context value when a toast is shown.
  // Its show callback remains stable and must not release pending navigation.
  view.rerender(<Provider><ToastContext.Provider value={{ ...toast }}><DraftGuard /></ToastContext.Provider></Provider>);
  expect(confirmNavigation).not.toHaveBeenCalled();
  expect(unsubscribe).not.toHaveBeenCalled();
  await act(async () => { complete({ $meta: { status } }); });
  view.rerender(<Provider><ToastContext.Provider value={{ ...toast }}><DraftGuard /></ToastContext.Provider></Provider>);
  expect(toast.show).toHaveBeenCalledWith({
    message: status === 200 ? "草稿已保存" : "草稿未能保存，请重试后再离开此页。",
    type: status === 200 ? "info" : "error",
  });
  expect(confirmNavigation).toHaveBeenCalledTimes(status === 200 ? 1 : 0);
  expect(mockBlock).toHaveBeenCalledTimes(1);
  view.unmount();
  runtime.destroy();
});

it("does not read a destroyed editor while navigating outside labeling", () => {
  const runtime = createLocaleRuntime("en-US");
  const Provider = runtime.provider;
  const editor = types.model({ annotationStore: types.frozen() }).create({ annotationStore: { selected: {} } });
  destroy(editor);
  window.Htx = editor;
  mockBlock.mockReturnValue(jest.fn());
  const view = render(<Provider><ToastContext.Provider value={{ show: jest.fn() }}><DraftGuard /></ToastContext.Provider></Provider>);
  expect(mockBlock.mock.calls[0][0]()).toBeUndefined();
  view.unmount();
  runtime.destroy();
});

it.each(["rejected", "unconfirmed"])("keeps edits on %s save and allows an explicit successful retry", async (failure) => {
  const save = jest.fn();
  if (failure === "rejected") save.mockRejectedValueOnce(new Error("offline"));
  else save.mockResolvedValueOnce({});
  save.mockResolvedValueOnce({ $meta: { status: 200 } });
  window.Htx = { annotationStore: { selected: { hasUnsavedDraft: true, history: { undoIdx: 1, hasChanges: true }, saveDraftImmediatelyWithResults: save } } };
  const runtime = createLocaleRuntime("zh-CN");
  const Provider = runtime.provider;
  const toast = { show: jest.fn() };
  mockBlock.mockReturnValue(jest.fn());
  const view = render(<Provider><ToastContext.Provider value={toast}><DraftGuard /></ToastContext.Provider></Provider>);
  const navigate = jest.fn();
  draftGuardCallback.current = navigate;
  await act(async () => { expect(mockBlock.mock.calls[0][0]()).toBe(DRAFT_GUARD_KEY); });
  expect(navigate).not.toHaveBeenCalled();
  expect(toast.show).toHaveBeenLastCalledWith({ message: "草稿未能保存，请重试后再离开此页。", type: "error" });
  await act(async () => { mockBlock.mock.calls[0][0](); });
  expect(navigate).toHaveBeenCalledTimes(1);
  view.unmount();
  runtime.destroy();
});

it.each(["en-US", "zh-CN"])("saves an undo back to index zero before leaving in %s", async (locale) => {
  let complete;
  const save = jest.fn(() => new Promise((resolve) => { complete = resolve; }));
  window.Htx = { annotationStore: { selected: { hasUnsavedDraft: true, history: { undoIdx: 0, hasChanges: true }, saveDraftImmediatelyWithResults: save } } };
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  const toast = { show: jest.fn() };
  mockBlock.mockReturnValue(jest.fn());
  const view = render(<Provider><ToastContext.Provider value={toast}><DraftGuard /></ToastContext.Provider></Provider>);
  const navigate = jest.fn();
  draftGuardCallback.current = navigate;
  expect(mockBlock.mock.calls[0][0]()).toBe(DRAFT_GUARD_KEY);
  expect(save).toHaveBeenCalledTimes(1);
  expect(navigate).not.toHaveBeenCalled();
  await act(async () => { complete({ $meta: { status: 200 } }); });
  expect(navigate).toHaveBeenCalledTimes(1);
  view.unmount();
  runtime.destroy();
});

it.each(["en-US", "zh-CN"])("leaves the completed queue without resaving confirmed history in %s", (locale) => {
  const save = jest.fn().mockResolvedValue({ $meta: { status: 409 } });
  window.Htx = { annotationStore: { selected: { hasUnsavedDraft: false, submissionStarted: 0, history: { hasChanges: true }, saveDraftImmediatelyWithResults: save } } };
  const runtime = createLocaleRuntime(locale);
  const Provider = runtime.provider;
  mockBlock.mockReturnValue(jest.fn());
  const view = render(<Provider><ToastContext.Provider value={{ show: jest.fn() }}><DraftGuard /></ToastContext.Provider></Provider>);
  expect(mockBlock.mock.calls[0][0]()).toBeUndefined();
  expect(save).not.toHaveBeenCalled();
  view.unmount();
  runtime.destroy();
});

it("saves pending media edits even after Undo history was reset", async () => {
  const save = jest.fn().mockResolvedValue({ $meta: { status: 200 } });
  window.Htx = { annotationStore: { selected: { hasUnsavedDraft: true, history: { hasChanges: false }, saveDraftImmediatelyWithResults: save } } };
  const runtime = createLocaleRuntime("en-US");
  const Provider = runtime.provider;
  mockBlock.mockReturnValue(jest.fn());
  const view = render(<Provider><ToastContext.Provider value={{ show: jest.fn() }}><DraftGuard /></ToastContext.Provider></Provider>);
  await act(async () => { expect(mockBlock.mock.calls[0][0]()).toBe(DRAFT_GUARD_KEY); });
  expect(save).toHaveBeenCalledTimes(1);
  view.unmount();
  runtime.destroy();
});
