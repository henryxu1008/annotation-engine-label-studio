import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ReviewAuditDownload } from "./ReviewAuditDownload";
import { sha256 } from "@noble/hashes/sha256";
import { bytesToHex } from "@noble/hashes/utils";

const mockCall = jest.fn();
const mockDownload = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCall }) }));
jest.mock("../../utils/download", () => ({ downloadFile: (...args) => mockDownload(...args) }));
const id = "40000000-0000-4000-8000-000000000004";
const profile = "review-audit-redacted/v1";
const effect = "supervisory_only";
const preview = {
  profile,
  effect,
  batch_binding_hash: "a".repeat(64),
  capture_preview_hash: "b".repeat(64),
  candidate_count: 1,
  entity_count: 3,
  edge_count: 2,
  gaps: {},
  redacted: true,
};
const receipt = {
  package_id: id,
  profile,
  effect,
  state: "dispatch_pending",
  error_code: "",
  captured_at: "2026-11-04T00:00:00Z",
  counts: { candidate: 1 },
  gaps: {},
  coverage: "complete",
  evidence: "complete",
  retryable: true,
  artifacts: [],
};
const response = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () =>
    JSON.parse(
      JSON.stringify({
        schema: "review-audit-response/v1",
        profile,
        effect,
        viewer_hash: "c".repeat(64),
        owner_hash: "d".repeat(64),
        collection_supported: true,
        ...body,
      }),
    ),
});
let Provider;
beforeEach(async () => {
  Provider = createLocaleRuntime("en-US").provider;
  mockCall.mockReset();
  mockDownload.mockReset();
  mockCall.mockImplementation(async (method) => {
    if (method === "reviewAuditCapabilitiesRaw") return response({ profile, effect, limits: {} });
    if (method === "reviewAuditListRaw") return response({ results: [], next_cursor: null });
    if (method === "reviewAuditPreviewRaw") return response(preview);
    if (method === "reviewAuditCreateRaw") return response(receipt, 202);
    if (method === "reviewAuditDetailRaw") return response(receipt);
    throw Error(method);
  });
});

async function open() {
  fireEvent.click(await screen.findByTestId("review-audit-open"));
  await screen.findByTestId("review-audit-preview");
  await waitFor(() => expect(screen.getByTestId("review-audit-preview").disabled).toBe(false));
}

test("I01: preview and explicit fixed-redacted confirmation precede capture", async () => {
  render(
    <Provider>
      <ReviewAuditDownload projectId={4} batchId={8} />
    </Provider>,
  );
  await open();
  fireEvent.click(screen.getByTestId("review-audit-preview"));
  const confirm = await screen.findByTestId("review-audit-confirm");
  expect(screen.getByTestId("review-audit-create").disabled).toBe(true);
  fireEvent.click(confirm);
  fireEvent.click(screen.getByTestId("review-audit-create"));
  await screen.findByTestId("review-audit-status");
  expect(mockCall.mock.calls.find(([m]) => m === "reviewAuditCreateRaw")[1].body).toEqual({
    review_batch_id: 8,
    expected_batch_binding_hash: preview.batch_binding_hash,
    expected_capture_preview_hash: preview.capture_preview_hash,
    profile,
  });
});

test("I02: a late capability cannot enable a different batch", async () => {
  let settle;
  mockCall.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        settle = resolve;
      }),
  );
  const view = render(
    <Provider>
      <ReviewAuditDownload projectId={4} batchId={8} />
    </Provider>,
  );
  view.rerender(
    <Provider>
      <ReviewAuditDownload projectId={5} batchId={9} />
    </Provider>,
  );
  await act(async () => settle(response({ profile, effect, limits: {} })));
  await waitFor(() => expect(mockCall).toHaveBeenCalledTimes(2));
});

const mount = (props = {}) =>
  render(
    <Provider>
      <ReviewAuditDownload projectId={4} batchId={8} {...props} />
    </Provider>,
  );
const confirm = async () => {
  await open();
  fireEvent.click(screen.getByTestId("review-audit-preview"));
  fireEvent.click(await screen.findByTestId("review-audit-confirm"));
  fireEvent.click(screen.getByTestId("review-audit-create"));
};

test("I03: a server-certified precommit budget rejection does not become UNKNOWN", async () => {
  const fallback = mockCall.getMockImplementation();
  mockCall.mockImplementation(async (method, options) => {
    if (method === "reviewAuditCreateRaw") {
      const result = response({ display_context: { reason: "AUDIT_TOO_LARGE" } }, 422);
      result.headers = { get: (key) => (key === "X-Audit-Command-State" ? "rejected" : null) };
      return result;
    }
    return fallback(method, options);
  });
  mount();
  await confirm();
  await screen.findByRole("alert");
  expect(screen.queryByTestId("review-audit-recover")).toBeNull();
  expect(mockCall.mock.calls.filter(([method]) => method === "reviewAuditCreateRaw")).toHaveLength(1);
});

test("I03: lost ACK recovery keeps original key/body with a mutating transport", async () => {
  const fallback = mockCall.getMockImplementation();
  const sent = [];
  mockCall.mockImplementation(async (method, options) => {
    if (method === "reviewAuditCreateRaw") {
      sent.push({ key: options.headers["Idempotency-Key"], body: { ...options.body } });
      Object.assign(options.body, { transport_mutated: true });
      return response(receipt, sent.length === 1 ? 503 : 202);
    }
    return fallback(method, options);
  });
  mount();
  await confirm();
  fireEvent.click(await screen.findByTestId("review-audit-recover"));
  await screen.findByTestId("review-audit-status");
  expect(sent).toHaveLength(2);
  expect(sent[1]).toEqual(sent[0]);
  expect(mockCall.mock.calls.filter(([m]) => m === "reviewAuditPreviewRaw")).toHaveLength(1);
});

test("F02: a viewer salt change keeps the original UNKNOWN command until explicit recovery", async () => {
  const fallback = mockCall.getMockImplementation();
  const sent = [];
  let rotated = false;
  mockCall.mockImplementation(async (method, options) => {
    if (method === "reviewAuditCapabilitiesRaw" && rotated) return response({ viewer_hash: "e".repeat(64) });
    if (method === "reviewAuditCreateRaw") {
      sent.push({ key: options.headers["Idempotency-Key"], body: { ...options.body } });
      if (sent.length === 1) {
        rotated = true;
        return response({ display_context: { reason: "AUDIT_VIEWER_CHANGED" } }, 403);
      }
      return response({ ...receipt, viewer_hash: "e".repeat(64) }, 202);
    }
    if (rotated) {
      const result = await fallback(method, options);
      const body = await result.json();
      return response({ ...body, viewer_hash: "e".repeat(64) });
    }
    return fallback(method, options);
  });
  mount();
  await confirm();
  await screen.findByTestId("review-audit-recover");
  expect(sent).toHaveLength(1);
  fireEvent.click(screen.getByTestId("review-audit-recover"));
  await screen.findByTestId("review-audit-status");
  expect(sent).toHaveLength(2);
  expect(sent[1]).toEqual(sent[0]);
});

test("B08: a provider without a proven creator collection cannot enable the flow", async () => {
  mockCall.mockResolvedValue(response({ collection_supported: false }));
  mount();
  await waitFor(() => expect(mockCall).toHaveBeenCalledTimes(1));
  expect(screen.queryByTestId("review-audit-open")).toBeNull();
});

test("I03: a failed detail read never silently creates another package", async () => {
  const fallback = mockCall.getMockImplementation();
  let reads = 0;
  mockCall.mockImplementation(async (method, options) => {
    if (method === "reviewAuditDetailRaw" && ++reads === 1) throw Error("offline");
    return fallback(method, options);
  });
  mount();
  await confirm();
  await screen.findByRole("alert");
  fireEvent.click(screen.getByTestId("review-audit-refresh"));
  await screen.findByTestId("review-audit-status");
  expect(mockCall.mock.calls.filter(([m]) => m === "reviewAuditCreateRaw")).toHaveLength(1);
});

test("I03: retry lost ACK recovery also retains its independent command key", async () => {
  const fallback = mockCall.getMockImplementation();
  const sent = [];
  mockCall.mockImplementation(async (method, options) => {
    if (method === "reviewAuditRetryRaw") {
      sent.push({ key: options.headers["Idempotency-Key"], body: { ...options.body }, params: { ...options.params } });
      return response(
        { key: options.headers["Idempotency-Key"], package_id: id, state: "dispatch_pending" },
        sent.length === 1 ? 503 : 200,
      );
    }
    return fallback(method, options);
  });
  mount();
  await confirm();
  fireEvent.click(await screen.findByTestId("review-audit-retry"));
  fireEvent.click(await screen.findByTestId("review-audit-recover"));
  await screen.findByTestId("review-audit-status");
  expect(sent).toHaveLength(2);
  expect(sent[1]).toEqual(sent[0]);
});

test("I04: explicit action moves focus before disable and async completion does not steal it", async () => {
  let settle;
  const fallback = mockCall.getMockImplementation();
  mockCall.mockImplementation((method, options) =>
    method === "reviewAuditPreviewRaw"
      ? new Promise((resolve) => {
          settle = resolve;
        })
      : fallback(method, options),
  );
  render(
    <Provider>
      <button type="button" data-testid="other-control">
        Other action
      </button>
      <ReviewAuditDownload projectId={4} batchId={8} />
    </Provider>,
  );
  await open();
  const button = screen.getByTestId("review-audit-preview");
  button.focus();
  fireEvent.click(button);
  expect(document.activeElement.tagName).toBe("H4");
  screen.getByTestId("other-control").focus();
  await act(async () => settle(response(preview)));
  await screen.findByTestId("review-audit-confirm");
  expect(document.activeElement).toBe(screen.getByTestId("other-control"));
});

test("I02: late JSON parse failure from an old owner cannot create UNKNOWN in new scope", async () => {
  let reject;
  const fallback = mockCall.getMockImplementation();
  mockCall.mockImplementation(async (method, options) =>
    method === "reviewAuditCreateRaw"
      ? {
          ok: true,
          status: 202,
          json: () =>
            new Promise((_, fail) => {
              reject = fail;
            }),
        }
      : fallback(method, options),
  );
  const view = mount();
  await confirm();
  await waitFor(() => expect(reject).toBeDefined());
  view.rerender(
    <Provider>
      <ReviewAuditDownload projectId={4} batchId={8} ownerKey="different-owner" />
    </Provider>,
  );
  await act(async () => reject(Error("late JSON failure")));
  await screen.findByTestId("review-audit-open");
  expect(screen.queryByTestId("review-audit-recover")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("I05: capability denial leaves no audit entry or raw export fallback", async () => {
  mockCall.mockResolvedValue(response({}, 403));
  mount();
  await waitFor(() => expect(mockCall).toHaveBeenCalled());
  expect(screen.queryByTestId("review-audit-open")).toBeNull();
  expect(mockDownload).not.toHaveBeenCalled();
});

test("I06: validated ZIP uses a fixed filename and the existing download helper", async () => {
  const bytes = new Uint8Array([1, 2, 3, 4]);
  const artifact = {
    artifact_id: "50000000-0000-4000-8000-000000000005",
    sha256: bytesToHex(sha256(bytes)),
    size_bytes: bytes.length,
  };
  const ready = { ...receipt, state: "ready", retryable: false, artifacts: [artifact] };
  const fallback = mockCall.getMockImplementation();
  const blob = { size: bytes.length, arrayBuffer: async () => bytes.buffer };
  mockCall.mockImplementation(async (method, options) => {
    if (method === "reviewAuditDetailRaw") return response(ready);
    if (method === "reviewAuditDownloadRaw")
      return {
        ok: true,
        status: 200,
        headers: {
          get: (key) => (key === "X-Audit-Viewer" ? "c".repeat(64) : key === "Content-Type" ? "application/zip" : null),
        },
        blob: async () => blob,
      };
    return fallback(method, options);
  });
  mount();
  await confirm();
  fireEvent.click(await screen.findByTestId("review-audit-download"));
  await waitFor(() => expect(mockDownload).toHaveBeenCalledWith(blob, `review-audit-${id}.zip`));
});

test("I06: mismatched ZIP hash never saves a file", async () => {
  const artifact = { artifact_id: "50000000-0000-4000-8000-000000000005", sha256: "0".repeat(64), size_bytes: 4 };
  const fallback = mockCall.getMockImplementation();
  mockCall.mockImplementation(async (method, options) => {
    if (method === "reviewAuditDetailRaw")
      return response({ ...receipt, state: "ready", retryable: false, artifacts: [artifact] });
    if (method === "reviewAuditDownloadRaw")
      return {
        ok: true,
        headers: { get: (key) => (key === "X-Audit-Viewer" ? "c".repeat(64) : "application/zip") },
        blob: async () => ({ size: 4, arrayBuffer: async () => new Uint8Array([9, 9, 9, 9]).buffer }),
      };
    return fallback(method, options);
  });
  mount();
  await confirm();
  fireEvent.click(await screen.findByTestId("review-audit-download"));
  await screen.findByRole("alert");
  expect(mockDownload).not.toHaveBeenCalled();
});

test("I02: late blob from a prior project is never saved", async () => {
  let settle;
  const bytes = new Uint8Array([1, 2, 3, 4]);
  const artifact = {
    artifact_id: "50000000-0000-4000-8000-000000000005",
    sha256: bytesToHex(sha256(bytes)),
    size_bytes: 4,
  };
  const fallback = mockCall.getMockImplementation();
  mockCall.mockImplementation(async (method, options) => {
    if (method === "reviewAuditDetailRaw")
      return response({ ...receipt, state: "ready", retryable: false, artifacts: [artifact] });
    if (method === "reviewAuditDownloadRaw")
      return {
        ok: true,
        headers: { get: (key) => (key === "X-Audit-Viewer" ? "c".repeat(64) : "application/zip") },
        blob: () =>
          new Promise((resolve) => {
            settle = resolve;
          }),
      };
    return fallback(method, options);
  });
  const view = mount();
  await confirm();
  fireEvent.click(await screen.findByTestId("review-audit-download"));
  await waitFor(() => expect(settle).toBeDefined());
  view.rerender(
    <Provider>
      <ReviewAuditDownload projectId={5} batchId={9} />
    </Provider>,
  );
  await act(async () => settle({ size: 4, arrayBuffer: async () => bytes.buffer }));
  expect(mockDownload).not.toHaveBeenCalled();
});

test("I01: Chinese flow uses the fixed-redaction locale catalog", async () => {
  Provider = createLocaleRuntime("zh-CN").provider;
  mount();
  await open();
  expect(screen.getByTestId("review-audit-open").textContent).toBe("审核审计包");
  expect(screen.getByText(/固定脱敏：不含任务原文/)).toBeTruthy();
});
