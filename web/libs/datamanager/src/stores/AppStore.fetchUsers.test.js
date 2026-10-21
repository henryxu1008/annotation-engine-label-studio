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
