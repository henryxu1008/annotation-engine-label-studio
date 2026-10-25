import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ClassificationArbitrationEntry, ClassificationArbitrations } from "./ClassificationArbitrations";

const mockCall = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCall }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
const origin = { batch_id: 3, task_id: 8, expected_manifest_hash: "a".repeat(64) };
const metadata = { id: 7, schema: "classification-arbitrations/v1", decision_rule: "adopt-exact-submission/v1",
  batch_id: 3, task_id: 8, batch_name: "Synthetic batch", assignee_id: 30, input_set_hash: "b".repeat(64),
  created_at: "created", calculated_at: "calculated", viewer_hash: "c".repeat(64), status: "pending", version: 1, can_decide: true, can_cancel: false };
const report = { task_id: 8, status: "comparable", outcome: "disagreement", reason: null, agreement: 0, matching_pairs: 0, total_pairs: 1,
  inputs: [11, 12].map((id) => ({ submission_id: id, revision: 1, submitter_id: id + 20, result_hash: `source-${id}`, config_hash: "config" })),
  locations: [{ from_name: "sentiment", to_name: "text", matching_pairs: 0, total_pairs: 1, agreement: 0, disagrees: true,
    values: [{ submission_id: 11, choices: ["Positive"] }, { submission_id: 12, choices: ["Negative"] }] }] };
let manager, state;
const capabilities = () => ({ enabled: true, viewer_hash: metadata.viewer_hash, can_read: true, can_create: manager, can_decide: !manager, can_cancel: manager });
const detail = () => ({ ...metadata, status: state, version: state === "pending" ? 1 : 2, can_decide: !manager && state === "pending",
  can_cancel: manager && state === "pending", available: true, reason: null, texts: [{ name: "text", text: "Frozen 合成 question" }], report,
  observed_at: "now", decision: state === "resolved" ? { id: 9, actor_id: 30, created_at: "saved", winner_submission_id: 11,
    winner_revision: 1, reason: "Human reason", rule: metadata.decision_rule } : null,
  cancellation: state === "cancelled" ? { actor_id: 1, created_at: "saved", reason: "Human reason" } : null });
const respond = async (method, options) => {
  switch (method) {
    case "classificationArbitrationCapability": return capabilities();
    case "classificationArbitrations": return { viewer_hash: metadata.viewer_hash, count: 1, next: null, previous: null, results: [detail()] };
    case "classificationArbitrationAssignees": return { ...origin, origin_manifest_hash: origin.expected_manifest_hash,
      input_set_hash: metadata.input_set_hash, viewer_hash: metadata.viewer_hash, batch_name: "Synthetic batch", count: 1, next: null, previous: null, results: [{ id: 30, name: "Independent reviewer" }] };
    case "classificationArbitrationDetail": return detail();
    case "createClassificationArbitration": return { ...detail(), origin_manifest_hash: origin.expected_manifest_hash, replayed: false,
      receipt: { operation: "create", key: options.headers["Idempotency-Key"], status_at_commit: "pending", version_at_commit: 1 } };
    case "resolveClassificationArbitration": state = "resolved"; return { id: 7, status: state, version: 2, replayed: false,
      decision_id: 9, winner_submission_id: 11, receipt: { operation: "decision", key: options.headers["Idempotency-Key"], status_at_commit: state, version_at_commit: 2 } };
    case "cancelClassificationArbitration": state = "cancelled"; return { id: 7, status: state, version: 2, replayed: false,
      receipt: { operation: "cancel", key: options.headers["Idempotency-Key"], status_at_commit: state, version_at_commit: 2 } };
    default: throw new Error(method);
  }
};
const mount = (locale = "en-US", initialOrigin = null) => {
  const runtime = createLocaleRuntime(locale), Provider = runtime.provider;
  return { ...render(<Provider><ClassificationArbitrations projectId={4} initialOrigin={initialOrigin} /></Provider>), runtime, Provider };
};
beforeEach(() => { manager = false; state = "pending"; mockCall.mockReset().mockImplementation(respond); });
const open = async () => fireEvent.click(await screen.findByRole("button", { name: /#8/ }));
const command = async (action, zh = false) => {
  if (action === "create") {
    fireEvent.change(await screen.findByLabelText(zh ? "独立审核员" : "Independent reviewer"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: zh ? "确认派发" : "Confirm dispatch" }));
  } else {
    await open();
    if (action === "decision") fireEvent.click((await screen.findAllByRole("radio"))[0]);
    fireEvent.change(await screen.findByLabelText(zh ? "裁决或取消依据" : "Decision or cancellation reason"), { target: { value: "Human reason" } });
    fireEvent.click(screen.getByRole("button", { name: action === "decision" ? (zh ? "采纳此冻结修订" : "Adopt this frozen revision") : (zh ? "确认取消" : "Confirm cancellation") }));
  }
};

it.each(["en-US", "zh-CN"])("dispatches and adopts a frozen revision in %s", async (locale) => {
  const zh = locale === "zh-CN";
  manager = true;
  let mounted = mount(locale, origin);
  await command("create", zh);
  await screen.findByTestId("arbitration-saved");
  expect(mockCall.mock.calls.find(([m]) => m === "createClassificationArbitration")[1].body).toEqual({ ...origin, assignee_id: 30 });
  expect(screen.queryAllByRole("radio")).toHaveLength(0);
  mounted.unmount(); mounted.runtime.destroy(); manager = false;
  mounted = mount(locale);
  await command("decision", zh);
  await screen.findByTestId("arbitration-decision");
  const saved = mockCall.mock.calls.find(([m]) => m === "resolveClassificationArbitration")[1].body;
  expect(saved).toEqual({ expected_version: 1, expected_input_set_hash: metadata.input_set_hash, winner_submission_id: 11, reason: "Human reason" });
  expect(screen.queryAllByRole("radio")).toHaveLength(0);
  mounted.unmount(); mounted.runtime.destroy();
});

it.each(["create", "decision", "cancel"])("preserves the original %s command across unknown acknowledgement and read refresh", async (action) => {
  manager = action !== "decision";
  let attempts = 0;
  const method = { create: "createClassificationArbitration", decision: "resolveClassificationArbitration", cancel: "cancelClassificationArbitration" }[action];
  mockCall.mockImplementation(async (m, options) => {
    if (m === method && ++attempts === 1) throw new Error("lost acknowledgement");
    const r = await respond(m, options);
    return m === method ? { ...r, replayed: true } : r;
  });
  const { runtime, unmount } = mount("en-US", action === "create" ? origin : null);
  await command(action);
  await screen.findByRole("button", { name: "Recover original command" });
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Recover original command" }).disabled).toBe(false));
  expect(mockCall.mock.calls.filter(([m]) => m === method)).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Recover original command" }));
  await screen.findByTestId("arbitration-saved");
  const calls = mockCall.mock.calls.filter(([m]) => m === method);
  expect(calls).toHaveLength(2); expect(calls[1][1].headers).toEqual(calls[0][1].headers); expect(calls[1][1].body).toEqual(calls[0][1].body);
  unmount(); runtime.destroy();
});

it("retains confirmed cancellation when the following read fails", async () => {
  manager = true;
  mockCall.mockImplementation((m, o) => m === "classificationArbitrationDetail" && state === "cancelled" ? Promise.reject(new Error("read failed")) : respond(m, o));
  const { runtime, unmount } = mount();
  await command("cancel");
  await screen.findByRole("alert");
  expect(screen.getByTestId("arbitration-saved").textContent).toContain("Cancellation");
  expect(screen.queryByRole("button", { name: "Recover original command" })).toBeNull();
  expect(mockCall.mock.calls.filter(([m]) => m === "cancelClassificationArbitration")).toHaveLength(1);
  unmount(); runtime.destroy();
});

it("clears late question data on blur", async () => {
  let resolve;
  mockCall.mockImplementation((m, o) => m === "classificationArbitrationDetail" ? new Promise((done) => { resolve = done; }) : respond(m, o));
  const { runtime, unmount } = mount();
  await open(); fireEvent.blur(window);
  await act(async () => resolve(detail()));
  expect(screen.queryByTestId("arbitration-text")).toBeNull(); expect(screen.queryAllByRole("radio")).toHaveLength(0);
  unmount(); runtime.destroy();
});

it("rejects unverified sources and does not display cached answers", async () => {
  mockCall.mockImplementation((m, o) => m === "classificationArbitrationDetail" ? Promise.resolve({ ...detail(), available: false, texts: [], report: null, reason: "source_unavailable" }) : respond(m, o));
  const { runtime, unmount } = mount(); await open(); await screen.findByTestId("arbitration-unavailable");
  expect(screen.queryByText("Frozen 合成 question")).toBeNull(); expect(screen.queryAllByRole("radio")).toHaveLength(0);
  unmount(); runtime.destroy();
});

it("discards an old project's late command acknowledgement", async () => {
  let resolve;
  manager = true;
  mockCall.mockImplementation((m, o) => m === "createClassificationArbitration" ? new Promise((done) => { resolve = async () => done(await respond(m, o)); }) : respond(m, o));
  const { runtime, unmount, rerender, Provider } = mount("en-US", origin); await command("create");
  rerender(<Provider><ClassificationArbitrations projectId={5} /></Provider>);
  await act(async () => resolve());
  expect(screen.queryByTestId("arbitration-saved")).toBeNull();
  unmount(); runtime.destroy();
});

it("treats a mismatched acknowledgement as unknown", async () => {
  manager = true;
  mockCall.mockImplementation(async (m, o) => {
    const r = await respond(m, o);
    return m === "createClassificationArbitration" ? { ...r, task_id: 99 } : r;
  });
  const { runtime, unmount } = mount("en-US", origin); await command("create");
  await screen.findByRole("button", { name: "Recover original command" });
  expect(screen.queryByTestId("arbitration-saved")).toBeNull();
  unmount(); runtime.destroy();
});

const casRejection = (status = 409) => ({ error: true, $meta: { ok: false, status },
  response: { display_context: { reason: "ARBITRATION_CAS_CONFLICT" } } });

it("discards a late queue when current capability denies reading", async () => {
  let resolveQueue;
  mockCall.mockImplementation((method, options) => {
    if (method === "classificationArbitrationCapability") return { ...capabilities(), can_read: false };
    if (method === "classificationArbitrations") return new Promise((done) => { resolveQueue = async () => done(await respond(method, options)); });
    return respond(method, options);
  });
  const { runtime, unmount } = mount();
  await waitFor(() => expect(resolveQueue).toBeDefined());
  await act(async () => resolveQueue());
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: /#8/ })).toBeNull();
  expect(screen.queryByRole("combobox")).toBeNull();
  unmount(); runtime.destroy();
});

it("discards a late frozen answer after capability revokes reading", async () => {
  let readable = true, resolveDetail;
  mockCall.mockImplementation((method, options) => {
    if (method === "classificationArbitrationCapability") return { ...capabilities(), can_read: readable };
    if (method === "classificationArbitrationDetail") return new Promise((done) => { resolveDetail = () => done(detail()); });
    return respond(method, options);
  });
  const { runtime, unmount } = mount();
  await screen.findByRole("button", { name: /#8/ }); readable = false;
  await open();
  await act(async () => resolveDetail());
  await screen.findByRole("alert");
  expect(screen.queryByTestId("arbitration-text")).toBeNull();
  expect(screen.queryAllByRole("radio")).toHaveLength(0);
  unmount(); runtime.destroy();
});

it.each(["decision", "cancel"])("hides stale %s controls when current capability denies the operation", async (action) => {
  manager = action === "cancel";
  mockCall.mockImplementation((method, options) => method === "classificationArbitrationCapability"
    ? { ...capabilities(), [action === "decision" ? "can_decide" : "can_cancel"]: false } : respond(method, options));
  const { runtime, unmount } = mount(); await open();
  await screen.findByTestId("arbitration-text");
  expect(screen.queryByRole("button", { name: action === "decision" ? "Adopt this frozen revision" : "Confirm cancellation" })).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
  unmount(); runtime.destroy();
});

it.each(["list", "detail", "roster"])("rejects %s data from a different viewer context", async (view) => {
  manager = view === "roster";
  let viewer = view === "detail" ? metadata.viewer_hash : "d".repeat(64);
  mockCall.mockImplementation((method, options) => method === "classificationArbitrationCapability"
    ? { ...capabilities(), viewer_hash: viewer } : respond(method, options));
  const { runtime, unmount } = mount("en-US", view === "roster" ? origin : null);
  if (view === "detail") { await screen.findByRole("button", { name: /#8/ }); viewer = "d".repeat(64); await open(); }
  await screen.findByRole("alert");
  expect(screen.queryByTestId("arbitration-text")).toBeNull();
  expect(screen.queryByRole("combobox")).toBeNull();
  expect(screen.queryByRole("button", { name: /#8/ })).toBeNull();
  unmount(); runtime.destroy();
});

it("keeps an unknown cancellation recoverable when read capability is revoked", async () => {
  manager = true;
  let readable = true, attempts = 0;
  mockCall.mockImplementation((method, options) => {
    if (method === "classificationArbitrationCapability") return { ...capabilities(), can_read: readable, can_create: readable };
    if (method === "cancelClassificationArbitration" && ++attempts === 1) throw new Error("lost acknowledgement");
    return respond(method, options);
  });
  const { runtime, unmount } = mount(); await command("cancel");
  await screen.findByRole("button", { name: "Recover original command" }); readable = false;
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Recover original command" }).disabled).toBe(false));
  expect(screen.queryByTestId("arbitration-text")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Recover original command" }));
  await screen.findByTestId("arbitration-saved");
  const calls = mockCall.mock.calls.filter(([m]) => m === "cancelClassificationArbitration");
  expect(calls).toHaveLength(2);
  expect(calls[1][1].headers).toEqual(calls[0][1].headers); expect(calls[1][1].body).toEqual(calls[0][1].body);
  unmount(); runtime.destroy();
});

it("allows a fresh command after a recognized first CAS rejection and a fresh read", async () => {
  let attempts = 0;
  mockCall.mockImplementation((m, o) => m === "resolveClassificationArbitration" && ++attempts === 1 ? casRejection() : respond(m, o));
  const { runtime, unmount } = mount(); await command("decision");
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Recover original command" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  fireEvent.click((await screen.findAllByRole("radio"))[0]);
  fireEvent.change(screen.getByLabelText("Decision or cancellation reason"), { target: { value: "Fresh decision" } });
  fireEvent.click(screen.getByRole("button", { name: "Adopt this frozen revision" }));
  await screen.findByTestId("arbitration-saved");
  const calls = mockCall.mock.calls.filter(([m]) => m === "resolveClassificationArbitration");
  expect(calls).toHaveLength(2); expect(calls[1][1].headers).not.toEqual(calls[0][1].headers);
  unmount(); runtime.destroy();
});

it.each([409, 503])("retains the original unknown command after a later rejection with HTTP %s", async (status) => {
  let attempts = 0;
  mockCall.mockImplementation((m, o) => {
    if (m !== "resolveClassificationArbitration") return respond(m, o);
    if (++attempts === 1) throw new Error("lost acknowledgement");
    return attempts === 2 ? casRejection(status) : respond(m, o);
  });
  const { runtime, unmount } = mount(); await command("decision");
  fireEvent.click(await screen.findByRole("button", { name: "Recover original command" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Recover original command" }).disabled).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "Refresh read-only view" }));
  await screen.findAllByRole("radio");
  expect(screen.getByRole("button", { name: "Adopt this frozen revision" }).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Recover original command" }));
  await screen.findByTestId("arbitration-saved");
  const calls = mockCall.mock.calls.filter(([m]) => m === "resolveClassificationArbitration");
  expect(calls).toHaveLength(3);
  for (const [, options] of calls.slice(1)) {
    expect(options.headers).toEqual(calls[0][1].headers); expect(options.body).toEqual(calls[0][1].body);
  }
  unmount(); runtime.destroy();
});
