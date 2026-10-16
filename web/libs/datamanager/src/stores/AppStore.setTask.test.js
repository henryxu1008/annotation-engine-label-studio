import { destroy } from "mobx-state-tree";
import { LSFWrapper } from "../sdk/lsf-sdk";
import { History } from "../utils/history";

jest.mock("../components/Common/Modal/Modal", () => ({ Modal: {} }));
jest.mock("./DataStores", () => ({}));
jest.mock("./Tabs", () => {
  const { types } = jest.requireActual("mobx-state-tree");
  return { TabStore: types.model({ views: types.array(types.frozen()) }) };
});
jest.mock("./Users", () => {
  const { types } = jest.requireActual("mobx-state-tree");
  return { User: types.model({ id: types.identifierNumber }) };
});
jest.mock("../utils/ActivityObserver", () => ({ ActivityObserver: class { destroy() {} } }));
jest.mock("../utils/utils", () => ({ isDefined: (value) => value !== undefined && value !== null }));
jest.mock("../sdk/comments-sdk", () => ({ CommentsSdk: class {} }));
jest.mock("../sdk/lsf-utils", () => ({ taskToLSFormat: (task) => task, annotationToServer: jest.fn() }));
jest.mock("@humansignal/ui", () => ({ Button: () => null }), { virtual: true });
jest.mock("@humansignal/core", () => ({}), { virtual: true });
jest.mock("@humansignal/core/lib/utils/feature-flags", () => ({ isActive: () => false }), { virtual: true });
jest.mock("@humansignal/core/lib/utils/annotation-cache", () => ({}), { virtual: true });

window.APP_SETTINGS = { feature_flags: {} };
const { AppStore } = require("./AppStore");
const deferred = () => {
  let resolve;
  const promise = new Promise((finish) => { resolve = finish; });
  return { promise, resolve };
};
const nextTick = () => new Promise((resolve) => setTimeout(resolve, 0));

const makeStore = () => {
  const store = AppStore.create({ toolbar: "" });
  const task = { id: 41 };
  const annotation = { id: "local", pk: null, type: "annotation", regionStore: { setRegionVisible: jest.fn(), selectRegionByID: jest.fn() } };
  const editor = {
    isLoading: true,
    annotationStore: { selected: annotation, annotations: [annotation], predictions: [] },
    setFlags(flags) { Object.assign(this, flags); },
  };
  const wrapper = Object.create(LSFWrapper.prototype);
  Object.assign(wrapper, { destroyed: false, lsf: editor });
  wrapper.setLSFTask = jest.fn().mockResolvedValue();
  wrapper.saveDraft = jest.fn().mockResolvedValue(true);
  store.taskStore = { selected: task, setSelected: jest.fn(), loadTask: jest.fn().mockResolvedValue(task) };
  store.annotationStore = { setSelected: jest.fn() };
  store._sdk = { lsf: wrapper, setMode: jest.fn() };
  wrapper.datamanager = { store };
  return { store, wrapper, editor, annotation };
};

beforeEach(() => {
  jest.spyOn(History, "navigate").mockImplementation(() => {});
  jest.spyOn(History, "getParams").mockReturnValue({});
  // Paint immediately; request/reset promises below control the ordering.
  jest.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => { callback(); return 0; });
});
afterEach(() => jest.restoreAllMocks());

it("keeps edits closed when editor bootstrap finishes before the task refresh", async () => {
  const { store, wrapper, editor } = makeStore();
  const request = deferred();
  store.taskStore.loadTask.mockReturnValue(request.promise);
  try {
    const loading = store.setTask({ taskID: 41 });
    await nextTick();
    expect(store.taskStore.loadTask).toHaveBeenCalled();
    wrapper.setLoading(false); // onLabelStudioLoad finishes while setTask is pending
    expect(editor.isLoading).toBe(true);
    request.resolve({ id: 41 });
    await loading;
    expect(editor.isLoading).toBe(false);
  } finally {
    request.resolve({ id: 41 });
    await nextTick();
    destroy(store);
  }
});

it("awaits the annotation reset before releasing loading or restoring region selection", async () => {
  const { store, wrapper, editor, annotation } = makeStore();
  const reset = deferred();
  const restoredRegion = { setRegionVisible: jest.fn(), selectRegionByID: jest.fn() };
  wrapper.setLSFTask.mockImplementation(async () => {
    await reset.promise;
    editor.annotationStore.selected = { ...annotation, regionStore: restoredRegion };
  });
  History.getParams.mockReturnValue({ region: "region-1" });
  try {
    const loading = store.setTask({ taskID: 41 });
    await nextTick();
    expect(wrapper.setLSFTask).toHaveBeenCalledTimes(1);
    expect(store.loadingData).toBe(true);
    expect(annotation.regionStore.selectRegionByID).not.toHaveBeenCalled();
    reset.resolve();
    await loading;
    expect(annotation.regionStore.selectRegionByID).not.toHaveBeenCalled();
    expect(restoredRegion.selectRegionByID).toHaveBeenCalledWith("region-1");
    expect(store.loadingData).toBe(false);
    expect(editor.isLoading).toBe(false);
  } finally {
    reset.resolve();
    await nextTick();
    destroy(store);
  }
});

it("releases loading and propagates a failed reset", async () => {
  const { store, wrapper, editor } = makeStore();
  wrapper.setLSFTask.mockRejectedValue(new Error("reset failed"));
  try {
    await expect(store.setTask({ taskID: 41 })).rejects.toThrow("reset failed");
    expect(store.loadingData).toBe(false);
    expect(editor.isLoading).toBe(false);
  } finally {
    destroy(store);
  }
});

it("selects a prediction from the URL in one awaited reset", async () => {
  const { store, wrapper, editor } = makeStore();
  const prediction = { id: "prediction-local", pk: "71", type: "prediction" };
  editor.annotationStore.predictions.push(prediction);
  History.getParams.mockReturnValue({ annotation: "71" });
  try {
    await store.setTask({ taskID: 41 });
    expect(wrapper.setLSFTask).toHaveBeenCalledTimes(1);
    expect(wrapper.setLSFTask).toHaveBeenCalledWith(store.taskStore.selected, "71", undefined, true);
  } finally {
    destroy(store);
  }
});

it("keeps the outgoing task selected and its URL unchanged until its draft is confirmed", async () => {
  const { store, wrapper } = makeStore();
  const save = deferred();
  wrapper.saveDraft.mockReturnValue(save.promise);
  try {
    const switching = store.setTask({ taskID: 42 });
    await nextTick();
    expect(History.navigate).not.toHaveBeenCalled();
    expect(store._sdk.setMode).not.toHaveBeenCalled();
    expect(store.taskStore.setSelected).not.toHaveBeenCalled();
    expect(store.taskStore.loadTask).not.toHaveBeenCalled();
    save.resolve(true);
    await switching;
    expect(store.taskStore.loadTask).toHaveBeenCalledWith(42, expect.anything());
  } finally {
    save.resolve(true);
    await nextTick();
    destroy(store);
  }
});

it("does not discard the outgoing draft on failure and permits an explicit navigation retry", async () => {
  const { store, wrapper } = makeStore();
  wrapper.saveDraft.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  try {
    expect(await store.setTask({ taskID: 42 })).toBe(false);
    expect(History.navigate).not.toHaveBeenCalled();
    expect(store.taskStore.loadTask).not.toHaveBeenCalled();
    expect(wrapper.setLSFTask).not.toHaveBeenCalled();
    await store.setTask({ taskID: 42 });
    expect(store.taskStore.loadTask).toHaveBeenCalledTimes(1);
  } finally {
    destroy(store);
  }
});

it("ignores repeated task choices while the first outgoing draft flush is pending", async () => {
  const { store, wrapper } = makeStore();
  const save = deferred();
  wrapper.saveDraft.mockReturnValue(save.promise);
  try {
    const first = store.setTask({ taskID: 42 });
    const repeated = store.setTask({ taskID: 43 });
    await nextTick();
    expect(wrapper.saveDraft).toHaveBeenCalledTimes(1);
    save.resolve(true);
    await Promise.all([first, repeated]);
    expect(store.taskStore.loadTask).toHaveBeenCalledTimes(1);
    expect(store.taskStore.loadTask).toHaveBeenCalledWith(42, expect.anything());
  } finally {
    save.resolve(true);
    await nextTick();
    destroy(store);
  }
});

it("keeps the task panel open after a failed draft flush", async () => {
  const { store, wrapper } = makeStore();
  store.taskStore.unset = jest.fn();
  store.annotationStore.unset = jest.fn();
  store._sdk.setMode = jest.fn();
  store._sdk.destroyLSF = jest.fn();
  wrapper.saveDraft.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  try {
    expect(await store.closeLabeling()).toBe(false);
    expect(store.taskStore.unset).not.toHaveBeenCalled();
    expect(store._sdk.destroyLSF).not.toHaveBeenCalled();
    expect(await store.closeLabeling()).toBe(true);
    expect(store.taskStore.unset).toHaveBeenCalledTimes(1);
    expect(store._sdk.destroyLSF).toHaveBeenCalledTimes(1);
  } finally {
    destroy(store);
  }
});

it("restores the accepted task URL when browser history navigation cannot save", async () => {
  const { store, wrapper } = makeStore();
  const accepted = { task: "41", annotation: "17", tab: "3" };
  History.getParams.mockReturnValue(accepted);
  try {
    await store.setTask({ taskID: 41 });
    History.navigate.mockClear();
    store.taskStore.loadTask.mockClear();
    wrapper.saveDraft.mockResolvedValue(false);
    expect(await store.setTask({ taskID: 42, pushState: false })).toBe(false);
    expect(store.taskStore.loadTask).not.toHaveBeenCalled();
    expect(History.navigate).toHaveBeenLastCalledWith(expect.objectContaining(accepted), true);
  } finally {
    destroy(store);
  }
});

it("does not apply an old navigation intent to a replacement editor", async () => {
  const { store, wrapper } = makeStore();
  const save = deferred();
  wrapper.saveDraft.mockReturnValue(save.promise);
  try {
    const switching = store.setTask({ taskID: 42 });
    store._sdk.lsf = null;
    save.resolve(true);
    expect(await switching).toBe(false);
    expect(store.taskStore.loadTask).not.toHaveBeenCalled();
  } finally {
    save.resolve(true);
    destroy(store);
  }
});
