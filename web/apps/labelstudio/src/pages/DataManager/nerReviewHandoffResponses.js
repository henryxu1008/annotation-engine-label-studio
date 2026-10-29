import { z } from "zod";
import { sha256 } from "@noble/hashes/sha256";
import { bytesToHex } from "@noble/hashes/utils";

const id = z.number().int().positive().safe();
const hash = z.string().regex(/^[0-9a-f]{64}$/);
const schema = z.literal("review-ner-arbitration-handoff/v1");
const kind = z.literal("ner");
const state = z.enum(["pending", "resolved", "cancelled"]);
const mode = z.enum(["dispatch", "link_existing"]);
const scope = { schema, kind, project_id: id, viewer_hash: hash, review_batch_id: id, submission_id: id };
const target = z.object({
  id,
  origin_hash: hash,
  input_set_hash: hash,
  created_at: z.string(),
  actor_id: id,
  assignee_id: id,
  batch_id: id,
  status_at_link: state,
  version_at_link: id,
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
  dispatch_origin: z.object({ ner_batch_id: id, task_id: id, expected_manifest_hash: hash }),
  existing_arbitration: z
    .object({
      id,
      schema: z.literal("ner-arbitrations/v1"),
      input_set_hash: hash,
      viewer_hash: hash,
      status: state,
      version: id,
    })
    .passthrough()
    .nullable(),
});
export const nerHandoffContextSchema = z.union([
  eligibleContext,
  z.object({ ...scope, actor_id: id, eligible: z.literal(false), reason: z.string(), can_handoff: z.literal(false) }),
]);
export const nerHandoffDetailSchema = z.object({
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
  target,
  decision: z.null(),
});
export const nerHandoffListSchema = z.object({
  ...scope,
  count: z.number().int().nonnegative().safe(),
  next: z.string().nullable(),
  previous: z.string().nullable(),
  results: z.array(nerHandoffDetailSchema).max(100),
});
export const nerHandoffReceiptSchema = z.object({
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
    arbitration_version_at_commit: id,
    arbitration_status_at_commit: state,
  }),
});
const methods = {
  reviewHandoffContext: "nerReviewHandoffContext",
  reviewHandoffDetail: "nerReviewHandoffDetail",
  createReviewHandoff: "createNerReviewHandoff",
};
const schemas = {
  reviewHandoffContext: nerHandoffContextSchema,
  reviewHandoffDetail: nerHandoffDetailSchema,
  createReviewHandoff: nerHandoffReceiptSchema,
};
const valid = (ok) => {
  if (!ok) throw new Error("NER handoff identity changed");
};
const canonical = (value) =>
  value && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(
        Object.keys(value)
          .sort()
          .map((key) => [key, canonical(value[key])]),
      )
    : value;

export const nerReviewHandoff = {
  kind: "ner",
  linkField: "ner_arbitration_id",
  methods,
  listMethod: "nerReviewHandoffs",
  listSchema: nerHandoffListSchema,
  capabilityMethod: "nerArbitrationCapability",
  title: "nerHandoffTitle",
  boundary: "nerHandoffBoundary",
  rejected: Object.fromEntries(
    [
      ...["INVALID_INPUT", "INVALID_KEY"].map((k) => [k, 400]),
      ...[
        "AUTHORITY_CHANGED",
        "KEY_CONFLICT",
        "SOURCE_CHANGED",
        "SOURCE_NOT_PENDING",
        "INELIGIBLE",
        "UNSUPPORTED_BATCH",
        "ALREADY_EXISTS",
        "CAS_CONFLICT",
        "INPUT_SET_CHANGED",
      ].map((k) => [k, 409]),
      ["BUDGET_EXCEEDED", 422],
    ].map(([key, status]) => [`REVIEW_NER_HANDOFF_${key}`, status]),
  ),
  scopeMatches: (row, source, projectId) =>
    row.kind === "ner" &&
    row.project_id === Number(projectId) &&
    row.review_batch_id === source.batch &&
    row.submission_id === source.source,
  parseCapability: (raw) =>
    z
      .object({
        enabled: z.literal(true),
        schema: z.literal("ner-arbitrations/v1"),
        actor_id: id,
        can_read: z.boolean(),
        can_create: z.boolean(),
        viewer_hash: hash,
      })
      .parse(raw),
  parse: (method, raw, expected) => {
    valid(schemas[method]);
    const value = schemas[method].parse(raw);
    valid(value.project_id === Number(expected.projectId));
    if (method === "reviewHandoffContext" && value.eligible)
      return {
        ...value,
        dispatch_origin: { ...value.dispatch_origin, batch_id: value.dispatch_origin.ner_batch_id },
      };
    return value;
  },
  async commandHash(projectId, source, body) {
    const payload = canonical({
      schema: "review-ner-arbitration-handoff/v1",
      action: "handoff",
      project_id: Number(projectId),
      batch_id: source.batch,
      source_id: source.source,
      body,
    });
    return bytesToHex(sha256(JSON.stringify(payload)));
  },
  validateAcknowledgement(result, intent) {
    valid(
      result.actor_id === intent.actorId &&
        result.project_id === Number(intent.projectId) &&
        result.receipt.request_hash === intent.requestHash,
    );
    if (intent.body.mode === "dispatch") valid(result.assignee_id === intent.body.assignee_id);
  },
};
