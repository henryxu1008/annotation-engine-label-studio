import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createLocaleRuntime } from "@humansignal/i18n";
import { ClassificationDisagreement, ClassificationEntry } from "./ClassificationDisagreement";

const mockCallApi = jest.fn();
jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: mockCallApi }) }));
jest.mock("@humansignal/ui", () => {
  const React = require("react");
  return { Button: ({ children, ...props }) => React.createElement("button", props, children) };
});
const pins = [1, 2, 3].map((id) => ({ submission_id: id, revision: 1, assignment_id: id,
  task_id: 8, submitter_id: id + 10, result_hash: `hash-${id}`, config_hash: "config", status: "pending" }));
const response = () => ({ schema: "classification-disagreements/v1", frozen: "signed-receipt", manifest_hash: "frozen-hash",
  manifest: { project_id: 4, task_id: 8, rule: "classification-exact-sets/v1", calculated_at: "fixed-time", inputs: pins },
  observed_at: "now", status: "comparable", reason: null, matching_pairs: 1, total_pairs: 3, agreement: 1 / 3,
  locations: [{ from_name: "sentiment", to_name: "text", matching_pairs: 1, total_pairs: 3, agreement: 1 / 3,
    disagrees: true, values: [{ submission_id: 1, choices: ["Positive"] }, { submission_id: 2, choices: ["Negative"] }] }] });
const mount = (locale = "en-US", entry = false) => {
  const runtime = createLocaleRuntime(locale), Provider = runtime.provider;
  return { ...render(<Provider>{entry ? <ClassificationEntry projectId={4} />
    : <ClassificationDisagreement projectId={4} initialTaskId={8} />}</Provider>), runtime, Provider };
};
beforeEach(() => mockCallApi.mockReset().mockImplementation(async (method) => method === "classificationSources"
  ? { sources: pins, max_inputs: 20 } : method === "classificationCapability" ? { enabled: true } : response()));
const calculate = async (zh = false) => {
  fireEvent.click(screen.getByRole("button", { name: zh ? "读取可选修订" : "Load available revisions" }));
  await screen.findAllByRole("checkbox");
  screen.getAllByRole("checkbox").forEach((checkbox) => fireEvent.click(checkbox));
  fireEvent.click(screen.getByRole("button", { name: zh ? "冻结输入并对比" : "Freeze inputs and compare" }));
  await screen.findByTestId("classification-report");
};

it.each(["en-US", "zh-CN"])("shows frozen inputs, pair counts and disagreement locations in %s", async (locale) => {
  const zh = locale === "zh-CN", { unmount, runtime } = mount(locale);
  await calculate(zh);
  expect(screen.getByTestId("classification-agreement").textContent).toContain("33.33%");
  expect(screen.getByTestId("classification-location").textContent).toContain("Negative");
  expect(screen.getByTestId("classification-report").textContent).toContain("fixed-time");
  fireEvent.click(screen.getByRole("button", { name: zh ? "重验冻结输入" : "Recheck frozen inputs" }));
  await waitFor(() => expect(mockCallApi.mock.calls.at(-1)[1].params.frozen).toBe("signed-receipt"));
  await screen.findByTestId("classification-report");
  expect(mockCallApi.mock.calls.at(-1)[1].params.submissions).toBeUndefined();
  unmount(); runtime.destroy();
});

it.each(["en-US", "zh-CN"])("renders unavailable as no score and clears evidence after revoked read in %s", async (locale) => {
  const zh = locale === "zh-CN", { unmount, runtime } = mount(locale);
  const original = mockCallApi.getMockImplementation();
  mockCallApi.mockImplementation(async (method) => method === "classificationReport" ? {
    ...response(), status: "unavailable", reason: "config_mismatch", agreement: null,
    matching_pairs: null, total_pairs: null, locations: [],
  } : original(method));
  await calculate(zh);
  expect(screen.getByTestId("classification-unavailable").textContent).toContain(zh ? "标签配置不同" : "configurations differ");
  expect(screen.queryByTestId("classification-agreement")).toBeNull();
  mockCallApi.mockResolvedValue({ error: true, $meta: { ok: false, status: 403 } });
  fireEvent.click(screen.getByRole("button", { name: zh ? "重验冻结输入" : "Recheck frozen inputs" }));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("classification-report")).toBeNull();
  expect(screen.queryByRole("checkbox")).toBeNull();
  unmount(); runtime.destroy();
});

it("drops late answers after blur and allows a read-only frozen retry", async () => {
  const { unmount, runtime } = mount();
  await calculate();
  let finish;
  mockCallApi.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  fireEvent.click(screen.getByRole("button", { name: "Recheck frozen inputs" }));
  fireEvent(window, new Event("blur"));
  await act(async () => finish(response()));
  expect(screen.queryByTestId("classification-report")).toBeNull();
  expect(screen.getByRole("button", { name: "Recheck frozen inputs" }).disabled).toBe(false);
  unmount(); runtime.destroy();
});

it("rejects malformed display values and wrong scope", async () => {
  const { unmount, runtime } = mount();
  await calculate();
  mockCallApi.mockResolvedValue({ ...response(), locations: [{ ...response().locations[0], from_name: {} }] });
  fireEvent.click(screen.getByRole("button", { name: "Recheck frozen inputs" }));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("classification-report")).toBeNull();
  mockCallApi.mockResolvedValue({ ...response(), manifest: { ...response().manifest, project_id: 99 } });
  fireEvent.click(screen.getByRole("button", { name: "Recheck frozen inputs" }));
  await screen.findByRole("alert");
  expect(screen.queryByTestId("classification-report")).toBeNull();
  unmount(); runtime.destroy();
});

it("does not expose the entry when the capability read is denied", async () => {
  mockCallApi.mockResolvedValue({ error: true });
  const { unmount, runtime } = mount("en-US", true);
  await act(async () => {});
  expect(screen.queryByTestId("open-classification-disagreement")).toBeNull();
  unmount(); runtime.destroy();
});

it("drops late source identities after the task changes", async () => {
  const { unmount, runtime } = mount();
  let finish;
  mockCallApi.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  fireEvent.click(screen.getByRole("button", { name: "Load available revisions" }));
  fireEvent(window, new Event("blur"));
  fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "9" } });
  const next = pins.map((pin) => ({ ...pin, submission_id: pin.submission_id + 20, task_id: 9 }));
  mockCallApi.mockResolvedValueOnce({ sources: next, max_inputs: 20 });
  fireEvent.click(screen.getByRole("button", { name: "Load available revisions" }));
  await screen.findByRole("checkbox", { name: /Submission #21/ });
  await act(async () => finish({ sources: pins, max_inputs: 20 }));
  expect(screen.queryByRole("checkbox", { name: /Submission #1 ·/ })).toBeNull();
  expect(screen.getByRole("checkbox", { name: /Submission #21/ })).toBeTruthy();
  unmount(); runtime.destroy();
});

it("does not let a stale rejected request erase a successful frozen retry", async () => {
  const { unmount, runtime } = mount();
  await calculate();
  let reject;
  mockCallApi.mockImplementationOnce(() => new Promise((_, failure) => { reject = failure; }));
  fireEvent.click(screen.getByRole("button", { name: "Recheck frozen inputs" }));
  fireEvent(window, new Event("blur"));
  fireEvent.click(screen.getByRole("button", { name: "Recheck frozen inputs" }));
  await screen.findByTestId("classification-report");
  await act(async () => reject(new Error("late denied request")));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByTestId("classification-report").textContent).toContain("fixed-time");
  unmount(); runtime.destroy();
});

it("drops a pending report when the project scope changes", async () => {
  const { rerender, unmount, runtime, Provider } = mount();
  await calculate();
  let finish;
  mockCallApi.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  fireEvent.click(screen.getByRole("button", { name: "Recheck frozen inputs" }));
  rerender(<Provider><ClassificationDisagreement projectId={5} initialTaskId={8} /></Provider>);
  await act(async () => finish(response()));
  expect(screen.queryByTestId("classification-report")).toBeNull();
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Recheck frozen inputs" })).toBeNull();
  unmount(); runtime.destroy();
});
