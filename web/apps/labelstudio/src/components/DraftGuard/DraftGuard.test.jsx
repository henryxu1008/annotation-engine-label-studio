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

it.each([200, 400])("keeps navigation blocked across a locale change until draft response %i", async (status) => {
  let complete;
  const save = jest.fn(() => new Promise((resolve) => { complete = resolve; }));
  window.Htx = { annotationStore: { selected: { history: { undoIdx: 1 }, saveDraftImmediatelyWithResults: save } } };
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
  expect(confirmNavigation).not.toHaveBeenCalled();
  expect(unsubscribe).not.toHaveBeenCalled();
  await act(async () => { complete({ $meta: { status } }); });
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
