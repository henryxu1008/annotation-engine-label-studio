import { act, fireEvent, render, screen } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ClassificationOverview } from "./ClassificationOverview";
import { Response } from "node-fetch";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
const ratio = (n, d = 1) => ({ numerator: String(n), denominator: String(d) });
const batch = { id: 6, visibility: "project", manifest_hash: "a".repeat(64), calculated_at: "batch-time", visible_task_count: 3 };
const person = (actor = 11) => ({ actor_id: actor, tasks: 3, comparable_tasks: 2, unavailable_tasks: 1, disagreement_tasks: 1,
  peer_pairs: 3, matching_peer_pairs: 1, unavailable_reasons: [{ reason: "missing_submission", count: 1 }],
  task_ids: [8, 9, 10], coverage: ratio(2, 3), exact_agreement: ratio(1, 2), partial_match: ratio(17, 24) });
const response = () => ({ schema: "classification-quality-overview/v1", rules: { aggregation: "classification-batch-quality-task-macro/v1",
  exact: "classification-exact-sets/v1", partial: "classification-jaccard-nonempty/v1", selection: "active-assignment-latest/v1" },
  scope: { kind: "batch", project_id: 4, batch_id: 6, visibility: "project", reference_kind: "manifest_hash", reference_hash: batch.manifest_hash },
  source_set_hash: "b".repeat(64), viewer: "c".repeat(64), scope_counts: { tasks: 3, comparable_tasks: 2, unavailable_tasks: 1,
    unavailable_reasons: [{ reason: "missing_submission", count: 1 }] }, source_status: "verified",
  current_source_issues: { task_count: 0, reasons: [] }, availability: "available", reason: null,
  summary: { coverage: ratio(2, 3), unanimous_tasks: 1, disagreement_tasks: 1, exact_agreement: ratio(1, 2), partial_match: ratio(13, 18) },
  annotators: [person(11), person(12)], pagination: { count: 2, page: 1, page_size: 50, next_page: null, previous_page: null },
  frozen: "overview-receipt", result_hash: "d".repeat(64), receipt_verified: true,
  batch_calculated_at: "batch-time", summarized_at: "first-time", observed_at: "read-time" });
const mount = (locale = "en-US", navigation = { current: {} }) => {
  const runtime = createLocaleRuntime(locale), Provider = runtime.provider, onOpenTask = jest.fn();
  const props = { projectId: 4, batch, navigation, onOpenTask };
  return { ...render(<Provider><ClassificationOverview {...props} /></Provider>), runtime, Provider, props };
};
beforeEach(() => mockCallApi.mockReset().mockResolvedValue(response()));

it.each(["en-US", "zh-CN"])("shows task macro and person denominators with read-only replay in %s", async (locale) => {
  const view = mount(locale);
  fireEvent.click(screen.getByTestId("quality-read"));
  expect((await screen.findByTestId("quality-summary")).textContent).toContain("72.22%");
  expect(screen.getByTestId("quality-summary").textContent).toContain("50.00%");
  expect(view.container.querySelector('[data-quality-actor="11"]').textContent).toContain("70.83%");
  fireEvent.click(view.container.querySelector('[data-quality-task="8"]'));
  expect(view.props.onOpenTask).toHaveBeenCalledWith(8);
  fireEvent.click(screen.getByTestId("quality-read"));
  await screen.findByTestId("quality-summary");
  expect(mockCallApi.mock.calls[1][1].params.frozen).toBe("overview-receipt");
  expect(mockCallApi.mock.calls.every(([method]) => method === "classificationQualityOverview")).toBe(true);
  view.unmount(); view.runtime.destroy();
});

it("hides all scores and task entries after source loss while retaining the original receipt", async () => {
  const view = mount(); fireEvent.click(screen.getByTestId("quality-read")); await screen.findByTestId("quality-summary");
  mockCallApi.mockResolvedValue({ ...response(), source_status: "unavailable", availability: "unavailable", reason: "source_unavailable",
    summary: null, annotators: null, pagination: null, result_hash: null, receipt_verified: false,
    current_source_issues: { task_count: 1, reasons: [{ reason: "source_unavailable", count: 1 }] } });
  fireEvent.click(screen.getByTestId("quality-read"));
  await screen.findByText(/denominator is unchanged/);
  expect(screen.queryByTestId("quality-summary")).toBeNull();
  expect(view.container.querySelector('[data-quality-task]')).toBeNull();
  expect(view.props.navigation.current.frozen).toBe("overview-receipt");
  view.unmount(); view.runtime.destroy();
});

it.each(["scope", "denominator", "hash", "native-failure", "unmasked"])("rejects malformed or stale %s responses", async (kind) => {
  const view = mount(); fireEvent.click(screen.getByTestId("quality-read")); await screen.findByTestId("quality-summary");
  const data = response();
  if (kind === "scope") data.scope.project_id = 100;
  if (kind === "denominator") data.annotators[0].tasks = 2;
  if (kind === "hash") data.result_hash = "f".repeat(64);
  if (kind === "unmasked") data.source_status = "unavailable";
  mockCallApi.mockResolvedValue(kind === "native-failure" ? new Response(null, { status: 403 }) : data);
  fireEvent.click(screen.getByTestId("quality-read")); await screen.findByRole("alert");
  expect(screen.queryByTestId("quality-summary")).toBeNull(); expect(view.container.querySelector('[data-quality-task]')).toBeNull();
  view.unmount(); view.runtime.destroy();
});

it("drops late reads after blur and after changing project scope", async () => {
  let resolve; mockCallApi.mockReturnValue(new Promise((done) => { resolve = done; }));
  const view = mount(); fireEvent.click(screen.getByTestId("quality-read"));
  act(() => window.dispatchEvent(new Event("blur")));
  await act(async () => resolve(response()));
  expect(screen.queryByTestId("quality-summary")).toBeNull();
  mockCallApi.mockReturnValue(new Promise((done) => { resolve = done; })); fireEvent.click(screen.getByTestId("quality-read"));
  view.rerender(<view.Provider><ClassificationOverview {...view.props} projectId={99} /></view.Provider>);
  await act(async () => resolve(response()));
  expect(screen.queryByTestId("quality-summary")).toBeNull();
  view.unmount(); view.runtime.destroy();
});

it("restores saved pagination on return using the same frozen receipt", async () => {
  const navigation = { current: {} }, view = mount("en-US", navigation);
  const first = response(); first.pagination = { count: 51, page: 1, page_size: 50, next_page: 2, previous_page: null };
  first.annotators = Array.from({ length: 50 }, (_, i) => person(i + 1)); mockCallApi.mockResolvedValue(first);
  fireEvent.click(screen.getByTestId("quality-read")); await screen.findByTestId("quality-summary");
  const second = { ...response(), annotators: [person(51)], pagination: { count: 51, page: 2, page_size: 50, next_page: null, previous_page: 1 } };
  mockCallApi.mockResolvedValue(second); fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  await screen.findByText("Annotator #51"); view.unmount(); view.runtime.destroy();
  const returned = mount("en-US", navigation); await screen.findByText("Annotator #51");
  expect(mockCallApi.mock.calls.at(-1)[1].params).toEqual({ pk: 4, batch: 6, page: 2, frozen: "overview-receipt" });
  returned.unmount(); returned.runtime.destroy();
});
