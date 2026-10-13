import { destroy, types, unprotect } from "mobx-state-tree";

jest.mock("../../components/Common/Pagination/Pagination", () => ({
  DEFAULT_PAGE_SIZE: 30,
  getStoredPageSize: () => 30,
}));
jest.mock("../../utils/feature-flags", () => ({ isFF: () => false }));

const { DataStore } = require("./DataStore");
const Item = types.model("FetchItem", { id: types.identifierNumber, source: "" });
const Records = DataStore("FetchRecords", { listItemType: Item, associatedItemType: Item, apiMethod: "tasks" });
const Root = types.model("FetchRoot", { records: Records, viewsStore: types.frozen() }).volatile(() => ({
  SDK: { type: "LS", invoke: jest.fn() },
  API: { getSettingsByMethodName: () => ({}) },
  apiCall: jest.fn().mockResolvedValue({ total: 1, tasks: [{ id: 3 }] }),
}));
const makeRoot = () => {
  const root = Root.create({ records: {}, viewsStore: { selected: { id: 7 } } });
  unprotect(root);
  return root;
};

afterEach(() => jest.useRealTimers());

it("does not start a debounced request after its workspace is destroyed", async () => {
  jest.useFakeTimers();
  const root = makeRoot();
  const api = root.apiCall;
  const pending = root.records.fetch({ id: 7 });
  destroy(root);
  await jest.advanceTimersByTimeAsync(150);
  await expect(pending).resolves.toBeUndefined();
  expect(api).not.toHaveBeenCalled();
});

it("ignores a collection response arriving after workspace destruction", async () => {
  const root = makeRoot();
  let finish;
  const notify = root.SDK.invoke;
  root.apiCall = jest.fn(() => new Promise((resolve) => { finish = resolve; }));
  const pending = root.records._performFetch({ id: 7 });
  destroy(root);
  finish({ total: 1, tasks: [{ id: 3 }] });
  await expect(pending).resolves.toBeUndefined();
  expect(notify).not.toHaveBeenCalled();
});

it("does not fetch from a detached workspace without its SDK", async () => {
  const root = makeRoot();
  const api = root.apiCall;
  root.SDK = undefined;
  try {
    await expect(root.records._performFetch({ id: 7 })).resolves.toBeUndefined();
    expect(api).not.toHaveBeenCalled();
  } finally { destroy(root); }
});

it("does not apply data after the workspace SDK is removed", async () => {
  const root = makeRoot();
  let finish;
  root.apiCall = jest.fn(() => new Promise((resolve) => { finish = resolve; }));
  const pending = root.records._performFetch({ id: 7 });
  root.SDK = undefined;
  finish({ total: 1, tasks: [{ id: 3 }] });
  try {
    await expect(pending).resolves.toBeUndefined();
    expect(root.records.list).toHaveLength(0);
    expect(root.records.total).toBe(0);
  } finally { destroy(root); }
});

it("loads and announces a collection while its workspace is alive", async () => {
  const root = makeRoot();
  try {
    await root.records._performFetch({ id: 7 });
    expect(root.records.list.map((item) => item.id)).toEqual([3]);
    expect(root.records.total).toBe(1);
    expect(root.SDK.invoke).toHaveBeenCalledWith("dataFetched", root.records);
  } finally { destroy(root); }
});
