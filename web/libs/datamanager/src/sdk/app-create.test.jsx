import ReactDOM from "react-dom";
import { createApp } from "./app-create";
import { registerModel } from "../stores/DynamicModel";

jest.mock("../components/App/App", () => ({ App: () => null }));
jest.mock("../stores/AppStore", () => ({ AppStore: {} }));
jest.mock("../stores/DataStores", () => ({}));
jest.mock("../stores/DynamicModel", () => ({ DynamicModel: {}, registerModel: jest.fn() }));
jest.mock("react-dom", () => ({ ...jest.requireActual("react-dom"), render: jest.fn() }));

beforeEach(() => jest.clearAllMocks());

it("bootstraps restricted columns only through the bound project route", async () => {
  const response = { error: "unavailable", status: 403 };
  const manager = {
    settings: { blindProtocol: true }, projectId: 7,
    api: { columns: jest.fn(), blindColumns: jest.fn().mockResolvedValue(response) }, invoke: jest.fn(),
  };
  await expect(createApp(document.createElement("div"), manager)).resolves.toBeNull();
  expect(manager.api.blindColumns).toHaveBeenCalledWith({ project: 7 });
  expect(manager.api.columns).not.toHaveBeenCalled();
  expect(ReactDOM.render).not.toHaveBeenCalled();
});

it.each(["project", "mode"])("ignores restricted columns after changing %s", async (change) => {
  let finish;
  const manager = {
    settings: { blindProtocol: true }, projectId: 7,
    api: { blindColumns: jest.fn(() => new Promise((resolve) => { finish = resolve; })) }, invoke: jest.fn(),
  };
  const pending = createApp(document.createElement("div"), manager);
  if (change === "project") manager.projectId = 8;
  else manager.settings.blindProtocol = false;
  finish({ columns: [] });
  await expect(pending).resolves.toBeNull();
  expect(registerModel).not.toHaveBeenCalled();
  expect(ReactDOM.render).not.toHaveBeenCalled();
});

it("does not apply legacy columns after restricted mode is enabled", async () => {
  let finish;
  const manager = {
    settings: { blindProtocol: false }, projectId: 7,
    api: { columns: jest.fn(() => new Promise((resolve) => { finish = resolve; })) }, invoke: jest.fn(),
  };
  const pending = createApp(document.createElement("div"), manager);
  manager.settings.blindProtocol = true;
  finish({ columns: [] });
  await expect(pending).resolves.toBeNull();
  expect(registerModel).not.toHaveBeenCalled();
  expect(ReactDOM.render).not.toHaveBeenCalled();
});

it.each([400, 401, 403, 500])("reports a %s bootstrap failure without publishing a store or rejecting initialization", async (status) => {
  const response = { error: "Reported API failure", response: { detail: "synthetic failure" } };
  Object.defineProperty(response, "$meta", { value: { status }, enumerable: false });
  const manager = { mode: "explorer", api: { columns: jest.fn().mockResolvedValue(response) }, invoke: jest.fn() };
  const root = document.createElement("div");

  await expect(createApp(root, manager)).resolves.toBeNull();
  expect(manager.invoke).toHaveBeenCalledWith("error", response);
  expect(manager.invoke).toHaveBeenCalledWith("crash", expect.objectContaining({ phase: "initialization" }));
  expect(registerModel).not.toHaveBeenCalled();
  expect(ReactDOM.render).not.toHaveBeenCalled();
  expect(root.childElementCount).toBe(0);
});

it("does not report an obsolete instance's bootstrap failure to the active workspace", async () => {
  const manager = { api: { columns: jest.fn().mockResolvedValue({ error: "Unauthorized" }) }, invoke: jest.fn() };
  await expect(createApp(document.createElement("div"), manager, () => false)).resolves.toBeNull();
  expect(manager.invoke).not.toHaveBeenCalled();
  expect(registerModel).not.toHaveBeenCalled();
});

it.each([{}, { columns: {} }])("rejects malformed success data rather than initializing an empty workspace", async (response) => {
  const manager = { api: { columns: jest.fn().mockResolvedValue(response) }, invoke: jest.fn() };
  await expect(createApp(document.createElement("div"), manager)).rejects.toThrow("Invalid Data Manager columns response");
  expect(manager.invoke).not.toHaveBeenCalled();
  expect(registerModel).not.toHaveBeenCalled();
  expect(ReactDOM.render).not.toHaveBeenCalled();
});
