import { destroy, types } from "mobx-state-tree";
import { History } from "../../utils/history";

jest.mock("../../components/Common/Pagination/Pagination", () => ({
  DEFAULT_PAGE_SIZE: 30,
  getStoredPageSize: () => 30,
}));
jest.mock("../../utils/feature-flags", () => ({ isFF: () => false }));

const { create } = require("./tasks");
const Root = types.model("QueueResponseRoot", {
  taskStore: create([]),
  viewsStore: types.frozen(),
}).volatile(() => ({
  SDK: { type: "LS", invoke: jest.fn() },
  API: { getSettingsByMethodName: () => ({}) },
  invokeAction: jest.fn(),
  apiCall: jest.fn(),
}));

const RestrictedRoot = types.model("RestrictedTaskRoot", {
  taskStore: create([{ id: "data", type: "List", target: "tasks" }]),
  viewsStore: types.frozen(),
}).volatile(() => ({
  SDK: { type: "LS", projectId: 7, settings: { blindProtocol: true }, invoke: jest.fn() },
  API: { getSettingsByMethodName: () => ({}) },
  invokeAction: jest.fn(),
  apiCall: jest.fn(),
}));
const taskHandle = 2 ** 51 + 31;
const restrictedTask = (id = taskHandle) => ({
  id, project: 7, assignment_id: id + 2, assignment_version: 1, can_label: true,
  data: { text: "synthetic" }, annotations: [], drafts: [], predictions: [], default_selected_annotation: null,
});

it("loads a bound restricted task into the editor store", async () => {
  const root = RestrictedRoot.create({ taskStore: {}, viewsStore: {} });
  root.apiCall.mockResolvedValue(restrictedTask());
  try {
    const task = await root.taskStore.loadTask(taskHandle);
    expect(task.id).toBe(taskHandle);
    expect(root.taskStore.selected.id).toBe(taskHandle);
    expect(root.taskStore.selected.data).toEqual({ text: "synthetic" });
    expect(root.apiCall).toHaveBeenCalledTimes(1);
  } finally { destroy(root); }
});

it("ignores obsolete, wrong-project and malformed restricted editor responses", async () => {
  const root = RestrictedRoot.create({ taskStore: {}, viewsStore: {} });
  let finishOld;
  root.apiCall.mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }))
    .mockResolvedValueOnce(restrictedTask(taskHandle + 10))
    .mockResolvedValueOnce({ ...restrictedTask(), assignment_id: 12 })
    .mockRejectedValueOnce(new Error("network"));
  try {
    const pending = root.taskStore.loadTask(taskHandle);
    expect((await root.taskStore.loadTask(taskHandle + 10)).id).toBe(taskHandle + 10);
    finishOld(restrictedTask());
    expect(await pending).toBeNull();
    expect(root.taskStore.selected.id).toBe(taskHandle + 10);
    expect(await root.taskStore.loadTask(taskHandle)).toBeNull();
    expect(await root.taskStore.loadTask(taskHandle)).toBeNull();
    let finishProject;
    root.apiCall.mockImplementationOnce(() => new Promise((resolve) => { finishProject = resolve; }));
    const switching = root.taskStore.loadTask(taskHandle);
    root.SDK.projectId = 8;
    finishProject(restrictedTask());
    expect(await switching).toBeNull();
    expect(root.taskStore.list.some((task) => task.id === taskHandle)).toBe(false);
  } finally { destroy(root); }
});

it("loads history with the same package context as the native next-task queue", async () => {
  const root = Root.create({ taskStore: {}, viewsStore: { selected: { id: 7 } } });
  const params = jest.spyOn(History, "getParams").mockReturnValue({ task_package: "4" });
  root.apiCall.mockResolvedValue([{ taskId: 9, annotationId: null }]);
  try {
    await expect(root.taskStore.loadTaskHistory({ projectId: 3 })).resolves.toEqual([{ taskId: 9, annotationId: undefined }]);
    expect(root.apiCall).toHaveBeenCalledWith("taskHistory", { projectId: 3, task_package: "4" });
  } finally { params.mockRestore(); destroy(root); }
});

it.each([
  [404, undefined, false],
  [404, "PAUSED", false],
  [403, undefined, false],
  [500, "QUEUE_EXHAUSTED", false],
  [404, "QUEUE_EXHAUSTED", null],
])("distinguishes queue availability from HTTP %s with reason %s", async (status, reason, expected) => {
  const root = Root.create({ taskStore: {}, viewsStore: { selected: { id: 7 } } });
  root.invokeAction.mockResolvedValue({
    $meta: { status }, error: "Request error", response: { display_context: { reason } },
  });
  try {
    await expect(root.taskStore.loadNextTask()).resolves.toBe(expected);
    if (expected === null) expect(root.SDK.invoke).toHaveBeenCalledWith("labelStreamFinished");
    else expect(root.SDK.invoke).not.toHaveBeenCalled();
  } finally { destroy(root); }
});
