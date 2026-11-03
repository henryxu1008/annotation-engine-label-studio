import { TextEncoder } from "node:util";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ConcernFollowupWorkspace } from "./ConcernFollowupWorkspace";
import { fieldDigest } from "./fieldEvidence";
import {
  followupContext,
  contextMatches,
  followupAckMatches,
  receiptOwnerHash,
  presentationDigest,
} from "./concernFollowupResponses";
import presentationVectors from "./followupPresentationVectors.json";

global.TextEncoder = TextEncoder;
it.each(presentationVectors)("matches Python's frozen display vector $name", ({ input, sha256 }) => {
  expect(presentationDigest(input)).toBe(sha256);
});
const mockCall = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCall }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return {
    Button: React.forwardRef(({ children, waiting, look, size, variant, ...props }, ref) => (
      <button ref={ref} {...props}>
        {children}
      </button>
    )),
    Typography: ({ children }) => <span>{children}</span>,
  };
});

const hash = "a".repeat(64);
const owner = "b".repeat(64);

beforeEach(() => {
  mockCall.mockReset();
  mockCall.mockResolvedValue({
    effect: "supervisory_only",
    owner_hash: owner,
    viewer_hash: hash,
    count: 0,
    results: [],
    next: null,
    can_record: true,
    source_integrity: "not_checked",
  });
});

it("keeps focus on the stable heading across explicit refresh and completion", async () => {
  const runtime = createLocaleRuntime("en-US");
  const Provider = runtime.provider;
  render(
    <Provider>
      <ConcernFollowupWorkspace projectId={4} />
    </Provider>,
  );
  await waitFor(() => expect(mockCall).toHaveBeenCalled());
  const refresh = await screen.findByTestId("followup-refresh");
  let finish;
  mockCall.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  refresh.focus();
  fireEvent.click(refresh);
  const heading = screen.getByRole("heading");
  expect(document.activeElement).toBe(heading);
  await act(async () =>
    finish({
      effect: "supervisory_only",
      owner_hash: owner,
      viewer_hash: hash,
      count: 0,
      results: [],
      next: null,
      can_record: true,
      source_integrity: "not_checked",
    }),
  );
  expect(document.activeElement).toBe(heading);
});

const ids = {
  task: "10000000-0000-4000-8000-000000000001",
  opinion: "10000000-0000-4000-8000-000000000002",
  instance: "10000000-0000-4000-8000-000000000003",
  followup: "10000000-0000-4000-8000-000000000004",
  secondKey: "10000000-0000-4000-8000-000000000005",
};
const timestamp = "2026-11-03T00:00:00Z";
const copy = (v) => JSON.parse(JSON.stringify(v));
const mount = (locale = "en-US", props = {}) => {
  const Provider = createLocaleRuntime(locale).provider;
  return render(
    <Provider>
      <ConcernFollowupWorkspace projectId={4} {...props} />
    </Provider>,
  );
};
const fixture = () => {
  const state = {
    viewer: hash,
    owner: receiptOwnerHash({
      owner: { principal: "test:1", source: "test", actor_id: 1 },
      project_id: 4,
      organization_id: 1,
      instance_id: ids.instance,
    }),
    recorded: null,
    mode: "success",
    calls: [],
    mutateContext: null,
    mutateAck: null,
  };
  const snap = {
    project: { id: 4, label_config: "<View/>" },
    task: { id: 10, data: { text: "Original frozen 静态文本" } },
    annotation: { result: [] },
  };
  const pins = {
    submission_id: 9,
    assignment_id: 7,
    task_id: 10,
    revision: 1,
    assignee_id: 2,
    submitter_id: 2,
    result_hash: fieldDigest(snap),
    task_data_hash: fieldDigest(snap.task.data),
    label_config_sha256: hash,
  };
  const first = {
    schema: "first-review-proof/v1",
    id: 11,
    submission_id: 9,
    reviewer_id: 3,
    decision: "approved",
    reason: "Initial reason",
    created_at: timestamp,
  };
  const opinion = {
    schema: "second-review-command-receipt/v1",
    command: "decision",
    task_id: ids.task,
    project_id: 4,
    organization_id: 1,
    instance_id: ids.instance,
    owner: { principal: "test:4", source: "test", actor_id: 4 },
    key: ids.secondKey,
    request_hash: hash,
    created_at: timestamp,
    effect: "supervisory_only",
    source_hash: hash,
    version: 2,
    first_review_hash: fieldDigest(first),
    decision_id: ids.opinion,
    outcome: "concern",
    reason: "Independent concern",
  };
  const base = () => ({ effect: "supervisory_only", viewer_hash: state.viewer, owner_hash: state.owner });
  const targetSnapshot = { ...snap, annotation: { result: ["New frozen result"] } };
  const target = {
    subject: { ...pins, submission_id: 12, revision: 2, result_hash: fieldDigest(targetSnapshot) },
    status_at_record: "pending",
    first_review: null,
    first_review_hash: null,
  };
  const context = (targetId) => {
    const selected = targetId ? target : null;
    return {
      ...base(),
      second_review_task_id: ids.task,
      opinion_id: ids.opinion,
      concern_hash: hash,
      context_hash: fieldDigest({
        schema: "second-review-concern-followup/v1",
        rule: "independent-disposition/v1",
        effect: "supervisory_only",
        project_id: 4,
        organization_id: 1,
        instance_id: ids.instance,
        concern_hash: hash,
        target: selected,
      }),
      expected_version: 0,
      original: {
        subject: pins,
        first_review: first,
        result_snapshot: snap,
        historical_revision: true,
        presentation_hash: fieldDigest(snap),
      },
      opinion,
      target: selected
        ? { ...selected, result_snapshot: targetSnapshot, presentation_hash: fieldDigest(targetSnapshot) }
        : null,
      candidates: [{ id: 12, revision: 2, status: "pending" }],
      source_health: "target_source_verified",
      metric_integrity: "not_checked",
    };
  };
  const list = () => ({
    ...base(),
    count: 1,
    next: null,
    can_record: true,
    source_integrity: "not_checked",
    results: [
      {
        task_id: ids.task,
        opinion_id: ids.opinion,
        submission_id: 9,
        revision: 1,
        batch_id: 8,
        opinion_created_at: timestamp,
        opinion,
        followup_id: state.recorded ? ids.followup : null,
        recorded: state.recorded,
      },
    ],
  });
  mockCall.mockImplementation(async (method, options) => {
    if (method === "concernFollowups") return list();
    if (method === "concernFollowupContext") {
      const value = context(options.params.related_submission_id);
      return state.mutateContext ? state.mutateContext(copy(value)) : value;
    }
    if (method === "concernFollowupDetail")
      return {
        ...base(),
        id: ids.followup,
        receipt: state.recorded,
        source_integrity: "not_checked",
        metric_integrity: "not_checked",
        original: { subject: pins, first_review: first, opinion },
        target: state.recorded.related_submission_id ? target : null,
      };
    if (method === "concernFollowupCreate") {
      state.calls.push(copy(options));
      const b = options.body;
      const receipt = state.recorded ?? {
        schema: "second-review-followup-receipt/v1",
        record_schema: "second-review-concern-followup/v1",
        rule: "independent-disposition/v1",
        command: "record",
        followup_id: ids.followup,
        project_id: 4,
        organization_id: 1,
        instance_id: ids.instance,
        owner: { principal: "test:1", source: "test", actor_id: 1 },
        key: options.headers["Idempotency-Key"],
        request_hash: fieldDigest(b),
        created_at: timestamp,
        effect: "supervisory_only",
        version: 1,
        second_review_task_id: ids.task,
        opinion_id: ids.opinion,
        concern_hash: b.expected_concern_hash,
        origin_ref_hash: b.expected_concern_hash,
        context_hash: b.expected_context_hash,
        disposition: b.disposition,
        reason: b.reason,
        related_submission_id: b.related_submission_id,
        target_ref_hash: fieldDigest(b.related_submission_id ? target : null),
      };
      const replayed = !!state.recorded;
      state.recorded = receipt;
      if (state.mode === "lost") {
        state.mode = "success";
        return { $meta: { ok: false, status: 503 } };
      }
      const ack = { ...base(), receipt, replayed, source_integrity: "not_checked" };
      return state.mutateAck ? state.mutateAck(copy(ack)) : ack;
    }
    throw new Error("unexpected method");
  });
  return { state, context, list, target };
};
const open = async () => {
  const button = await screen.findByText(/Submission #9|提交 #9/);
  fireEvent.click(button);
  await screen.findByTestId("followup-reason");
};
const record = async () => {
  await act(async () => {
    fireEvent.change(screen.getByTestId("followup-reason"), { target: { value: "Independent disposition 处置" } });
    fireEvent.click(screen.getByTestId("followup-confirm"));
  });
  await act(async () => {
    fireEvent.click(screen.getByTestId("followup-record"));
  });
};

it.each(["en-US", "zh-CN"])("records an explicit no-change disposition in %s", async (locale) => {
  const { state } = fixture();
  mount(locale);
  await open();
  expect(screen.getAllByTestId("review-result-snapshot")).toHaveLength(1);
  expect(screen.getByTestId("followup-target").value).toBe("");
  await record();
  await waitFor(() => expect(state.calls).toHaveLength(1));
  await waitFor(() => expect(screen.queryByTestId("followup-record")).toBeNull());
  expect(state.calls[0].body.disposition).toBe("no_change");
  expect(state.calls[0].body.related_submission_id).toBeNull();
  expect(screen.queryByTestId("review-result-snapshot")).toBeNull();
});

it("only previews a successor after explicit selection and binds that exact target", async () => {
  const { state } = fixture();
  mount();
  await open();
  fireEvent.change(screen.getByTestId("followup-target"), { target: { value: "12" } });
  await waitFor(() => expect(screen.getAllByTestId("review-result-snapshot")).toHaveLength(2));
  await record();
  await waitFor(() => expect(state.calls).toHaveLength(1));
  expect(state.calls[0].body.related_submission_id).toBe(12);
  expect(state.calls[0].body.disposition).toBe("revision_linked");
});

it("recovers UNKNOWN with the exact original body/key after viewer rotation", async () => {
  const { state } = fixture();
  state.mode = "lost";
  mount();
  await open();
  await record();
  const recover = await screen.findByTestId("followup-recover");
  expect(document.activeElement).toBe(recover);
  state.viewer = "f".repeat(64);
  fireEvent.click(recover);
  await waitFor(() => expect(state.calls).toHaveLength(2));
  expect(state.calls[1].body).toEqual(state.calls[0].body);
  expect(state.calls[1].headers).toEqual(state.calls[0].headers);
  await waitFor(() => expect(screen.queryByTestId("followup-recover")).toBeNull());
});

it.each(["snapshot", "project", "first", "viewer", "context"])(
  "discards the whole invalid %s context",
  async (kind) => {
    const { state } = fixture();
    state.mutateContext = (r) => {
      if (kind === "snapshot") r.original.result_snapshot.task.data.text = "wrong";
      if (kind === "project") r.opinion.project_id = 99;
      if (kind === "first") r.original.first_review.submission_id = 100;
      if (kind === "viewer") r.viewer_hash = "f".repeat(64);
      if (kind === "context") r.context_hash = "f".repeat(64);
      return r;
    };
    mount();
    fireEvent.click(await screen.findByText(/Submission #9/));
    await screen.findByRole("alert");
    expect(screen.queryByTestId("review-result-snapshot")).toBeNull();
    expect(screen.queryByTestId("followup-record")).toBeNull();
    expect(state.calls).toHaveLength(0);
  },
);

it.each(["task", "key", "hash", "owner", "receiptOwner", "disposition"])(
  "retains UNKNOWN for a mismatched %s acknowledgement",
  async (kind) => {
    const { state } = fixture();
    state.mutateAck = (r) => {
      if (kind === "task") r.receipt.second_review_task_id = ids.instance;
      if (kind === "key") r.receipt.key = ids.secondKey;
      if (kind === "hash") r.receipt.request_hash = "f".repeat(64);
      if (kind === "owner") r.owner_hash = "f".repeat(64);
      if (kind === "receiptOwner") r.receipt.owner.actor_id = 999;
      if (kind === "disposition") r.receipt.disposition = "revision_linked";
      return r;
    };
    mount();
    await open();
    await record();
    expect(await screen.findByTestId("followup-recover")).toBeTruthy();
    expect(state.calls).toHaveLength(1);
    expect(screen.queryByTestId("review-result-snapshot")).toBeNull();
  },
);

it("clears the old command and source when the principal changes before recovery", async () => {
  const { state } = fixture();
  state.mode = "lost";
  mount();
  await open();
  await record();
  const recover = await screen.findByTestId("followup-recover");
  state.owner = "f".repeat(64);
  fireEvent.click(recover);
  await waitFor(() => expect(screen.queryByTestId("followup-recover")).toBeNull());
  expect(state.calls).toHaveLength(1);
  expect(screen.queryByTestId("review-result-snapshot")).toBeNull();
});

it("masks answers on blur and ignores its late context", async () => {
  fixture();
  mount();
  const ordinary = mockCall.getMockImplementation();
  let finish;
  mockCall.mockImplementation((method, options) =>
    method === "concernFollowupContext"
      ? new Promise((resolve) => {
          finish = () => ordinary(method, options).then(resolve);
        })
      : ordinary(method, options),
  );
  fireEvent.click(await screen.findByText(/Submission #9/));
  await waitFor(() => expect(finish).toBeDefined());
  fireEvent(window, new Event("blur"));
  await act(async () => {
    await finish();
  });
  expect(screen.queryByTestId("review-result-snapshot")).toBeNull();
});

it("does not steal focus after the user moves during a refresh", async () => {
  const { list } = fixture();
  mount();
  const refresh = await screen.findByTestId("followup-refresh");
  await screen.findByText(/Submission #9/);
  let finish;
  mockCall.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  fireEvent.click(refresh);
  const outside = document.createElement("button");
  document.body.appendChild(outside);
  outside.focus();
  await act(async () => finish(list()));
  expect(document.activeElement).toBe(outside);
  outside.remove();
});

it.each([
  ["en-US", "approved"],
  ["en-US", "rejected"],
  ["zh-CN", "approved"],
  ["zh-CN", "rejected"],
])("shows the exact reviewed successor state in %s / %s", async (locale, status) => {
  const { target } = fixture();
  target.status_at_record = status;
  target.first_review = {
    schema: "first-review-proof/v1",
    id: 13,
    submission_id: 12,
    reviewer_id: 3,
    decision: status,
    reason: "New initial review",
    created_at: timestamp,
  };
  target.first_review_hash = fieldDigest(target.first_review);
  mount(locale);
  await open();
  fireEvent.change(screen.getByTestId("followup-target"), { target: { value: "12" } });
  await waitFor(() => expect(screen.getAllByTestId("review-result-snapshot")).toHaveLength(2));
  await record();
  await waitFor(() => expect(screen.queryByTestId("followup-record")).toBeNull());
  expect(
    screen.getByText(new RegExp(locale === "zh-CN" ? "后继修订记录时状态" : "Successor status at record")),
  ).toBeTruthy();
});

it("accepts 2,000 Unicode code points through input, acknowledgement and history", async () => {
  const { state } = fixture();
  mount();
  await open();
  const reason = "😀".repeat(2000);
  await act(async () => {
    fireEvent.change(screen.getByTestId("followup-reason"), { target: { value: reason } });
    fireEvent.click(screen.getByTestId("followup-confirm"));
  });
  await act(async () => fireEvent.click(screen.getByTestId("followup-record")));
  await waitFor(() => expect(state.calls).toHaveLength(1));
  await waitFor(() => expect(screen.queryByTestId("followup-recover")).toBeNull());
  expect(screen.getByText(reason)).toBeTruthy();
});

it("does not truncate an over-limit reason or send an invalid command", async () => {
  const { state } = fixture();
  mount();
  await open();
  fireEvent.change(screen.getByTestId("followup-reason"), { target: { value: "a".repeat(2001) } });
  fireEvent.click(screen.getByTestId("followup-confirm"));
  expect(screen.getByTestId("followup-record").disabled).toBe(true);
  expect(screen.getByTestId("followup-reason").value).toHaveLength(2001);
  expect(state.calls).toHaveLength(0);
});

it("uses a writable transport copy and hides its saved notice after read authority fails", async () => {
  fixture();
  mount();
  await open();
  await record();
  await waitFor(() => expect(screen.queryByTestId("followup-record")).toBeNull());
  const posted = mockCall.mock.calls.find(([method]) => method === "concernFollowupCreate")[1];
  expect(Object.isFrozen(posted.body)).toBe(false);
  const ordinary = mockCall.getMockImplementation();
  mockCall.mockImplementation((method, options) =>
    method === "concernFollowups" ? Promise.resolve({ $meta: { ok: false, status: 403 } }) : ordinary(method, options),
  );
  fireEvent.click(screen.getByTestId("followup-refresh"));
  await screen.findByRole("alert");
  expect(screen.queryByText("Follow-up disposition recorded.")).toBeNull();
  expect(screen.queryByText(/Submission #9/)).toBeNull();
});

it("preserves the original recovery command when the shared transport mutates its body", async () => {
  const { state } = fixture();
  state.mode = "lost";
  const ordinary = mockCall.getMockImplementation();
  mockCall.mockImplementation((method, options) => {
    if (method !== "concernFollowupCreate") return ordinary(method, options);
    Object.assign(options.body, { ...options.body });
    const result = ordinary(method, options);
    options.body.reason = "Transport-owned mutation after serialization";
    return result;
  });
  mount();
  await open();
  await record();
  fireEvent.click(await screen.findByTestId("followup-recover"));
  await waitFor(() => expect(state.calls).toHaveLength(2));
  expect(state.calls[1].body).toEqual(state.calls[0].body);
  expect(state.calls[1].headers).toEqual(state.calls[0].headers);
  await waitFor(() => expect(screen.queryByTestId("followup-recover")).toBeNull());
});
