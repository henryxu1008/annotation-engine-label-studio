import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { Response } from "node-fetch";
import { NerDisagreement, NerEntry } from "./NerDisagreement";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: React.forwardRef(({ children, look, size, ...props }, ref) =>
    React.createElement("button", { ...props, ref }, children)) };
});
const hash = (c) => c.repeat(64), ratio = (n, d = 1) => ({ numerator: String(n), denominator: String(d) });
const text = 'Alice joined Acme 😀 李 <img src="x">';
const per = { start: 0, end: 5, label: "PER" }, org = { start: 13, end: 17, label: "ORG" };
const pins = [1, 2, 3].map((id) => ({ submission_id: id, assignment_id: id, assignee_id: id + 10, task_id: 8,
  revision: 1, submitter_id: id + 10, result_hash: hash("a"), config_hash: hash("b"), task_data_hash: hash("c") }));
const sources = () => ({ schema: "ner-disagreements/v1", viewer: hash("e"), max_inputs: 20,
  scope: { project_id: 4, task_id: 8 }, sources: pins.map((p) => ({ id: p.submission_id, assignment_id: p.assignment_id,
    assignment__assignee_id: p.assignee_id, assignment__task_id: p.task_id, submitted_by_id: p.submitter_id,
    revision: p.revision, result_hash: p.result_hash, status: "pending" })) });
const response = (left = 1, right = 2) => {
  const entities = { 1: [per, org], 2: [per, { ...org, end: 16 }], 3: [per] };
  const pairs = [[1, 2], [1, 3], [2, 3]].map(([a, b]) => ({ left: a, right: b, left_entities: entities[a].length,
    right_entities: entities[b].length, matching_entities: 1, f1: a === 1 && b === 2 ? ratio(1, 2) : ratio(2, 3),
    availability: "available", reason: null }));
  return { schema: "ner-disagreements/v1", rule: "ner-strict-entities/v1", explanation: "ner-overlap-explanation/v1",
    engine: "nervaluate/0.2.0/strict-correct-counter", viewer: hash("e"), scope: { project_id: 4, task_id: 8, input_ids: [1, 2, 3] },
    manifest: { schema: "ner-disagreements/v1", rule: "ner-strict-entities/v1", explanation: "ner-overlap-explanation/v1",
      engine: "nervaluate/0.2.0/strict-correct-counter", project_id: 4, task_id: 8, viewer: hash("e"), inputs: pins,
      calculated_at: "2026-10-27T00:00:00+00:00", result_hash: hash("d") },
    manifest_hash: hash("f"), frozen: "ner-receipt", calculated_at: "2026-10-27T00:00:00+00:00", observed_at: "2026-10-27T00:00:01+00:00",
    source_status: "verified", receipt_verified: true, result_hash: hash("d"), availability: "available", reason: null,
    input_count: 3, total_pairs: 3, comparable_pairs: 3, unavailable_pairs: 0, pairs,
    selected_pair: { left, right, text, control: { from_name: "ner", to_name: "text", field: "text", labels: ["PER", "ORG"] },
      left_entities: entities[left], right_entities: entities[right], matches: [{ left: 0, right: 0 }],
      groups: [{ start: 13, end: left === 1 ? 17 : 16, left: [1], right: right === 2 ? [1] : [],
        kind: right === 2 ? "boundary_difference" : "left_only" }] } };
};
const mount = (locale = "en-US", entry = false) => {
  const runtime = createLocaleRuntime(locale), Provider = runtime.provider;
  return { ...render(<Provider>{entry ? <NerEntry projectId={4} /> : <NerDisagreement projectId={4} initialTaskId={8} />}</Provider>),
    runtime, Provider };
};
beforeEach(() => mockCallApi.mockReset().mockImplementation(async (method, args) => method === "nerSources" ? sources()
  : method === "nerCapability" ? { enabled: true, schema: "ner-disagreements/v1" }
    : response(args.params.left ?? 1, args.params.right ?? 2)));
const compare = async () => {
  fireEvent.click(screen.getByTestId("ner-load")); await screen.findAllByRole("checkbox");
  screen.getAllByRole("checkbox").forEach((box) => fireEvent.click(box));
  fireEvent.click(screen.getByTestId("ner-compare")); await screen.findByTestId("ner-detail");
};

it.each(["en-US", "zh-CN"])("compares explicit frozen revisions and renders safe differences in %s", async (locale) => {
  const view = mount(locale); await compare();
  expect(screen.getByTestId("ner-pair-1-2").textContent).toContain("50.00%");
  expect(screen.getByTestId("ner-pair-1-3").textContent).toContain("66.67%");
  expect(screen.getByTestId("ner-left-text").textContent).toBe(text);
  expect(screen.getByTestId("ner-left-text").querySelector("mark").textContent).toBe("Alice");
  expect(view.container.querySelector("img")).toBeNull();
  expect(screen.getByTestId("ner-difference").textContent).toContain(locale === "zh-CN" ? "边界不同" : "different boundaries");
  expect(mockCallApi.mock.calls[1][1].params.submissions).toBe("1,2,3");
  expect(mockCallApi.mock.calls.every(([method]) => ["nerSources", "nerReport"].includes(method))).toBe(true);
  view.unmount(); view.runtime.destroy();
});

it("keeps the selected pair and receipt on refresh and restores matrix focus", async () => {
  const view = mount(); await compare();
  fireEvent.click(screen.getByTestId("ner-pair-1-3"));
  await waitFor(() => expect(screen.getByTestId("ner-detail").textContent).toContain("#3"));
  fireEvent.click(screen.getByTestId("ner-read")); await screen.findByTestId("ner-detail");
  expect(mockCallApi.mock.calls.at(-1)[1].params).toMatchObject({ frozen: "ner-receipt", left: 1, right: 3 });
  expect(mockCallApi.mock.calls.at(-1)[1].params.submissions).toBeUndefined();
  fireEvent.click(screen.getByTestId("ner-back-matrix"));
  expect(screen.queryByTestId("ner-detail")).toBeNull();
  expect(document.activeElement).toBe(screen.getByTestId("ner-pair-1-3"));
  view.unmount(); view.runtime.destroy();
});

it.each(["en-US", "zh-CN"])("does not turn two empty sources into a perfect score in %s", async (locale) => {
  const view = mount(locale);
  mockCallApi.mockImplementation(async (method) => {
    if (method === "nerSources") return sources();
    const r = response(); r.availability = "unavailable"; r.reason = "no_entities"; r.comparable_pairs = 0; r.unavailable_pairs = 3;
    r.pairs.forEach((p) => Object.assign(p, { left_entities: 0, right_entities: 0, matching_entities: 0,
      f1: null, availability: "unavailable", reason: "no_entities" }));
    Object.assign(r.selected_pair, { left_entities: [], right_entities: [], matches: [], groups: [] });
    return r;
  });
  await compare();
  expect(screen.getByTestId("ner-pair-1-2").textContent).not.toContain("100.00");
  expect(screen.getByTestId("ner-unavailable").textContent).toContain(locale === "zh-CN" ? "双方均无实体" : "no entities");
  view.unmount(); view.runtime.destroy();
});

it("masks every answer after source loss, keeps the denominator and only retries the old GET", async () => {
  const view = mount(); await compare();
  mockCallApi.mockResolvedValue({ ...response(), source_status: "unavailable", receipt_verified: false, result_hash: null,
    availability: "unavailable", reason: "source_unavailable", pairs: null, selected_pair: null,
    comparable_pairs: undefined, unavailable_pairs: undefined });
  fireEvent.click(screen.getByTestId("ner-read")); await screen.findByTestId("ner-unavailable");
  expect(screen.queryByTestId("ner-left-text")).toBeNull();
  expect(screen.queryByTestId("ner-matrix")).toBeNull();
  expect(view.container.textContent).toContain("Frozen inputs: 3");
  expect(view.container.textContent).not.toContain("0 / 3");
  fireEvent.click(screen.getByTestId("ner-read"));
  await waitFor(() => expect(mockCallApi.mock.calls.at(-1)[1].params.frozen).toBe("ner-receipt"));
  view.unmount(); view.runtime.destroy();
});

it.each(["scope", "viewer", "denominator", "ratio", "hash", "time", "unmasked", "span", "native-failure"])(
  "clears old evidence after a malformed or revoked %s response", async (kind) => {
    const view = mount(); await compare(); const r = response();
    if (kind === "scope") r.scope.task_id = 99;
    if (kind === "viewer") r.viewer = hash("a");
    if (kind === "denominator") r.pairs[0].left_entities = 10;
    if (kind === "ratio") r.pairs[0].f1 = ratio(1, 1);
    if (kind === "hash") r.result_hash = hash("b");
    if (kind === "time") r.calculated_at = "different";
    if (kind === "unmasked") r.source_status = "unavailable";
    if (kind === "span") r.selected_pair.left_entities[0] = { ...per, end: 10000 };
    mockCallApi.mockResolvedValue(kind === "native-failure" ? new Response(null, { status: 403 }) : r);
    fireEvent.click(screen.getByTestId("ner-read")); await screen.findByRole("alert");
    expect(screen.queryByTestId("ner-left-text")).toBeNull(); expect(screen.queryByTestId("ner-matrix")).toBeNull();
    view.unmount(); view.runtime.destroy();
  });

it("discards delayed responses after blur, a new task, and unmount", async () => {
  const view = mount(); await compare();
  let resolve; mockCallApi.mockImplementation(() => new Promise((done) => { resolve = done; }));
  fireEvent.click(screen.getByTestId("ner-read")); fireEvent(window, new Event("blur"));
  await act(async () => resolve(response()));
  expect(screen.queryByTestId("ner-detail")).toBeNull();
  fireEvent.click(screen.getByTestId("ner-read"));
  fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "99" } });
  await act(async () => resolve(response()));
  expect(screen.queryByTestId("ner-detail")).toBeNull(); expect(screen.queryByTestId("ner-read")).toBeNull();
  view.unmount(); view.runtime.destroy();
});

it("selects only one revision per participant without automatically choosing any sources", async () => {
  const view = mount(); const r = sources(); r.sources.push({ ...r.sources[0], id: 4, revision: 2 });
  mockCallApi.mockResolvedValue(r); fireEvent.click(screen.getByTestId("ner-load")); await screen.findAllByRole("checkbox");
  const boxes = screen.getAllByRole("checkbox"); expect(boxes.every((box) => !box.checked)).toBe(true);
  fireEvent.click(boxes[0]); fireEvent.click(boxes[3]);
  expect(boxes[0].checked).toBe(false); expect(boxes[3].checked).toBe(true);
  expect(screen.getByTestId("ner-compare").disabled).toBe(true);
  view.unmount(); view.runtime.destroy();
});

it("shows an entry only for a live enabled capability and drops delayed capability reads", async () => {
  const view = mount("en-US", true); await screen.findByTestId("open-ner-disagreement");
  let resolve; mockCallApi.mockImplementation(() => new Promise((done) => { resolve = done; }));
  fireEvent(window, new Event("focus")); fireEvent(window, new Event("blur"));
  await act(async () => resolve({ enabled: true, schema: "ner-disagreements/v1" }));
  expect(screen.queryByTestId("open-ner-disagreement")).toBeNull();
  view.unmount(); view.runtime.destroy();
});
