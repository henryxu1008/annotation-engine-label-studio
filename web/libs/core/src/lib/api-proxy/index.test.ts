import { APIProxy } from "./index";

it("sends PUT updates with a JSON content type and the complete body", async () => {
  const fetchMock = jest.fn().mockResolvedValue({
    status: 200,
    ok: true,
    url: "http://localhost/api/example",
    headers: new Headers(),
    text: async () => "{}",
  });
  const originalFetch = global.fetch;
  global.fetch = fetchMock;
  try {
    const api = new APIProxy({ gateway: "http://localhost/api", endpoints: { update: "PUT:/example" } });
    await api.methods.update({}, { body: { candidate_ids: [8, 9], due_at: null, priority: 3 } });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const options = fetchMock.mock.calls[0][1] as RequestInit;
    expect((options.headers as Headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(options.body as string)).toEqual({ candidate_ids: [8, 9], due_at: null, priority: 3 });
  } finally {
    global.fetch = originalFetch;
  }
});

it("lets the browser set multipart headers for PUT FormData", async () => {
  const fetchMock = jest.fn().mockResolvedValue({
    status: 200,
    ok: true,
    url: "http://localhost/api/example",
    headers: new Headers(),
    text: async () => "{}",
  });
  const originalFetch = global.fetch;
  global.fetch = fetchMock;
  try {
    const api = new APIProxy({ gateway: "http://localhost/api", endpoints: { update: "PUT:/example" } });
    const form = new FormData();
    form.append("name", "example");
    await api.methods.update({}, { body: form });
    const options = fetchMock.mock.calls[0][1] as RequestInit;
    expect((options.headers as Headers).has("Content-Type")).toBe(false);
    expect(options.body).toBe(form);
  } finally {
    global.fetch = originalFetch;
  }
});
