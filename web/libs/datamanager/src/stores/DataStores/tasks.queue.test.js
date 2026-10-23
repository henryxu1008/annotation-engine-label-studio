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
