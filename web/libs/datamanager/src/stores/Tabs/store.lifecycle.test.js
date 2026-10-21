import { destroy, types, unprotect } from "mobx-state-tree";

jest.mock("../../utils/history", () => ({ History: { navigate: jest.fn() } }));
jest.mock("./tab", () => {
  const { flow, types } = jest.requireActual("mobx-state-tree");
  return { Tab: types.model("LifecycleTab", { id: types.identifierNumber, tabKey: "test" })
    .volatile(() => ({ pendingReload: Promise.resolve(), selected: { _invokeChangeEvent: jest.fn() } }))
    .actions((self) => ({ reload: flow(function* () { yield self.pendingReload; }), save: flow(function* () {}) })) };
});

window.APP_SETTINGS = { hostname: "http://localhost" };
const { TabStore } = require("./store");
const Root = types.model("TabLifecycleRoot", { viewsStore: TabStore }).volatile(() => ({
  SDK: { invoke: jest.fn() }, dataStore: { clear: jest.fn() }, apiCall: jest.fn().mockResolvedValue([]),
}));
const makeRoot = () => {
  const root = Root.create({ viewsStore: { views: [{ id: 7 }, { id: 8 }] } });
  unprotect(root);
  return root;
};

it("does not announce a tab reload completed after workspace destruction", async () => {
  const root = makeRoot();
  const view = root.viewsStore.views[1];
  const notify = root.SDK.invoke;
  const changed = view.selected._invokeChangeEvent;
  let finish;
  view.pendingReload = new Promise((resolve) => { finish = resolve; });
  const pending = root.viewsStore.setSelected(8);
  destroy(root);
  finish();
  await expect(pending).resolves.toBeUndefined();
  expect(notify).not.toHaveBeenCalled();
  expect(changed).not.toHaveBeenCalled();
});

it("does not announce a tab after its workspace SDK is removed", async () => {
  const root = makeRoot();
  const view = root.viewsStore.views[1];
  const changed = view.selected._invokeChangeEvent;
  let finish;
  view.pendingReload = new Promise((resolve) => { finish = resolve; });
  const pending = root.viewsStore.setSelected(8);
  root.SDK = undefined;
  finish();
  try {
    await expect(pending).resolves.toBeUndefined();
    expect(changed).not.toHaveBeenCalled();
  } finally { destroy(root); }
});

it("selects and announces a live tab after its collection reload", async () => {
  const root = makeRoot();
  const view = root.viewsStore.views[1];
  try {
    await root.viewsStore.setSelected(8);
    expect(root.viewsStore.selected).toBe(view);
    expect(root.SDK.invoke).toHaveBeenCalledWith("tabChanged", view);
    expect(view.selected._invokeChangeEvent).toHaveBeenCalledTimes(1);
  } finally { destroy(root); }
});

it("stops tab initialization when selection finishes after workspace destruction", async () => {
  const root = makeRoot();
  let finish;
  root.viewsStore.views[0].pendingReload = new Promise((resolve) => { finish = resolve; });
  const pending = root.viewsStore.fetchTabs();
  await Promise.resolve();
  await Promise.resolve();
  destroy(root);
  finish();
  await expect(pending).resolves.toBeUndefined();
});
