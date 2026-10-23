import { validBatchCapability, validBatchDetail, validBatchList } from "./reviewBatchResponses";

const detail = () => ({
  id: 7, name: "Sample 样本", plan_hash: "a".repeat(64), as_of: "2026-10-24T00:00:00Z",
  role: "reviewer", algorithm: "sha256-ranked-v1", rule: { rate: 50, seed: "fixed" },
  candidate_count: 1, selected_count: 1,
  items: [{ submission_id: 11, revision: 1, selected: true, status: "rejected", can_review: false,
    review_reason: "Fix 修正", successor: { id: 12, revision: 2, status: "pending" } }],
});

it("accepts the existing unavailable/unknown successor and empty-reason contract", () => {
  for (const status of ["future-status", null, undefined]) {
    for (const review_reason of [null, "", undefined]) {
      const value = detail();
      value.items[0].successor.status = status;
      value.items[0].review_reason = review_reason;
      expect(validBatchDetail(value, 7)).toBe(true);
    }
  }
  const value = detail();
  value.items[0].successor = null;
  expect(validBatchDetail(value, 7)).toBe(true);
});

it.each(["id", "name", "plan_hash", "as_of", "role", "algorithm", "candidate_count", "selected_count"])(
  "rejects an object in the rendered detail field %s", (field) => {
    expect(validBatchDetail({ ...detail(), [field]: {} }, 7)).toBe(false);
  },
);
it.each(["rate", "seed"])("rejects an object in rule.%s", (field) => {
  const value = detail(); value.rule[field] = {};
  expect(validBatchDetail(value, 7)).toBe(false);
});
it.each(["submission_id", "revision", "selected", "status", "can_review", "review_reason"])(
  "rejects an object in the rendered item field %s", (field) => {
    const value = detail(); value.items[0][field] = {};
    expect(validBatchDetail(value, 7)).toBe(false);
  },
);
it.each(["id", "revision", "status"])("rejects an object in successor.%s", (field) => {
  const value = detail(); value.items[0].successor[field] = {};
  expect(validBatchDetail(value, 7)).toBe(false);
});
it("rejects truthy booleans, wrong scope, invalid rows and inconsistent counts", () => {
  for (const field of ["selected", "can_review"]) {
    const value = detail(); value.items[0][field] = "false";
    expect(validBatchDetail(value, 7)).toBe(false);
  }
  expect(validBatchDetail(detail(), 8)).toBe(false);
  expect(validBatchDetail({ ...detail(), items: [null] }, 7)).toBe(false);
  expect(validBatchDetail({ ...detail(), candidate_count: 2 }, 7)).toBe(false);
  expect(validBatchDetail({ ...detail(), selected_count: 0 }, 7)).toBe(false);
});
it("validates list row labels and capability booleans without coercion", () => {
  expect(validBatchList({ results: { batches: [{ id: 7, name: "Sample" }] } })).toBe(true);
  expect(validBatchList({ results: { batches: [{ id: 7, name: {} }] } })).toBe(false);
  expect(validBatchList({ results: { batches: [null] } })).toBe(false);
  expect(validBatchCapability({ enabled: true, can_create: false })).toBe(true);
  expect(validBatchCapability({ enabled: true, can_create: "false" })).toBe(false);
});
