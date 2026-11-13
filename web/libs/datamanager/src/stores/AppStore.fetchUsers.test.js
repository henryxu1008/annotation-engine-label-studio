import { destroy } from "mobx-state-tree";

jest.mock("./Tabs", () => {
  const { flow, types } = jest.requireActual("mobx-state-tree");
  return {
    TabStore: types.model("TestTabStore", { views: types.array(types.frozen()) }).actions(() => ({
      fetchColumns() {},
      fetchTabs: flow(function* () {}),
    })),
  };
});

window.APP_SETTINGS = { hostname: "http://localhost" };
const { AppStore } = require("./AppStore");

const makeStore = (usersResponse, users = [], project = {}) => {
  const store = AppStore.create({ toolbar: "", users, project });
  const invoke = jest.fn();
  store._sdk = {
    api: {
      users: jest.fn().mockResolvedValue(usersResponse),
      project: jest.fn().mockResolvedValue(usersResponse),
    },
    invoke,
  };
  return { store, invoke };
};

it("does not spread a 401 error response into users during logout", async () => {
  const error = { status: 401, error: "Unauthorized", response: { detail: "Authentication required" } };
  const existingUser = {
    id: 7, firstName: "Existing", lastName: "User", username: "existing",
    email: "existing@example.com", lastActivity: "", avatar: null, initials: "EU",
  };
  const { store, invoke } = makeStore(error, [existingUser]);

  try {
    await expect(store.fetchUsers()).resolves.toBeUndefined();
    expect(store.users).toHaveLength(1);
    expect(store.users[0].email).toBe(existingUser.email);
    expect(invoke).toHaveBeenCalledWith("error", error);
  } finally {
    destroy(store);
  }
});

it("rejects a malformed successful users response", async () => {
  const { store } = makeStore({ users: [] });

  try {
    await expect(store.fetchUsers()).rejects.toThrow("Users API must return an array");
    expect(store.users).toHaveLength(0);
  } finally {
    destroy(store);
  }
});

it("reports an API error when optional request params are omitted", async () => {
  const error = { status: 401, error: "Unauthorized", response: { detail: "Authentication required" } };
  const { store, invoke } = makeStore(error);

  try {
    await expect(store.apiCall("users")).resolves.toBe(error);
    expect(invoke).toHaveBeenCalledWith("error", error);
  } finally {
    destroy(store);
  }
});

it("keeps project data when its in-flight request returns 401", async () => {
  const error = { status: 401, error: "Unauthorized" };
  const project = { id: 7, title: "Existing project" };
  const { store } = makeStore(error, [], project);

  try {
    await expect(store.fetchProject({ force: true })).resolves.toBe(false);
    expect(store.project).toEqual(project);
    expect(store.projectFetch).toBe(false);
  } finally {
    destroy(store);
  }
});

it("ends initial loading after a reported project error", async () => {
  const error = { status: 401, error: "Unauthorized" };
  const { store, invoke } = makeStore(error);

  try {
    await expect(store.fetchData()).resolves.toBeUndefined();
    expect(store.loading).toBe(false);
    expect(store.project).toEqual({});
    expect(invoke).toHaveBeenCalledWith("error", error);
  } finally {
    destroy(store);
  }
});

it("bootstraps a restricted project without loading the legacy roster or project route", async () => {
  const { store } = makeStore([]);
  store._sdk.settings = { blindProtocol: true };
  store._sdk.projectId = 7;
  store._sdk.api.sharedParams = {};
  store._sdk.api.blindProject = jest.fn().mockResolvedValue({ id: 7, title: "Scoped" });
  store._sdk.api.blindWork = jest.fn().mockResolvedValue({ results: [], page: 1, total: 0, has_next: false });
  try {
    await store.fetchData();
    expect(store.loading).toBe(false);
    expect(store.project.id).toBe(7);
    expect(store._sdk.api.blindProject).toHaveBeenCalledWith({ project: 7 }, expect.anything());
    expect(store._sdk.api.blindWork).toHaveBeenCalledWith({ projectID: 7, page: 1 }, expect.anything());
    expect(store.blindWorkPage.results).toEqual([]);
    expect(store._sdk.api.project).not.toHaveBeenCalled();
    expect(store._sdk.api.users).not.toHaveBeenCalled();
  } finally {
    destroy(store);
  }
});

it("clears revoked work and ignores stale or cross-project list responses", async () => {
  const { store } = makeStore([]);
  const task = 2 ** 51 + 1;
  const page = (text) => ({
    results: [{ id: task, task_id: task, project: 7, assignment_id: task + 1, assignment_version: 1,
      can_label: true, data: { text } }], page: 1, total: 1, has_next: false,
  });
  store._sdk.settings = { blindProtocol: true };
  store._sdk.projectId = 7;
  store._sdk.api.sharedParams = {};
  let finishOld;
  store._sdk.api.blindWork = jest.fn()
    .mockResolvedValueOnce(page("first"))
    .mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }))
    .mockResolvedValueOnce(page("newer"))
    .mockResolvedValueOnce({ error: "revoked", status: 403 });
  try {
    expect(await store.loadBlindWork()).toBe(true);
    const oldRequest = store.loadBlindWork();
    expect(store.blindWorkPage).toBeNull();
    expect(await store.loadBlindWork()).toBe(true);
    finishOld(page("stale"));
    expect(await oldRequest).toBe(false);
    expect(store.blindWorkPage.results[0].data.text).toBe("newer");
    expect(await store.loadBlindWork()).toBe(false);
    expect(store.blindWorkPage).toBeNull();
    expect(store.blindWorkError).toBe(true);
    let finishCrossProject;
    store._sdk.api.blindWork.mockImplementationOnce(() => new Promise((resolve) => { finishCrossProject = resolve; }));
    const pending = store.loadBlindWork();
    store._sdk.projectId = 8;
    finishCrossProject(page("wrong project"));
    expect(await pending).toBe(false);
    expect(store.blindWorkPage).toBeNull();
  } finally {
    destroy(store);
  }
});

it("clears the restricted list and ends loading when the request fails", async () => {
  const { store } = makeStore([]);
  store._sdk.settings = { blindProtocol: true };
  store._sdk.projectId = 7;
  store._sdk.api.sharedParams = {};
  store._sdk.api.blindProject = jest.fn().mockResolvedValue({ id: 7 });
  store._sdk.api.blindWork = jest.fn().mockRejectedValue(new Error("network failure"));
  try {
    await expect(store.fetchData()).resolves.toBeUndefined();
    expect(store.loading).toBe(false);
    expect(store.blindWorkPage).toBeNull();
    expect(store.blindWorkError).toBe(true);
  } finally {
    destroy(store);
  }
});

it("does not apply restricted project bootstrap after switching projects", async () => {
  const { store } = makeStore([], [], { id: 7, title: "Old" });
  let finish;
  store._sdk.settings = { blindProtocol: true };
  store._sdk.projectId = 7;
  store._sdk.api.sharedParams = {};
  store._sdk.api.blindProject = jest.fn(() => new Promise((resolve) => { finish = resolve; }));
  store._sdk.api.blindWork = jest.fn();
  try {
    const pending = store.fetchData();
    expect(store.project).toEqual({});
    store._sdk.projectId = 8;
    finish({ id: 7, title: "Stale" });
    await pending;
    expect(store.project).toEqual({});
    expect(store._sdk.api.blindWork).not.toHaveBeenCalled();
    expect(store.loading).toBe(false);
  } finally {
    destroy(store);
  }
});

it.each(["fetchUsers", "fetchProject", "fetchData"])("ignores a %s response after its workspace is destroyed", async (method) => {
  const { store, invoke } = makeStore([]);
  let finish;
  const response = new Promise((resolve) => { finish = resolve; });
  store._sdk.api.users.mockReturnValue(response);
  store._sdk.api.project.mockReturnValue(response);
  const pending = store[method]({ force: true });
  destroy(store);
  finish(method === "fetchProject" ? { id: 7, title: "Obsolete project" } : []);
  await expect(pending).resolves.toBe(method === "fetchProject" ? false : undefined);
  expect(invoke).not.toHaveBeenCalled();
});
