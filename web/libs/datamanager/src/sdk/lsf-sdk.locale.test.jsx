import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { createLocaleRuntime } from "@humansignal/i18n";
import { LSFWrapper } from "./lsf-sdk";
import { Modal } from "../components/Common/Modal/Modal";

function createDraftWrapper() {
  const previousSettings = window.APP_SETTINGS;
  window.APP_SETTINGS = previousSettings ?? {};
  const init = jest.spyOn(LSFWrapper.prototype, "initLabelStudio").mockImplementation(() => {});
  try {
    return new LSFWrapper({ store: { project: { id: 7 }, users: [] }, hasInterface: () => false }, document.createElement("div"), {});
  } finally {
    init.mockRestore();
    window.APP_SETTINGS = previousSettings;
  }
}

it("advances draft tokens only after success and never adopts the remote revision on conflict", () => {
  const wrapper = createDraftWrapper();
  const annotation = { setDraftRevision: jest.fn(), setDraftConflict: jest.fn() };
  wrapper.handleDraftResponse(annotation, { $meta: { status: 200 }, revision: 3 });
  expect(annotation.setDraftRevision).toHaveBeenCalledWith(3);
  wrapper.handleDraftResponse(annotation, { $meta: { status: 409 }, revision: 8,
    response: { display_context: { reason: "DRAFT_CONFLICT" } } });
  expect(annotation.setDraftConflict).toHaveBeenCalledTimes(1);
  expect(annotation.setDraftRevision).toHaveBeenCalledTimes(1);
});

it.each([404, 403, 503])("handles a disappeared existing draft separately from transient or permission errors: %s", async (status) => {
  const wrapper = createDraftWrapper();
  const annotation = { draftId: 7, setDraftRevision: jest.fn(), setDraftConflict: jest.fn() };
  wrapper.task = { id: 42 };
  wrapper.prepareData = () => ({ draft_id: 7, result: [{ value: 'local edit' }] });
  wrapper.needsDraftSave = () => true;
  wrapper.saveUserLabels = jest.fn();
  wrapper.datamanager.invoke = jest.fn();
  const response = { $meta: { status } };
  wrapper.datamanager.apiCall = jest.fn().mockResolvedValue(response);
  expect(await wrapper.onSubmitDraft({}, annotation)).toBe(response);
  expect(wrapper.datamanager.apiCall).toHaveBeenCalledTimes(1);
  expect(wrapper.datamanager.apiCall.mock.calls[0][0]).toBe('updateDraft');
  const options = wrapper.datamanager.apiCall.mock.calls[0][3];
  expect(options.errorHandler(response)).toBe(status === 404);
  expect(annotation.setDraftConflict.mock.calls).toEqual(status === 404 ? [['missing']] : []);
  expect(annotation.setDraftRevision).not.toHaveBeenCalled();
  expect(annotation.draftId).toBe(7);
  wrapper.handleDraftResponse(annotation, response); // Other endpoints' 404 is not proof of a missing draft.
  expect(annotation.setDraftConflict).toHaveBeenCalledTimes(status === 404 ? 1 : 0);
});

it.each(['submit', 'update'])("waits for this page's pending draft acknowledgement before %s", async (kind) => {
  const wrapper = createDraftWrapper();
  let finish;
  const annotation = { pk: 17, draftRevision: 1, draftSavePromise: new Promise(resolve => { finish = resolve; }) };
  wrapper.task = { id: 42 };
  wrapper.lsf = { annotationStore: { selected: annotation } };
  wrapper.prepareData = jest.fn(() => ({ draft_revision: annotation.draftRevision }));
  wrapper.withinLoadingState = fn => fn();
  wrapper.saveUserLabels = jest.fn();
  wrapper.datamanager.invoke = jest.fn();
  wrapper.shouldExitStream = () => false;
  wrapper.showOperationToast = jest.fn();
  const request = jest.fn().mockResolvedValue({ $meta: { status: 503 } });
  wrapper.datamanager.apiCall = request;
  const saving = kind === 'submit'
    ? wrapper.submitCurrentAnnotation('submitAnnotation', request)
    : wrapper.onUpdateAnnotation({}, annotation);
  expect(wrapper.prepareData).not.toHaveBeenCalled();
  annotation.draftRevision = 2;
  finish({ $meta: { status: 200 } });
  await saving;
  expect(wrapper.prepareData).toHaveBeenCalled();
  if (kind === 'submit') expect(request).toHaveBeenCalledWith(expect.anything(), { draft_revision: 2 });
  else expect(request.mock.calls[0][2].body).toEqual({ draft_revision: 2 });
});

it("reloads fresh server results without merging obsolete local annotations", async () => {
  const wrapper = createDraftWrapper();
  const old = { id: 42, drafts: [{ id: 1, revision: 1 }] };
  const fresh = { id: 42, drafts: [{ id: 1, revision: 2 }] };
  wrapper.task = old;
  wrapper.lsf = {};
  wrapper.datamanager.store.taskStore = { loadTask: jest.fn().mockResolvedValueOnce(null).mockResolvedValueOnce(fresh) };
  wrapper.setLSFTask = jest.fn();
  expect(await wrapper.onReloadDraft()).toBe(false);
  expect(wrapper.task).toBe(old);
  expect(wrapper.setLSFTask).not.toHaveBeenCalled();
  expect(await wrapper.onReloadDraft()).toBe(true);
  expect(wrapper.task).toBe(fresh);
  expect(wrapper.setLSFTask).toHaveBeenCalledWith(fresh, undefined, true);
});

it.each([17, { id: 17 }, null])("selects the returned owner's latest submitted annotation without fabricating a new draft: %j", async (owner) => {
  const wrapper = createDraftWrapper();
  const task = { id: 42, drafts: [], annotations: [
    { id: 100, completed_by: owner }, { id: 99, completed_by: owner }, { id: 101, completed_by: 18 },
  ] };
  wrapper.task = { id: 42 };
  wrapper.lsf = { user: { id: 17 } };
  wrapper.datamanager.store.taskStore = { loadTask: jest.fn().mockResolvedValue(task) };
  wrapper.setLSFTask = jest.fn();
  expect(await wrapper.onReloadDraft()).toBe(true);
  expect(wrapper.setLSFTask).toHaveBeenCalledWith(task, owner === null ? undefined : "100", true);
  expect(task.annotations.map(a => a.id)).toEqual([100, 99, 101]);
});

it.each([200, 409])("does not delete a skipped annotation after its draft write returns %s", async (status) => {
  const wrapper = createDraftWrapper();
  const selected = { pk: 17, draftId: 7, pauseAutosave: jest.fn(), setDraftConflict: jest.fn(), setDraftRevision: jest.fn() };
  wrapper.task = { id: 42 };
  wrapper.lsf = { annotationStore: { selected } };
  wrapper.prepareData = jest.fn().mockReturnValue({ draft_id: 7, draft_revision: 1 });
  wrapper.withinLoadingState = async (fn) => fn();
  wrapper.datamanager.invoke = jest.fn();
  wrapper.datamanager.apiCall = jest.fn().mockResolvedValueOnce({ $meta: { status },
    ...(status === 409 ? { response: { display_context: { reason: 'DRAFT_CONFLICT' } } } : { revision: 2 })
  }).mockResolvedValueOnce({ $meta: { status: 204 } });
  wrapper.loadTask = jest.fn();
  await wrapper.onUnskipTask();
  expect(wrapper.datamanager.apiCall).toHaveBeenCalledTimes(status === 200 ? 2 : 1);
  expect(wrapper.loadTask).toHaveBeenCalledTimes(status === 200 ? 1 : 0);
  expect(selected.setDraftConflict).toHaveBeenCalledTimes(status === 409 ? 1 : 0);
});

it.each([0, 503, 201])("returns the real submit response %s and exits only after success", async (status) => {
  const wrapper = createDraftWrapper();
  const result = { $meta: { status } };
  wrapper.shouldExitStream = () => true;
  wrapper.submitCurrentAnnotation = jest.fn().mockResolvedValue(result);
  wrapper.showOperationToast = jest.fn();
  wrapper.exitStream = jest.fn();
  expect(await wrapper.onSubmitAnnotation()).toBe(result);
  expect(wrapper.exitStream).toHaveBeenCalledTimes(status === 201 ? 1 : 0);
});

it.each([0, 503, 200])("returns the real update response %s and keeps failed edits on the same task", async (status) => {
  const wrapper = createDraftWrapper();
  const result = { $meta: { status } };
  wrapper.task = { id: 42 };
  wrapper.prepareData = jest.fn().mockReturnValue({});
  wrapper.shouldExitStream = () => true;
  wrapper.saveUserLabels = jest.fn();
  wrapper.lsf = { setFlags: jest.fn() };
  wrapper.datamanager.apiCall = jest.fn().mockResolvedValue(result);
  wrapper.datamanager.invoke = jest.fn();
  wrapper.showOperationToast = jest.fn();
  wrapper.exitStream = jest.fn();
  wrapper.loadTask = jest.fn();
  expect(await wrapper.onUpdateAnnotation({}, { pk: 17 })).toBe(result);
  expect(wrapper.exitStream).toHaveBeenCalledTimes(status === 200 ? 1 : 0);
  expect(wrapper.loadTask).not.toHaveBeenCalled();
});

it.each([0, 503, "offline"])("does not reload or remain loading when submit fails: %s", async (status) => {
  const wrapper = createDraftWrapper();
  wrapper.task = { id: 42 };
  wrapper.lsf = { annotationStore: { selected: {} }, setFlags: jest.fn() };
  wrapper.prepareData = jest.fn().mockReturnValue({});
  wrapper.saveUserLabels = jest.fn();
  wrapper.loadTask = jest.fn();
  const result = { $meta: { status } };
  const submit = status === "offline" ? jest.fn().mockRejectedValue(new Error("offline")) : jest.fn().mockResolvedValue(result);
  const pending = wrapper.submitCurrentAnnotation("submitAnnotation", submit);
  if (status === "offline") await expect(pending).rejects.toThrow("offline");
  else expect(await pending).toBe(result);
  expect(wrapper.loadTask).not.toHaveBeenCalled();
  expect(wrapper.lsf.setFlags).toHaveBeenLastCalledWith({ isLoading: false });
});

it.each(["next", "prev"])("keeps the current task when %s navigation cannot save edits", async (direction) => {
  const wrapper = createDraftWrapper();
  wrapper.saveDraft = jest.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  wrapper.loadTask = jest.fn();
  const navigate = direction === "next" ? wrapper.onNextTask : wrapper.onPrevTask;
  await navigate(42, 17);
  expect(wrapper.loadTask).not.toHaveBeenCalled();
  await navigate(42, 17);
  expect(wrapper.loadTask).toHaveBeenCalledWith(42, 17, true);
});

it("saves an annotation undone to index zero before publishing its selection", async () => {
  const wrapper = createDraftWrapper();
  wrapper.datamanager.invoke = jest.fn();
  wrapper.saveDraft = jest.fn().mockResolvedValue(true);
  const outgoing = { hasUnsavedDraft: true, history: { undoIdx: 0, hasChanges: true } };
  await wrapper._invokeSelectAnnotation(null, outgoing, {});
  expect(wrapper.saveDraft).toHaveBeenCalledWith(outgoing);
  expect(wrapper.datamanager.invoke).toHaveBeenCalledWith("onSelectAnnotation", null, outgoing, {}, wrapper);
});

it.each([true, false])("uses persisted confirmation, rather than reset/stale Undo history, for draft navigation: %s", async (dirty) => {
  const wrapper = createDraftWrapper();
  const annotation = { hasUnsavedDraft: dirty, history: { hasChanges: !dirty }, saveDraftImmediatelyWithResults: jest.fn().mockResolvedValue({ $meta: { status: 200 } }) };
  wrapper.lsf = { annotationStore: { selected: annotation } };
  wrapper.draftToast = jest.fn();
  expect(await wrapper.saveDraft()).toBe(true);
  expect(annotation.saveDraftImmediatelyWithResults).toHaveBeenCalledTimes(dirty ? 1 : 0);
  wrapper.datamanager.invoke = jest.fn();
  wrapper.saveDraft = jest.fn().mockResolvedValue(true);
  await wrapper._invokeSelectAnnotation(null, annotation, {});
  expect(wrapper.saveDraft).toHaveBeenCalledTimes(dirty ? 1 : 0);
  expect(wrapper.datamanager.invoke).toHaveBeenCalledWith("onSelectAnnotation", null, annotation, {}, wrapper);
});

it.each([200, 403, "offline"])("uses the actual in-flight draft result %s instead of inventing success", async (status) => {
  let complete;
  const pending = new Promise((resolve, reject) => { complete = status === "offline" ? reject : resolve; });
  const selected = { isDraftSaving: true, saveDraftImmediatelyWithResults: jest.fn(() => pending) };
  const wrapper = createDraftWrapper();
  wrapper.lsf = { annotationStore: { selected } };
  wrapper.needsDraftSave = () => true;
  wrapper.draftToast = jest.fn();
  const saving = wrapper.saveDraft();
  expect(wrapper.draftToast).not.toHaveBeenCalled();
  complete(status === "offline" ? new Error("offline") : { $meta: { status } });
  expect(await saving).toBe(status === 200);
  if (status === "offline") expect(wrapper.draftToast).toHaveBeenCalledWith(0);
  else expect(wrapper.draftToast).toHaveBeenCalledWith(status, { $meta: { status } });
});

it.each(["en-US", "zh-CN"])("makes an unassigned manager task read-only with %s guidance", (locale) => {
  const runtime = createLocaleRuntime(locale);
  const selected = { setReadonly: jest.fn() };
  const prior = { setReadonly: jest.fn() };
  const wrapper = {
    task: { can_label: false },
    editorInterfaces: ["submit", "update", "skip", "annotations:add-new", "annotations:delete", "ground-truth"],
    lsf: { toggleInterface: jest.fn() },
    annotations: [prior],
    currentAnnotation: selected,
    datamanager: { localeRuntime: runtime, invoke: jest.fn() },
    refreshTaskAccessGuidance: LSFWrapper.prototype.refreshTaskAccessGuidance,
  };
  LSFWrapper.prototype.applyTaskAccess.call(wrapper);
  expect(selected.setReadonly).toHaveBeenCalledWith(true);
  expect(prior.setReadonly).toHaveBeenCalledWith(true);
  expect(wrapper.lsf.toggleInterface.mock.calls.every(([, enabled]) => enabled === false)).toBe(true);
  expect(wrapper.datamanager.invoke).toHaveBeenCalledWith("toast", expect.objectContaining({
    message: locale === "zh-CN" ? "此任务为只读。请先联系项目管理员，将任务分配给你后再标注。"
      : "This task is read-only. Ask a project manager to assign it to you before labeling.",
    duration: -1,
  }));
  wrapper.task.can_label = true;
  wrapper.lsf.toggleInterface.mockClear();
  LSFWrapper.prototype.applyTaskAccess.call(wrapper);
  expect(wrapper.lsf.toggleInterface).toHaveBeenCalledWith("submit", true);
  expect(wrapper.datamanager.invoke).toHaveBeenLastCalledWith("toast:dismiss", { id: "task-assignment-read-only" });
  runtime.destroy();
});

it.each(["en-US", "zh-CN"])("does not show completed work when a %s labeling queue request fails", async (locale) => {
  const runtime = createLocaleRuntime(locale);
  const confirm = jest.spyOn(Modal, "confirm").mockImplementation(() => {});
  const lsf = { setFlags: jest.fn() };
  const store = {
    taskStore: { loadNextTask: jest.fn().mockResolvedValue(false) },
    closeLabeling: jest.fn(),
  };
  const wrapper = {
    destroyed: false,
    lsf,
    labelStream: true,
    datamanager: { store, localeRuntime: runtime },
    withinLoadingState: async (callback) => callback(),
    selectTask: jest.fn(),
    loadTask: jest.fn().mockResolvedValue(undefined),
  };

  try {
    await LSFWrapper.prototype.loadTask.call(wrapper);
    expect(lsf.setFlags).toHaveBeenCalledWith({ noTask: false });
    expect(wrapper.selectTask).not.toHaveBeenCalled();
    const options = confirm.mock.lastCall[0];
    expect(options.title).toBe(locale === "zh-CN" ? "无法读取标注任务" : "Unable to load labeling tasks");
    expect(options.okText).toBe(locale === "zh-CN" ? "重试" : "Retry");
    expect(options.cancelText).toBe(locale === "zh-CN" ? "返回任务列表" : "Back to task list");
    await options.onOk();
    options.onCancel();
    expect(wrapper.loadTask).toHaveBeenCalledTimes(1);
    expect(store.closeLabeling).toHaveBeenCalledTimes(1);
    wrapper.destroyed = true;
    await options.onOk();
    options.onCancel();
    expect(wrapper.loadTask).toHaveBeenCalledTimes(1);
    expect(store.closeLabeling).toHaveBeenCalledTimes(1);
  } finally {
    confirm.mockRestore();
    runtime.destroy();
  }
});

it("keeps the completed state for a genuinely exhausted labeling queue", async () => {
  const confirm = jest.spyOn(Modal, "confirm").mockImplementation(() => {});
  const lsf = { setFlags: jest.fn() };
  const wrapper = {
    destroyed: false,
    lsf,
    labelStream: true,
    datamanager: { store: { taskStore: { loadNextTask: jest.fn().mockResolvedValue(null) } } },
    withinLoadingState: async (callback) => callback(),
    selectTask: jest.fn(),
  };
  try {
    await LSFWrapper.prototype.loadTask.call(wrapper);
    expect(lsf.setFlags).toHaveBeenCalledWith({ noTask: true });
    expect(confirm).not.toHaveBeenCalled();
  } finally {
    confirm.mockRestore();
  }
});

it("renders SDK operation feedback in the current locale while keeping the support link", () => {
  const runtime = createLocaleRuntime("en-US");
  const invoke = jest.fn();
  const wrapper = { datamanager: { localeRuntime: runtime, invoke } };

  LSFWrapper.prototype.showOperationToast.call(wrapper, 200, "annotationSaved", "annotationSaveFailed", {});
  expect(invoke).toHaveBeenLastCalledWith("toast", { message: "Annotation saved successfully", type: "info" });

  runtime.updateLocale("zh-CN");
  LSFWrapper.prototype.showOperationToast.call(wrapper, 200, "annotationSaved", "annotationSaveFailed", {});
  expect(invoke).toHaveBeenLastCalledWith("toast", { message: "标注已保存", type: "info" });

  LSFWrapper.prototype.showOperationToast.call(wrapper, 400, "annotationSaved", "annotationSaveFailed", {});
  const message = invoke.mock.lastCall[1].message;
  render(message);
  expect(screen.getByText(/标注未能保存/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "支持团队" })).toHaveAttribute("href", "https://support.humansignal.com/hc/en-us/requests/new");
  runtime.destroy();
});

it("releases loading after a notification preload has no task", async () => {
  const previousSettings = window.APP_SETTINGS;
  window.APP_SETTINGS = {
    ...previousSettings,
    feature_flags: { ...previousSettings?.feature_flags, feat_front_dev_1752_notification_links_in_label_and_review_streams: true },
  };
  const init = jest.spyOn(LSFWrapper.prototype, "initLabelStudio").mockImplementation(() => {});

  try {
    const dm = {
      store: {
        project: { id: 7 },
        taskStore: { selected: null, loadTaskHistory: jest.fn().mockResolvedValue([]) },
        users: [],
      },
      hasInterface: jest.fn().mockReturnValue(false),
      invoke: jest.fn(),
      taskSelectionPromise: null,
    };
    const wrapper = new LSFWrapper(dm, document.createElement("div"), {
      preload: { interaction: "notifications", task: 41 },
    });
    wrapper.preloadTask = jest.fn().mockResolvedValue(false);
    const ls = { setFlags: jest.fn(), setTaskHistory: jest.fn() };

    await wrapper.onLabelStudioLoad(ls);
    expect(wrapper.preloadTask).toHaveBeenCalledTimes(1);
    expect(ls.setFlags.mock.calls.map(([flags]) => flags.isLoading)).toEqual([true, false]);

    wrapper.preloadTask.mockRejectedValueOnce(new Error("request failed"));
    ls.setFlags.mockClear();
    await expect(wrapper.onLabelStudioLoad(ls)).rejects.toThrow("request failed");
    expect(ls.setFlags.mock.calls.map(([flags]) => flags.isLoading)).toEqual([true, false]);

    wrapper.preload = {};
    ls.setFlags.mockClear();
    await wrapper.onLabelStudioLoad(ls);
    expect(ls.setFlags.mock.calls.map(([flags]) => flags.isLoading)).toEqual([true]);
  } finally {
    init.mockRestore();
    window.APP_SETTINGS = previousSettings;
  }
});

it("does not read a removed Data Manager store after preload finishes", async () => {
  const previousSettings = window.APP_SETTINGS;
  window.APP_SETTINGS = {
    ...previousSettings,
    feature_flags: { ...previousSettings?.feature_flags, feat_front_dev_1752_notification_links_in_label_and_review_streams: true },
  };
  const init = jest.spyOn(LSFWrapper.prototype, "initLabelStudio").mockImplementation(() => {});

  try {
    const dm = {
      store: {
        project: { id: 7 },
        taskStore: { selected: null, loadTaskHistory: jest.fn().mockResolvedValue([]) },
        users: [],
      },
      hasInterface: jest.fn().mockReturnValue(false),
      invoke: jest.fn(),
      taskSelectionPromise: null,
    };
    const wrapper = new LSFWrapper(dm, document.createElement("div"), {
      preload: { interaction: "notifications", task: 41 },
    });
    let finishPreload;
    wrapper.preloadTask = jest.fn(() => new Promise((resolve) => { finishPreload = resolve; }));
    const ls = { setFlags: jest.fn(), setTaskHistory: jest.fn() };
    const loading = wrapper.onLabelStudioLoad(ls);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(wrapper.preloadTask).toHaveBeenCalledTimes(1);

    wrapper.destroyed = true;
    dm.store = null;
    finishPreload(false);
    await expect(loading).resolves.toBeUndefined();
    expect(ls.setFlags.mock.calls.map(([flags]) => flags.isLoading)).toEqual([true]);
  } finally {
    init.mockRestore();
    window.APP_SETTINGS = previousSettings;
  }
});

it("does not initialize labels in an editor destroyed during the request", async () => {
  let finishRequest;
  const userLabels = { init: jest.fn() };
  const wrapper = {
    destroyed: false,
    lsf: { userLabels },
    project: { id: 7 },
    datamanager: {
      apiCall: jest.fn(() => new Promise((resolve) => { finishRequest = resolve; })),
    },
  };
  const loading = LSFWrapper.prototype.loadUserLabels.call(wrapper);
  wrapper.destroyed = true;
  wrapper.lsf = null;
  finishRequest({ results: [{ from_name: "choice", label: { value: "A" } }] });
  await expect(loading).resolves.toBeUndefined();
  expect(userLabels.init).not.toHaveBeenCalled();
});

it("releases loading only while the same editor is still alive", async () => {
  const lsf = {};
  const wrapper = { destroyed: false, lsf, setLoading: jest.fn() };
  let finishRequest;
  const pending = LSFWrapper.prototype.withinLoadingState.call(wrapper,
    () => new Promise((resolve) => { finishRequest = resolve; }));
  expect(wrapper.setLoading).toHaveBeenCalledWith(true);
  wrapper.destroyed = true;
  wrapper.lsf = null;
  finishRequest("done");
  await expect(pending).resolves.toBe("done");
  expect(wrapper.setLoading.mock.calls).toEqual([[true]]);

  wrapper.destroyed = false;
  wrapper.lsf = lsf;
  wrapper.setLoading.mockClear();
  await expect(LSFWrapper.prototype.withinLoadingState.call(wrapper, () => Promise.reject(new Error("failed"))))
    .rejects.toThrow("failed");
  expect(wrapper.setLoading.mock.calls).toEqual([[true], [false]]);
});

it("does not select a task after its editor is destroyed during task loading", async () => {
  let finishRequest;
  const lsf = { setFlags: jest.fn() };
  const wrapper = {
    destroyed: false,
    lsf,
    labelStream: true,
    datamanager: {
      store: { taskStore: { loadNextTask: jest.fn(() => new Promise((resolve) => { finishRequest = resolve; })) } },
    },
    setLoading: jest.fn(),
    selectTask: jest.fn(),
  };
  wrapper.withinLoadingState = (callback) => LSFWrapper.prototype.withinLoadingState.call(wrapper, callback);
  const loading = LSFWrapper.prototype.loadTask.call(wrapper);
  wrapper.destroyed = true;
  wrapper.lsf = null;
  finishRequest({ id: 41 });
  await expect(loading).resolves.toBeUndefined();
  expect(lsf.setFlags).not.toHaveBeenCalled();
  expect(wrapper.selectTask).not.toHaveBeenCalled();
});
