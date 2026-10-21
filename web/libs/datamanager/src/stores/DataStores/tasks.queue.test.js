import { destroy, types } from "mobx-state-tree";

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
}));

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
