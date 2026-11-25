import Form from "./Form";

jest.mock("@humansignal/ui", () => ({ Button: () => null }), { virtual: true });
jest.mock("./Elements", () => ({ Counter: () => null, Input: () => null, Select: () => null, Toggle: () => null }));

it("does not report a rejected proxy request as a successful form submission", async () => {
  const onError = jest.fn();
  const onSubmit = jest.fn();
  const form = {
    api: { call: jest.fn().mockResolvedValue({ error: "unavailable", status: 403 }) },
    props: { onError, onSubmit }, setState: jest.fn(),
  };
  expect(await Form.prototype.submitWithAPI.call(form, "task", {})).toBe(false);
  expect(onError).toHaveBeenCalledTimes(1);
  expect(onSubmit).not.toHaveBeenCalled();
});

it("blocks raw fetch before sending in a restricted session", async () => {
  const fetchBefore = global.fetch;
  global.fetch = jest.fn();
  const onError = jest.fn();
  const form = { api: { operationGuard: () => false, sharedParams: {} }, props: { onError } };
  try {
    expect(await Form.prototype.submitWithFetch.call(form, new FormData())).toBe(false);
    expect(global.fetch).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(1);
  } finally {
    global.fetch = fetchBefore;
  }
});

it("keeps successful API form submissions working", async () => {
  const onSubmit = jest.fn();
  const form = { api: { call: jest.fn().mockResolvedValue({ id: 3 }) }, props: { onSubmit }, setState: jest.fn() };
  expect(await Form.prototype.submitWithAPI.call(form, "action", {})).toBe(true);
  expect(onSubmit).toHaveBeenCalledWith({ id: 3 });
});

it("keeps ordinary raw form submissions working", async () => {
  const fetchBefore = global.fetch;
  global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ id: 4 }) });
  const onSubmit = jest.fn();
  const form = {
    api: { operationGuard: () => true, sharedParams: {} }, props: { onSubmit },
    formElement: { current: { action: "/ordinary" } }, setState: jest.fn(),
  };
  try {
    expect(await Form.prototype.submitWithFetch.call(form, new FormData())).toBe(true);
    expect(onSubmit).toHaveBeenCalledWith({ id: 4 });
    expect(global.fetch).toHaveBeenCalledTimes(1);
  } finally {
    global.fetch = fetchBefore;
  }
});
