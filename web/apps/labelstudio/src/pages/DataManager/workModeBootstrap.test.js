import { loadWorkspaceBootstrap } from "./workModeBootstrap";

const validMode = (restricted) => ({ project_id: 7, session_user_id: 11, restricted });
const options = (callApi, isCancelled = () => false) => ({ callApi, projectId: 7, sessionUserId: 11, isCancelled });

it("selects restricted work before any model or legacy workspace request", async () => {
  const callApi = jest.fn().mockResolvedValue(validMode(true));
  await expect(loadWorkspaceBootstrap(options(callApi))).resolves.toEqual({ restricted: true, interactiveBackend: null });
  expect(callApi).toHaveBeenCalledTimes(1);
  expect(callApi.mock.calls[0][0]).toBe("projectWorkMode");
});

it("retains model discovery for ordinary work", async () => {
  const backend = { id: 4, is_interactive: true };
  const callApi = jest.fn().mockResolvedValueOnce(validMode(false)).mockResolvedValueOnce([backend]);
  await expect(loadWorkspaceBootstrap(options(callApi))).resolves.toEqual({ restricted: false, interactiveBackend: backend });
  expect(callApi.mock.calls.map(([name]) => name)).toEqual(["projectWorkMode", "mlBackends"]);
});

it.each([
  null, {}, { ...validMode(false), project_id: 8 }, { ...validMode(false), session_user_id: 12 },
  { ...validMode(false), restricted: "false" }, { ...validMode(false), $meta: { ok: false } },
])("fails closed on an unavailable, stale or malformed mode response", async (response) => {
  const callApi = jest.fn().mockResolvedValue(response);
  await expect(loadWorkspaceBootstrap(options(callApi))).rejects.toThrow("Work mode is unavailable");
  expect(callApi).toHaveBeenCalledTimes(1);
});

it("does not initialize a stale session after an asynchronous response", async () => {
  let complete;
  let cancelled = false;
  const callApi = jest.fn().mockReturnValue(new Promise((resolve) => { complete = resolve; }));
  const pending = loadWorkspaceBootstrap(options(callApi, () => cancelled));
  cancelled = true;
  complete(validMode(false));
  await expect(pending).resolves.toBeNull();
  expect(callApi).toHaveBeenCalledTimes(1);
});

it("does not start a request after the workspace has been disposed", async () => {
  const callApi = jest.fn();
  await expect(loadWorkspaceBootstrap(options(callApi, () => true))).resolves.toBeNull();
  expect(callApi).not.toHaveBeenCalled();
});
