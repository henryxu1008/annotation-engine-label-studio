import { bboxArbitrationAdapter } from "./BboxArbitrations";

jest.mock("../../providers/ApiProvider", () => ({ useAPI: () => ({ callApi: jest.fn() }) }));
jest.mock("@humansignal/ui", () => ({ Button: () => null }));

const h = (value) => value.repeat(64);
const pin = (id) => ({ submission_id: id, assignment_id: id, assignee_id: id + 10, task_id: 8, revision: 1,
  submitter_id: id + 10, result_hash: h("a"), config_hash: h("b"), task_data_hash: h("c"),
  media: { token: "b03bb7b2-8fa0-4ebf-9e4c-0cc4d5b70406", sha256: h("d"), proof_hash: h("e") } });
const detail = { id: 9, project_id: 4, schema: "bbox-arbitration/v1", batch_id: 7, task_id: 8, batch_name: "Synthetic",
  assignee_id: 30, input_set_hash: h("f"), viewer_hash: h("a"), status: "pending", version: 1, created_at: "now",
  calculated_at: "now", observed_at: "now", can_decide: true, can_cancel: false, selected_pair: [1, 2],
  decision: null, cancellation: null, origin: { project_id: 4, batch_id: 7, item: { task_id: 8,
    cohort: [1, 2].map((id) => ({ assignment_id: id, assignee_id: id + 10, source: pin(id) })),
    result: { availability: "available", reason: null, input_count: 2, image: { sha256: h("d"), width: 100, height: 100 },
      boxes: [[['Car', 0, 0, 20000000, 20000000]], [['Car', 60000000, 0, 20000000, 20000000]]],
      pairs: [{ left: 1, right: 2, f1: { numerator: "0", denominator: "1" }, matches: [], left_only: [0], right_only: [0] }] } } } };

it("validates the frozen cohort and limits adoption to the displayed pair", () => {
  const data = bboxArbitrationAdapter.parse("classificationArbitrationDetail", detail, { projectId: 4, arbitration: 9 });
  expect(bboxArbitrationAdapter.canAdopt(data, 1)).toBe(true);
  expect(bboxArbitrationAdapter.canAdopt(data, 999)).toBe(false);
  expect(() => bboxArbitrationAdapter.parse("classificationArbitrationDetail", { ...detail, selected_pair: [1, 99] },
    { projectId: 4, arbitration: 9 })).toThrow();
});

it("rejects acknowledgements for another actor or frozen input", () => {
  const intent = { projectId: 4, viewerHash: h("a"), actorId: 30, inputHash: h("f"), action: "decision", winnerRevision: 1 };
  const result = { project_id: 4, viewer_hash: h("a"), input_set_hash: h("f"), winner_revision: 1,
    receipt: { actor_id: 30, input_set_hash: h("f") } };
  expect(() => bboxArbitrationAdapter.validateAcknowledgement(result, intent)).not.toThrow();
  expect(() => bboxArbitrationAdapter.validateAcknowledgement({ ...result, receipt: { ...result.receipt, actor_id: 31 } }, intent)).toThrow();
});
