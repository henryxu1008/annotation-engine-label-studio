import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { createLocaleRuntime } from "@humansignal/i18n";
import { LSFWrapper } from "./lsf-sdk";
import { Modal } from "../components/Common/Modal/Modal";

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
