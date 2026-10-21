import { DataManager } from "./dm-sdk";
import { LSFWrapper } from "./lsf-sdk";
import { createApp } from "./app-create";
import { destroy } from "mobx-state-tree";

jest.mock("./app-create", () => ({ createApp: jest.fn() }));
jest.mock("../components/DataManager/Toolbar/instruments", () => ({ instruments: {} }));
jest.mock("../utils/api-proxy", () => ({ APIProxy: class {} }));
jest.mock("mobx-state-tree", () => ({ ...jest.requireActual("mobx-state-tree"), destroy: jest.fn() }));
jest.mock("react-dom", () => ({ ...jest.requireActual("react-dom"), unmountComponentAtNode: jest.fn() }));

const makeManager = () => new DataManager({
  root: document.createElement("div"),
  settings: {},
  labelStudio: {},
  polling: false,
  projectId: 7,
});
const nextTick = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => jest.clearAllMocks());

it("updates Data Manager and nested editor locale without recreating either store", async () => {
  const store = { submitAnnotation: jest.fn(), saveDraft: jest.fn() };
  createApp.mockResolvedValue(store);
  const manager = makeManager();
  await nextTick();
  const setEditorLocale = jest.fn();
  manager.lsf = { lsfInstance: { setLocale: setEditorLocale }, destroy: jest.fn() };

  expect(manager.setLocale("zh-CN")).toBe(true);
  expect(manager.localeRuntime.t("datamanager:filters")).toBe("筛选条件");
  expect(setEditorLocale).toHaveBeenCalledWith("zh-CN");
  expect(createApp).toHaveBeenCalledTimes(1);
  expect(store.submitAnnotation).not.toHaveBeenCalled();
  expect(store.saveDraft).not.toHaveBeenCalled();

  manager.destroy();
  expect(destroy).toHaveBeenCalledWith(store);
  expect(manager.setLocale("en-US")).toBe(false);
});

it("does not adopt a store returned after destroy", async () => {
  let resolveApp;
  createApp.mockReturnValue(new Promise((resolve) => { resolveApp = resolve; }));
  const manager = makeManager();
  manager.destroy();
  const store = { submitAnnotation: jest.fn() };
  resolveApp(store);
  await nextTick();
  expect(manager.store).toBeNull();
  expect(destroy).toHaveBeenCalledWith(store);
});

it("refreshes persistent task guidance on locale change without resetting annotation access or data", async () => {
  const store = { submitAnnotation: jest.fn(), saveDraft: jest.fn() };
  createApp.mockResolvedValue(store);
  const manager = makeManager();
  await nextTick();
  const annotation = { result: [{ value: { choices: ["Positive"] } }], setReadonly: jest.fn() };
  const result = annotation.result;
  const invoke = jest.fn();
  manager.invoke = invoke;
  manager.lsf = {
    task: { can_label: false },
    lsf: { toggleInterface: jest.fn() },
    lsfInstance: { setLocale: jest.fn() },
    annotations: [annotation],
    currentAnnotation: annotation,
    datamanager: manager,
    refreshTaskAccessGuidance: LSFWrapper.prototype.refreshTaskAccessGuidance,
    destroy: jest.fn(),
  };
  LSFWrapper.prototype.applyTaskAccess.call(manager.lsf);
  annotation.setReadonly.mockClear();
  manager.lsf.lsf.toggleInterface.mockClear();

  expect(manager.setLocale("zh-CN")).toBe(true);
  expect(invoke).toHaveBeenLastCalledWith("toast", expect.objectContaining({
    id: "task-assignment-read-only",
    message: "此任务为只读。请先联系项目管理员，将任务分配给你后再标注。",
    duration: -1,
  }));
  expect(manager.setLocale("en-US")).toBe(true);
  expect(invoke).toHaveBeenLastCalledWith("toast", expect.objectContaining({
    message: "This task is read-only. Ask a project manager to assign it to you before labeling.",
  }));
  expect(annotation.result).toBe(result);
  expect(annotation.setReadonly).not.toHaveBeenCalled();
  expect(manager.lsf.lsf.toggleInterface).not.toHaveBeenCalled();
  expect(createApp).toHaveBeenCalledTimes(1);
  expect(store.submitAnnotation).not.toHaveBeenCalled();
  expect(store.saveDraft).not.toHaveBeenCalled();

  manager.lsf.task = null;
  expect(manager.setLocale("zh-CN")).toBe(true);
  expect(invoke).toHaveBeenLastCalledWith("toast:dismiss", { id: "task-assignment-read-only" });
  manager.lsf.destroyed = true;
  invoke.mockClear();
  expect(manager.setLocale("en-US")).toBe(true);
  expect(invoke).not.toHaveBeenCalled();
  manager.destroy();
  invoke.mockClear();
  expect(manager.setLocale("zh-CN")).toBe(false);
  expect(invoke).not.toHaveBeenCalled();
});

it("shares an in-flight task selection so a second caller cannot reset the editor", async () => {
  createApp.mockResolvedValue({});
  const manager = makeManager();
  await nextTick();
  let finishSelection;
  const selection = new Promise((resolve) => { finishSelection = resolve; });
  manager.store = {
    taskStore: { selected: { id: 41, lastAnnotation: null } },
    annotationStore: { selected: null },
  };
  manager.lsf = { task: null, selectTask: jest.fn(() => selection), destroy: jest.fn() };

  const first = manager.startLabeling();
  const second = manager.startLabeling();
  await Promise.resolve();
  expect(manager.lsf.selectTask).toHaveBeenCalledTimes(1);
  expect(manager.taskSelectionPromise).toBeTruthy();
  finishSelection();
  await Promise.all([first, second]);
  expect(manager.taskSelectionPromise).toBeNull();
  manager.destroy();
});

it("loads the latest selected task after an earlier selection finishes", async () => {
  createApp.mockResolvedValue({});
  const manager = makeManager();
  await nextTick();
  const pending = [];
  manager.store = {
    taskStore: { selected: { id: 41, lastAnnotation: null } },
    annotationStore: { selected: null },
  };
  manager.lsf = {
    task: null,
    selectTask: jest.fn((task) => {
      manager.lsf.task = task;
      return new Promise((resolve) => pending.push(resolve));
    }),
    destroy: jest.fn(),
  };

  const first = manager.startLabeling();
  await Promise.resolve();
  manager.store.taskStore.selected = { id: 42, lastAnnotation: null };
  const second = manager.startLabeling();
  manager.store.taskStore.selected = { id: 43, lastAnnotation: null };
  const third = manager.startLabeling();
  await Promise.resolve();
  expect(manager.lsf.selectTask).toHaveBeenCalledTimes(1);
  pending.shift()();
  await nextTick();
  expect(manager.lsf.selectTask).toHaveBeenCalledTimes(2);
  expect(manager.lsf.selectTask.mock.calls[1][0].id).toBe(43);
  expect(manager.taskSelectionPromise).toBeTruthy();
  pending.shift()();
  await Promise.all([first, second, third]);
  expect(manager.lsf.task.id).toBe(43);
  expect(manager.taskSelectionPromise).toBeNull();
  manager.destroy();
});

it("does not reuse an old editor selection after reload", async () => {
  const oldStore = {
    taskStore: { selected: { id: 41, lastAnnotation: null } },
    annotationStore: { selected: null },
  };
  const newStore = {
    taskStore: { selected: { id: 42, lastAnnotation: null } },
    annotationStore: { selected: null },
  };
  createApp.mockResolvedValueOnce(oldStore).mockResolvedValueOnce(newStore);
  const manager = makeManager();
  await nextTick();
  let finishOldSelection;
  const oldSelection = new Promise((resolve) => { finishOldSelection = resolve; });
  const oldEditor = { task: null, selectTask: jest.fn(() => oldSelection), destroy: jest.fn() };
  manager.lsf = oldEditor;

  const pendingOld = manager.startLabeling();
  await Promise.resolve();
  expect(oldEditor.selectTask).toHaveBeenCalledTimes(1);

  manager.reload();
  await nextTick();
  const newEditor = { task: null, selectTask: jest.fn().mockResolvedValue(), destroy: jest.fn() };
  manager.lsf = newEditor;
  await manager.startLabeling();
  expect(newEditor.selectTask).toHaveBeenCalledWith(newStore.taskStore.selected, undefined);
  expect(oldEditor.destroy).toHaveBeenCalledTimes(1);

  finishOldSelection();
  await pendingOld;
  expect(manager.taskSelectionPromise).toBeNull();
  manager.destroy();
});
