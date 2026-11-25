import { destroy, types } from "mobx-state-tree";
import { LSFWrapper } from "../sdk/lsf-sdk";
import { History } from "../utils/history";

jest.mock("../components/Common/Modal/Modal", () => ({ Modal: {} }));
jest.mock("./DataStores", () => ({}));
jest.mock("./Tabs", () => {
  const { types } = jest.requireActual("mobx-state-tree");
  return { TabStore: types.model({ views: types.array(types.frozen()) }).actions(() => ({ fetchColumns() {} })) };
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

it("waits for a blind write and its refresh before saving the draft and navigating", async () => {
  const { store, wrapper } = makeStore();
  const write = deferred();
  const navigate = jest.fn();
  store._sdk.settings = { blindProtocol: true };
  wrapper.pendingBlindWrite = write.promise;
  try {
    const leaving = store.navigateWithSavedDraft(navigate);
    await nextTick();
    expect(wrapper.saveDraft).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
    write.resolve({ $meta: { status: 201 } });
    expect(await leaving).toBe(true);
    expect(wrapper.saveDraft).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledTimes(1);
  } finally {
    destroy(store);
  }
});

it.each(["failed", "rejected"])("keeps the editor open when a pending blind write %s", async (failure) => {
  const { store, wrapper } = makeStore();
  const navigate = jest.fn();
  store._sdk.settings = { blindProtocol: true };
  wrapper.pendingBlindWrite = failure === "failed"
    ? Promise.resolve({ $meta: { status: 409 } })
    : Promise.reject(new Error("network failure"));
  try {
    expect(await store.navigateWithSavedDraft(navigate)).toBe(false);
    expect(wrapper.saveDraft).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  } finally {
    destroy(store);
  }
});

beforeEach(() => {
  jest.spyOn(History, "navigate").mockImplementation(() => {});
  jest.spyOn(History, "getParams").mockReturnValue({});
  // Paint immediately; request/reset promises below control the ordering.
  jest.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => { callback(); return 0; });
});
afterEach(() => jest.restoreAllMocks());

it("blocks legacy task calls in blind mode and supplies the active handle for draft writes", async () => {
  const { store, wrapper } = makeStore();
  const task = 2 ** 51 + 11;
  const draft = 2 ** 51 + 12;
  const assignment = 2 ** 51 + 13;
  const rawTasks = jest.fn();
  const update = jest.fn().mockResolvedValue({ id: draft, revision: 2 });
  const project = jest.fn().mockResolvedValue({ id: 7 });
  const work = jest.fn().mockResolvedValue({ results: [], page: 1, has_next: false });
  wrapper.task = { id: task };
  Object.assign(store._sdk, { settings: { blindProtocol: true }, projectId: 7,
    api: { tasks: rawTasks, blindUpdateDraft: update, blindProject: project, blindWork: work } });
  try {
    store._sdk.settings.blindProtocol = "true";
    expect((await store.apiCall("tasks", {})).status).toBe(403);
    store._sdk.settings.blindProtocol = true;
    expect(await store.apiCall("tasks", {})).toEqual({ error: "Work operation is unavailable", status: 403 });
    expect(rawTasks).not.toHaveBeenCalled();
    store._sdk.api.sharedParams = { taskID: 7 };
    expect((await store.apiCall("updateDraft", { draftID: draft }, { body: {} })).status).toBe(403);
    expect(update).not.toHaveBeenCalled();
    store._sdk.api.sharedParams = {};
    await store.apiCall("project", { project: 99 });
    expect(project).toHaveBeenCalledWith({ project: 7 }, expect.anything());
    await store.apiCall("workList", { page: 1 });
    expect(work).toHaveBeenCalledWith({ projectID: 7, page: 1 }, expect.anything());
    await store.apiCall("updateDraft", { draftID: draft }, {
      body: { draft_revision: 1, assignment_id: assignment, assignment_version: 4 },
    });
    expect(update).toHaveBeenCalledWith({ taskID: task, draftID: draft }, expect.objectContaining({
      body: { draft_revision: 1, assignment_id: assignment, assignment_version: 4 },
    }));
    await store.apiCall("updateDraft", { taskID: task + 1, draftID: draft }, { body: {} });
    expect(update).toHaveBeenCalledTimes(1);
    update.mockResolvedValueOnce({ error: "conflict", status: 409 });
    const conflict = await store.apiCall("updateDraft", { draftID: draft }, {
      body: { draft_revision: 1, assignment_id: assignment, assignment_version: 4 },
    }, { errorHandler: () => true });
    expect(conflict.status).toBe(409);
    expect(update).toHaveBeenCalledTimes(2);
  } finally {
    destroy(store);
  }
});

it("loads and selects a restricted task exactly once before opening the editor", async () => {
  const { store, wrapper } = makeStore();
  const taskID = 2 ** 51 + 21;
  const loaded = { id: taskID };
  store._sdk.settings = { blindProtocol: true };
  store.taskStore.loadTask.mockImplementation(async () => {
    store.taskStore.selected = loaded;
    return loaded;
  });
  try {
    expect(await store.setTask({ taskID })).toBe(true);
    expect(store.taskStore.setSelected).not.toHaveBeenCalled();
    expect(store.taskStore.loadTask).toHaveBeenCalledTimes(1);
    expect(store.taskStore.loadTask).toHaveBeenCalledWith(taskID, { select: true });
    expect(wrapper.setLSFTask).toHaveBeenCalledWith(loaded, expect.anything(), undefined, false);
  } finally {
    destroy(store);
  }
});

it("opens the real restricted task store from an opaque API response", async () => {
  const { create } = require("./DataStores/tasks");
  const Combined = types.compose(AppStore, types.model({
    taskStore: create([{ id: "data", type: "List", target: "tasks" }]),
    annotationStore: types.frozen(),
  }));
  const store = Combined.create({ toolbar: "", taskStore: {}, annotationStore: {} });
  const taskID = 2 ** 51 + 31;
  const response = { id: taskID, project: 7, assignment_id: taskID + 1, assignment_version: 1,
    can_label: true, data: { text: "synthetic" }, annotations: [], drafts: [], predictions: [],
    default_selected_annotation: null };
  const wrapper = { saveDraft: jest.fn().mockResolvedValue(true), setLoading: jest.fn(),
    setLSFTask: jest.fn().mockResolvedValue(true), currentAnnotation: { id: "local" },
    lsf: { annotationStore: { annotations: [], predictions: [] } } };
  const read = jest.fn().mockResolvedValue(response);
  store._sdk = { settings: { blindProtocol: true }, projectId: 7, lsf: wrapper,
    setMode: jest.fn(), invoke: jest.fn(), api: { sharedParams: {}, blindTask: read } };
  try {
    expect(await store.setTask({ taskID })).toBe(true);
    expect(read).toHaveBeenCalledTimes(1);
    expect(read).toHaveBeenCalledWith({ taskID }, expect.anything());
    expect(store.taskStore.selected.id).toBe(taskID);
    expect(store.taskStore.selected.data).toEqual({ text: "synthetic" });
    expect(wrapper.setLSFTask).toHaveBeenCalledWith(store.taskStore.selected, "local", undefined, false);
  } finally {
    destroy(store);
  }
});

it("restores an opaque task on restricted workspace startup without using legacy routes", async () => {
  const { create } = require("./DataStores/tasks");
  const Combined = types.compose(AppStore, types.model({
    taskStore: create([{ id: "data", type: "List", target: "tasks" }]), annotationStore: types.frozen(),
  }));
  const store = Combined.create({ toolbar: "", taskStore: {}, annotationStore: {} });
  const taskID = 2 ** 51 + 41;
  const read = jest.fn().mockResolvedValue({ id: taskID, project: 7, assignment_id: taskID + 1,
    assignment_version: 1, can_label: true, data: { text: "synthetic" }, annotations: [], drafts: [],
    predictions: [], default_selected_annotation: null });
  const wrapper = { saveDraft: jest.fn().mockResolvedValue(true), setLoading: jest.fn(),
    setLSFTask: jest.fn().mockResolvedValue(true), currentAnnotation: { id: "local" },
    lsf: { annotationStore: { annotations: [], predictions: [] } } };
  store._sdk = { settings: { blindProtocol: true }, projectId: 7, lsf: wrapper,
    setMode: jest.fn(), invoke: jest.fn(), api: { sharedParams: {}, blindTask: read,
      blindProject: jest.fn().mockResolvedValue({ id: 7 }),
      blindWork: jest.fn().mockResolvedValue({ results: [], page: 1, total: 0, has_next: false }) } };
  History.getParams.mockReturnValue({ task: String(taskID) });
  try {
    await store.fetchData();
    expect(store.loading).toBe(false);
    expect(store.taskStore.selected.id).toBe(taskID);
    expect(read).toHaveBeenCalledTimes(1);
    expect(wrapper.setLSFTask).toHaveBeenCalledTimes(1);
  } finally { destroy(store); }
});

it("does not open a legacy task number from a restricted URL", async () => {
  const { store, wrapper } = makeStore();
  store._sdk.settings = { blindProtocol: true };
  store._sdk.projectId = 7;
  store._sdk.api = { sharedParams: {}, blindProject: jest.fn().mockResolvedValue({ id: 7 }),
    blindWork: jest.fn().mockResolvedValue({ results: [], page: 1, total: 0, has_next: false }) };
  History.getParams.mockReturnValue({ task: "42" });
  try {
    await store.fetchData();
    expect(store.taskStore.loadTask).not.toHaveBeenCalled();
    expect(wrapper.setLSFTask).not.toHaveBeenCalled();
    expect(store.blindWorkError).toBe(true);
    expect(History.navigate).toHaveBeenCalledWith({ task: null, annotation: null, region: null }, true);
  } finally { destroy(store); }
});

it("clears the editor and refuses navigation when restricted work is unavailable", async () => {
  const { store, wrapper } = makeStore();
  store._sdk.settings = { blindProtocol: true };
  store._sdk.destroyLSF = jest.fn();
  store.taskStore.loadTask.mockResolvedValue(null);
  store.taskStore.unset = jest.fn();
  store.annotationStore.unset = jest.fn();
  try {
    expect(await store.setTask({ taskID: 2 ** 51 + 21 })).toBe(false);
    expect(store.taskStore.unset).toHaveBeenCalled();
    expect(store.annotationStore.unset).toHaveBeenCalled();
    expect(store.blindWorkPage).toBeNull();
    expect(store.blindWorkError).toBe(true);
    expect(store._sdk.destroyLSF).toHaveBeenCalled();
    expect(store._sdk.setMode).toHaveBeenLastCalledWith("explorer");
    expect(wrapper.setLSFTask).not.toHaveBeenCalled();
    expect(store.loadingData).toBe(false);
  } finally {
    destroy(store);
  }
});

it.each(["all", "filtered"])("keeps a package queue independent of the last %s project view", async (mode) => {
  const { store } = makeStore();
  const request = jest.fn().mockResolvedValue({ id: 41 });
  Object.assign(store._sdk, { api: { invokeAction: request }, getAction: () => null, invoke: jest.fn() });
  History.getParams.mockReturnValue({ task_package: "7" });
  localStorage.setItem("dm:labelstream:mode", mode);
  try {
    await store.invokeAction("next_task", { reload: false });
    expect(request.mock.calls[0][0]).toEqual({ id: "next_task", task_package: "7" });
    expect(request.mock.calls[0][1].body).toEqual({});
  } finally { localStorage.removeItem("dm:labelstream:mode"); destroy(store); }
});

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
