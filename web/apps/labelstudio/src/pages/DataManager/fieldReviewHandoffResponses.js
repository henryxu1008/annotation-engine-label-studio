import { z } from "zod";
import { validFieldCost } from "./fieldReviewPriority";
import { handoffCommandHash } from "./reviewHandoffCommands";

const SCHEMA = "review-field-arbitration-handoff/v1";
const id = z.number().int().positive().safe();
const hash = z.string().regex(/^[0-9a-f]{64}$/);
const state = z.enum(["pending", "resolved", "cancelled"]);
const mode = z.enum(["dispatch", "link_existing"]);
const scope = {
  schema: z.literal(SCHEMA),
  kind: z.literal("field"),
  project_id: id,
  viewer_hash: hash,
  review_batch_id: id,
  submission_id: id,
};
const targetAtLink = z.object({
  id,
  input_set_hash: hash,
  created_at: z.string(),
  assignee_id: id,
  batch_id: id,
  task_id: id,
  status_at_link: z.enum(["pending", "resolved"]),
  version_at_link: id,
});
const target = z.object({
  id,
  viewer_hash: hash,
  input_set_hash: hash,
  task_evidence_hash: hash,
  field_batch_id: id,
  task_id: id,
  created_at: z.string(),
  assignee_id: id,
  status: state,
  version: id,
});
const eligibleContext = z.object({
  ...scope,
  actor_id: id,
  eligible: z.literal(true),
  reason: z.null(),
  revision: id,
  expected_plan_hash: hash,
  expected_input_set_hash: hash,
  can_handoff: z.boolean(),
  dispatch_origin: z.object({ field_batch_id: id, task_id: id, expected_manifest_hash: hash }),
  existing_arbitration: target.extend({ schema: z.literal("field-arbitrations/v1") }).nullable(),
});
export const fieldHandoffContextSchema = z.union([
  eligibleContext,
  z.object({ ...scope, actor_id: id, eligible: z.literal(false), reason: z.string(), can_handoff: z.literal(false) }),
]);
export const fieldHandoffDetailSchema = z.object({
  ...scope,
  id,
  actor_id: id,
  revision: id,
  arbitration_id: id,
  input_set_hash: hash,
  mode,
  created_at: z.string(),
  available: z.boolean(),
  source_integrity: z.literal("not_checked"),
  source_status: z.enum(["pending", "approved", "rejected", "superseded", "unavailable"]),
  arbitration_status: state.nullable(),
  arbitration_version: id.nullable(),
  target: targetAtLink,
  decision: z.null(),
});
export const fieldHandoffListSchema = z.object({
  ...scope,
  count: z.number().int().nonnegative().safe(),
  next: z.string().nullable(),
  previous: z.string().nullable(),
  results: z.array(fieldHandoffDetailSchema).max(100),
});
export const fieldHandoffReceiptSchema = z.object({
  ...scope,
  id,
  actor_id: id,
  arbitration_id: id,
  input_set_hash: hash,
  assignee_id: id,
  replayed: z.boolean(),
  receipt: z.object({
    operation: z.literal("handoff"),
    key: z.string().uuid(),
    request_hash: hash,
    mode,
    plan_hash: hash,
    origin_hash: hash,
    arbitration_status_at_commit: z.enum(["pending", "resolved"]),
    arbitration_version_at_commit: id,
  }),
});
export const fieldHandoffProvenanceSchema = z.object({
  ...scope,
  id,
  arbitration_id: id,
  snapshot_integrity: z.literal("not_checked"),
  provenance_status: z.enum(["verified", "unavailable"]),
  reason: z.string().nullable(),
  target: target.nullable(),
});
const valid = (value) => {
  if (!value) throw new Error("Field handoff context changed");
};
const versionMatches = (status, version) => version === (status === "pending" ? 1 : 2);
const cost = (raw, sourceFree) => {
  valid(raw?.validation_passes === 2);
  const work = { ...raw };
  delete work.validation_passes;
  valid(validFieldCost(work, sourceFree));
  if (!sourceFree) valid(work.per_pass.normalized_values >= 2 && work.per_pass.field_equalities >= 2);
};
const methods = {
  reviewHandoffContext: "fieldReviewHandoffContext",
  reviewHandoffDetail: "fieldReviewHandoffDetail",
  reviewHandoffProvenance: "fieldReviewHandoffProvenance",
  createReviewHandoff: "createFieldReviewHandoff",
};
const schemas = {
  reviewHandoffContext: fieldHandoffContextSchema,
  reviewHandoffDetail: fieldHandoffDetailSchema,
  reviewHandoffProvenance: fieldHandoffProvenanceSchema,
  createReviewHandoff: fieldHandoffReceiptSchema,
};

export const fieldReviewHandoff = {
  kind: "field",
  linkField: "field_arbitration_id",
  methods,
  requiresViewerHash: true,
  maskUnavailableSource: true,
  sameOwner: (authority, intent) =>
    hash.safeParse(intent.handoffOwnerHash).success &&
    authority.handoff_owner_hash === intent.handoffOwnerHash &&
    authority.actor_id === intent.actorId,
  listMethod: "fieldReviewHandoffs",
  listSchema: fieldHandoffListSchema,
  capabilityMethod: "fieldArbitrationCapability",
  title: "fieldHandoffTitle",
  boundary: "fieldHandoffBoundary",
  integrity: "fieldHandoffIntegrity",
  rejected: Object.fromEntries(
    [
      ...["INVALID_INPUT", "INVALID_KEY"].map((code) => [code, 400]),
      ...[
        "AUTHORITY_CHANGED",
        "KEY_CONFLICT",
        "SOURCE_CHANGED",
        "SOURCE_NOT_PENDING",
        "SOURCE_UNAVAILABLE",
        "INELIGIBLE",
        "UNSUPPORTED_BATCH",
        "ALREADY_EXISTS",
        "CAS_CONFLICT",
        "INPUT_SET_CHANGED",
        "ASSIGNEE_UNAVAILABLE",
        "DUPLICATE_ACTIVE",
        "ORIGIN_CHANGED",
        "ORIGIN_INVALID",
        "LEDGER_INVALID",
      ].map((code) => [code, 409]),
      ["BUDGET_EXCEEDED", 422],
    ].map(([code, status]) => [`REVIEW_FIELD_HANDOFF_${code}`, status]),
  ),
  scopeMatches: (row, source, projectId) =>
    row.kind === "field" &&
    row.project_id === Number(projectId) &&
    row.review_batch_id === source.batch &&
    row.submission_id === source.source,
  parseCapability: (raw) =>
    z
      .object({
        enabled: z.literal(true),
        schema: z.literal("field-arbitrations/v1"),
        actor_id: id,
        can_read: z.boolean(),
        can_create: z.boolean(),
        viewer_hash: hash,
      })
      .parse(raw),
  validateList: (raw) => cost(raw.processing_cost, true),
  parse(method, raw, expected) {
    valid(schemas[method]);
    const value = schemas[method].parse(raw);
    valid(value.project_id === Number(expected.projectId));
    cost(
      raw.processing_cost,
      (method !== "createReviewHandoff" && (method !== "reviewHandoffContext" || !value.eligible)) ||
        value.replayed === true,
    );
    if (method === "reviewHandoffContext" && value.eligible) {
      if (value.existing_arbitration) {
        valid(
          value.existing_arbitration.input_set_hash === value.expected_input_set_hash &&
            value.existing_arbitration.viewer_hash === value.viewer_hash &&
            versionMatches(value.existing_arbitration.status, value.existing_arbitration.version) &&
            value.existing_arbitration.status !== "cancelled",
        );
      }
      return {
        ...value,
        dispatch_origin: { ...value.dispatch_origin, batch_id: value.dispatch_origin.field_batch_id },
      };
    }
    if (method === "reviewHandoffDetail") {
      valid(
        value.target.id === value.arbitration_id &&
          value.target.input_set_hash === value.input_set_hash &&
          versionMatches(value.target.status_at_link, value.target.version_at_link),
      );
      valid(
        value.arbitration_status === null
          ? value.arbitration_version === null
          : versionMatches(value.arbitration_status, value.arbitration_version),
      );
    }
    if (method === "createReviewHandoff") {
      valid(versionMatches(value.receipt.arbitration_status_at_commit, value.receipt.arbitration_version_at_commit));
    }
    if (method === "reviewHandoffProvenance") {
      valid(
        value.provenance_status === "verified"
          ? value.target !== null && value.reason === null
          : value.target === null && value.reason !== null,
      );
    }
    return value;
  },
  commandHash: (projectId, source, body) => handoffCommandHash(SCHEMA, projectId, source, body),
  validateAcknowledgement(result, intent) {
    valid(
      result.actor_id === intent.actorId &&
        result.project_id === Number(intent.projectId) &&
        result.receipt.request_hash === intent.requestHash,
    );
    const expectedAssignee = intent.body.mode === "dispatch" ? intent.body.assignee_id : intent.handoffAssigneeId;
    valid(result.assignee_id === expectedAssignee);
  },
  verifyProvenance(value, detail, source, handoffId, projectId) {
    valid(
      this.scopeMatches(value, source, projectId) &&
        value.id === handoffId &&
        value.arbitration_id === detail.id &&
        value.viewer_hash === detail.viewer_hash,
    );
    if (value.provenance_status !== "verified") return false;
    const expected = value.target;
    valid(
      expected.id === detail.id &&
        [
          "viewer_hash",
          "input_set_hash",
          "task_evidence_hash",
          "field_batch_id",
          "task_id",
          "created_at",
          "assignee_id",
          "status",
          "version",
        ].every((key) => expected[key] === detail[key]),
    );
    return detail.available === true;
  },
};
