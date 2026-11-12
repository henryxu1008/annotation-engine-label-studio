import { APIProxy } from "./index";
import { blindEndpointAllowed } from "../../sdk/blind-protocol";

const task = 2 ** 51 + 42;

it("blocks direct and raw legacy calls before fetch in blind mode", async () => {
  const originalFetch = global.fetch;
  global.fetch = jest.fn().mockResolvedValue({
    ok: true, status: 200, headers: new Headers(), url: "http://localhost/api/tasks/blind/",
    text: async () => "{}",
  });
  const api = new APIProxy({
    gateway: "http://localhost/api/dm",
    endpoints: { columns: "/columns", task: "/../tasks/:taskID", blindTask: "/../tasks/blind/:taskID/" },
    operationGuard: blindEndpointAllowed,
  });
  try {
    expect((await api.columns()).status).toBe(403);
    expect((await api.call("task", { params: { taskID: 42 } })).status).toBe(403);
    expect((await api.taskRaw({ taskID: task })).status).toBe(403);
    expect((await api.blindTask({ taskID: 42 })).status).toBe(403);
    expect(global.fetch).not.toHaveBeenCalled();
    await api.blindTask({ taskID: task });
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(global.fetch.mock.calls[0][0]).toContain(`/tasks/blind/${task}`);
    api.sharedParams = { taskID: 42 };
    expect((await api.blindTask({ taskID: task })).status).toBe(403);
    expect(global.fetch).toHaveBeenCalledTimes(1);
    for (const definition of [
      { path: "/../tasks/:taskID/", method: "get" },
      { path: "/../tasks/blind/:taskID/", method: "post" },
      { path: "/../tasks/blind/:taskID/", method: "get", gateway: "https://example.invalid" },
      { path: "/../tasks/blind/:taskID/", method: "get", params: { task_package: 7 } },
    ]) {
      const altered = new APIProxy({ gateway: "http://localhost/api/dm", endpoints: { blindTask: definition },
        operationGuard: blindEndpointAllowed });
      expect((await altered.blindTask({ taskID: task })).status).toBe(403);
    }
    expect(global.fetch).toHaveBeenCalledTimes(1);
  } finally {
    global.fetch = originalFetch;
  }
});

it("allows only the exact scoped bootstrap endpoint and parameter", async () => {
  const originalFetch = global.fetch;
  global.fetch = jest.fn().mockResolvedValue({
    ok: true, status: 200, headers: new Headers(), url: "http://localhost/api/dm/blind-columns",
    text: async () => '{"columns":[]}',
  });
  const api = new APIProxy({
    gateway: "http://localhost/api/dm",
    endpoints: { blindColumns: { path: "/blind-columns/", method: "get" } },
    operationGuard: blindEndpointAllowed,
  });
  try {
    expect((await api.blindColumns({ project: 7, taskID: 3 })).status).toBe(403);
    expect((await api.blindColumns({ project: 0 })).status).toBe(403);
    expect(global.fetch).not.toHaveBeenCalled();
    await api.blindColumns({ project: 7 });
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(global.fetch.mock.calls[0][0]).toContain("/api/dm/blind-columns?project=7");
  } finally {
    global.fetch = originalFetch;
  }
});

it("limits personal work requests to the pinned project and page parameters", async () => {
  const originalFetch = global.fetch;
  global.fetch = jest.fn().mockResolvedValue({
    ok: true, status: 200, headers: new Headers(), url: "http://localhost/api/projects/7/blind-work",
    text: async () => '{"results":[]}',
  });
  const api = new APIProxy({
    gateway: "http://localhost/api/dm",
    endpoints: { blindWork: { path: "/../projects/:projectID/blind-work/", method: "get" } },
    operationGuard: blindEndpointAllowed,
  });
  try {
    expect((await api.blindWork({ projectID: 7, page: 1, view: 12 })).status).toBe(403);
    expect((await api.blindWork({ projectID: 7, page: 1001 })).status).toBe(403);
    await api.blindWork({ projectID: 7, page: 2 });
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(global.fetch.mock.calls[0][0]).toContain("/api/projects/7/blind-work?page=2");
  } finally {
    global.fetch = originalFetch;
  }
});
