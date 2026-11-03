// Validate the entire DTO before caching any answer or command acknowledgement.
import { z } from "zod";
import { fieldDigest } from "./fieldEvidence";

const id = z.number().int().positive().max(Number.MAX_SAFE_INTEGER);
const hash = z.string().regex(/^[0-9a-f]{64}$/);
const uuid = z.string().uuid();
const effect = z.literal("supervisory_only");
// Match the server's str.strip whitespace set, including its C0 separators.
export const hasReviewReason = (value) => typeof value === "string" && /[^\p{White_Space}\u001c-\u001f]/u.test(value);
// Python's server limit counts Unicode code points, not UTF-16 code units.
// Keep the same raw and JSON UTF-8 limits without normalizing the command.
export const reviewReasonSchema = z.string().refine((value) => {
  try {
    return (
      [...value].length <= 2000 &&
      !value.includes("\0") &&
      ![...value].some((c) => {
        const n = c.codePointAt(0);
        return n >= 0xd800 && n <= 0xdfff;
      }) &&
      new TextEncoder().encode(value).length <= 8192 &&
      new TextEncoder().encode(JSON.stringify(value)).length <= 8192
    );
  } catch {
    return false;
  }
});
const owner = z.object({ principal: z.string().min(1).max(512), source: z.string().min(1).max(128), actor_id: id });
const baseReceipt = z.object({
  schema: z.literal("second-review-command-receipt/v1"),
  command: z.enum(["dispatch", "decision", "cancel"]),
  task_id: uuid,
  project_id: id,
  organization_id: id,
  instance_id: uuid,
  owner,
  key: uuid,
  request_hash: hash,
  created_at: z.string().datetime(),
  effect,
  source_hash: hash,
});
export const receiptSchema = z.discriminatedUnion("command", [
  baseReceipt.extend({
    command: z.literal("dispatch"),
    first_review_hash: hash,
    batch_id: id,
    submission_id: id,
    revision: id,
    assignee_id: id,
    version: z.literal(1),
  }),
  baseReceipt.extend({
    command: z.literal("decision"),
    first_review_hash: hash,
    decision_id: uuid,
    outcome: z.enum(["confirmed", "concern"]),
    reason: reviewReasonSchema,
    version: z.literal(2),
  }),
  baseReceipt.extend({
    command: z.literal("cancel"),
    reason: reviewReasonSchema.refine(hasReviewReason),
    version: z.literal(2),
  }),
]);
export const capabilitySchema = z.object({
  effect,
  viewer_hash: hash,
  owner_hash: hash,
  can_read: z.boolean(),
  can_organize: z.boolean(),
  can_decide: z.boolean(),
  can_cancel: z.boolean(),
});
export const taskSchema = z
  .object({
    id: uuid,
    project_id: id,
    batch_id: id,
    submission_id: id,
    revision: id,
    assignee_id: id,
    status: z.enum(["pending", "completed", "cancelled"]),
    version: z.union([z.literal(1), z.literal(2)]),
    effect,
    source_hash: hash,
    first_review_hash: hash,
    created_at: z.string().datetime(),
    source_integrity: z.literal("not_checked"),
    viewer_hash: hash,
    owner_hash: hash,
    decision: receiptSchema.nullable(),
    cancellation: receiptSchema.nullable(),
  })
  .superRefine((r, ctx) => {
    const valid =
      r.status === "pending"
        ? r.version === 1 && !r.decision && !r.cancellation
        : r.status === "completed"
          ? r.version === 2 && r.decision?.command === "decision" && !r.cancellation
          : r.version === 2 && r.cancellation?.command === "cancel" && !r.decision;
    if (
      !valid ||
      [r.decision, r.cancellation]
        .filter(Boolean)
        .some((a) => a.task_id !== r.id || a.project_id !== r.project_id || a.source_hash !== r.source_hash)
    )
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "inconsistent task" });
  });
export const listSchema = z.object({
  count: z.number().int().nonnegative(),
  results: z.array(taskSchema).max(100),
  next: z.string().nullable(),
  viewer_hash: hash,
  owner_hash: hash,
  effect,
});
export const contextSchema = z.object({
  batch_id: id,
  submission_id: id,
  plan_hash: hash,
  source_hash: hash,
  effect,
  viewer_hash: hash,
  owner_hash: hash,
  eligible: z.boolean(),
  existing: taskSchema.nullable(),
  source_health: z.literal("target_source_verified"),
  metric_integrity: z.literal("not_checked"),
});
export const assigneeSchema = contextSchema.extend({
  count: z.number().int().nonnegative(),
  results: z.array(z.object({ id, display_name: z.string().min(1).max(1024) }).strict()).max(100),
  next: z.string().nullable(),
});
export const sourceSchema = z.object({
  task: taskSchema,
  result_snapshot: z
    .object({
      project: z.object({ id, label_config: z.string() }).passthrough(),
      task: z.object({ id, data: z.record(z.unknown()) }).passthrough(),
    })
    .passthrough(),
  first_review: z
    .object({
      schema: z.literal("first-review-proof/v1"),
      id,
      submission_id: id,
      reviewer_id: id,
      decision: z.enum(["approved", "rejected"]),
      reason: z.string(),
      created_at: z.string().datetime(),
    })
    .strict(),
  subject: z
    .object({
      submission_id: id,
      assignment_id: id,
      task_id: id,
      revision: id,
      assignee_id: id,
      submitter_id: id,
      result_hash: hash,
      task_data_hash: hash,
      label_config_sha256: hash,
    })
    .strict(),
  historical_revision: z.boolean(),
  source_health: z.literal("target_source_verified"),
  metric_integrity: z.literal("not_checked"),
  viewer_hash: hash,
  owner_hash: hash,
  effect,
});
export const ackSchema = z.object({
  receipt: receiptSchema,
  replayed: z.boolean(),
  source_integrity: z.literal("not_checked"),
  viewer_hash: hash,
  owner_hash: hash,
});
export const failed = (r) => !r || r.error || r.$meta?.ok === false || typeof r.status === "number";
export const parse = (schema, raw) => {
  if (failed(raw)) throw new Error("request failed");
  return schema.parse(raw);
};
export const sameOwner = (a, b) => !!a && !!b && a.owner_hash === b.owner_hash;
export const sourceMatches = (r, projectId, taskId, capability) =>
  r.task.id === taskId &&
  r.task.project_id === projectId &&
  sameOwner(r, capability) &&
  r.viewer_hash === capability.viewer_hash &&
  r.subject.submission_id === r.task.submission_id &&
  r.subject.revision === r.task.revision &&
  r.subject.task_id === r.result_snapshot.task.id &&
  r.result_snapshot.project.id === projectId &&
  r.first_review.submission_id === r.task.submission_id &&
  fieldDigest(r.first_review) === r.task.first_review_hash;
export const ackMatches = (r, intent, capability) =>
  sameOwner(r, capability) &&
  r.viewer_hash === capability.viewer_hash &&
  r.receipt.request_hash === fieldDigest(intent.body) &&
  r.receipt.command === intent.command &&
  r.receipt.key === intent.key &&
  r.receipt.project_id === intent.projectId &&
  (!intent.taskId || r.receipt.task_id === intent.taskId) &&
  (intent.command === "cancel" || r.receipt.source_hash === intent.body.expected_source_hash) &&
  (intent.command !== "dispatch" ||
    (r.receipt.batch_id === intent.body.batch_id &&
      r.receipt.submission_id === intent.body.submission_id &&
      r.receipt.assignee_id === intent.body.assignee_id)) &&
  (intent.command !== "decision" ||
    (r.receipt.outcome === intent.body.outcome && r.receipt.reason === intent.body.reason)) &&
  (intent.command !== "cancel" || r.receipt.reason === intent.body.reason);
export const definitiveRejection = (r) => {
  const reason = r?.response?.display_context?.reason ?? r?.display_context?.reason;
  return (
    failed(r) &&
    {
      SECOND_REVIEW_INVALID_INPUT: 400,
      SECOND_REVIEW_VIEWER_CHANGED: 409,
      SECOND_REVIEW_VERSION_CONFLICT: 409,
      SECOND_REVIEW_SUBJECT_EXISTS: 409,
      SECOND_REVIEW_SOURCE_CHANGED: 409,
      SECOND_REVIEW_ASSIGNEE_INELIGIBLE: 403,
      SECOND_REVIEW_INPUT_UNSUPPORTED: 400,
    }[reason] === r?.$meta?.status &&
    r?.$meta?.status !== undefined
  );
};
