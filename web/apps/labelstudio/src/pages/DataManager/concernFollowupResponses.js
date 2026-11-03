import { z } from "zod";
import stableStringify from "fast-json-stable-stringify";
import { sha256 } from "@noble/hashes/sha256";
import { bytesToHex } from "@noble/hashes/utils";
import { fieldDigest } from "./fieldEvidence";
import {
  receiptSchema as secondReceipt,
  sourceSchema,
  reviewReasonSchema,
  hasReviewReason,
} from "./secondReviewResponses";
import { definitiveRejection } from "./secondReviewResponses";

const id = z.number().int().positive().max(Number.MAX_SAFE_INTEGER);
const hash = z.string().regex(/^[0-9a-f]{64}$/);
const uuid = z.string().uuid();
const time = z.string().datetime();
const effect = z.literal("supervisory_only");
const subject = sourceSchema.shape.subject;
const initial = sourceSchema.shape.first_review;
const snapshot = sourceSchema.shape.result_snapshot;
const opinion = secondReceipt.refine(
  (r) => r.command === "decision" && r.outcome === "concern" && hasReviewReason(r.reason),
);
const envelope = { effect, owner_hash: hash, viewer_hash: hash };

export const followupReceipt = z
  .object({
    schema: z.literal("second-review-followup-receipt/v1"),
    record_schema: z.literal("second-review-concern-followup/v1"),
    rule: z.literal("independent-disposition/v1"),
    command: z.literal("record"),
    followup_id: uuid,
    project_id: id,
    organization_id: id,
    instance_id: uuid,
    owner: z
      .object({ principal: z.string().min(1).max(512), source: z.string().min(1).max(128), actor_id: id })
      .strict(),
    key: uuid,
    request_hash: hash,
    created_at: time,
    effect,
    version: z.literal(1),
    second_review_task_id: uuid,
    opinion_id: uuid,
    concern_hash: hash,
    origin_ref_hash: hash,
    context_hash: hash,
    disposition: z.enum(["no_change", "revision_linked"]),
    reason: reviewReasonSchema.refine(hasReviewReason),
    related_submission_id: id.nullable(),
    target_ref_hash: hash,
  })
  .strict()
  .superRefine((r, ctx) => {
    if (
      !hasReviewReason(r.reason) ||
      r.origin_ref_hash !== r.concern_hash ||
      (r.disposition === "no_change") !== (r.related_submission_id === null)
    )
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "inconsistent disposition" });
  });

const target = z
  .object({
    subject,
    status_at_record: z.enum(["pending", "approved", "rejected"]),
    first_review: initial.nullable(),
    first_review_hash: hash.nullable(),
  })
  .strict()
  .superRefine((r, ctx) => {
    if (
      r.status_at_record === "pending"
        ? r.first_review !== null || r.first_review_hash !== null
        : !r.first_review ||
          r.first_review.decision !== r.status_at_record ||
          r.first_review.submission_id !== r.subject.submission_id ||
          r.first_review.reviewer_id === r.subject.submitter_id ||
          fieldDigest(r.first_review) !== r.first_review_hash
    )
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "invalid target review" });
  });
export const targetRef = (value) =>
  value
    ? {
        subject: value.subject,
        status_at_record: value.status_at_record,
        first_review: value.first_review,
        first_review_hash: value.first_review_hash,
      }
    : null;

const summary = z
  .object({
    task_id: uuid,
    opinion_id: uuid,
    submission_id: id,
    revision: id,
    batch_id: id,
    opinion_created_at: time,
    opinion,
    followup_id: uuid.nullable(),
    recorded: followupReceipt.nullable(),
  })
  .strict()
  .superRefine((r, ctx) => {
    if (
      r.opinion.task_id !== r.task_id ||
      r.opinion.decision_id !== r.opinion_id ||
      r.opinion.created_at !== r.opinion_created_at ||
      (r.followup_id === null) !== (r.recorded === null) ||
      (r.recorded &&
        (r.recorded.followup_id !== r.followup_id ||
          r.recorded.second_review_task_id !== r.task_id ||
          r.recorded.opinion_id !== r.opinion_id ||
          r.recorded.project_id !== r.opinion.project_id ||
          r.recorded.instance_id !== r.opinion.instance_id))
    )
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "inconsistent history" });
  });

export const followupList = z
  .object({
    ...envelope,
    count: z.number().int().nonnegative(),
    results: z.array(summary).max(100),
    next: z.string().nullable(),
    can_record: z.boolean(),
    source_integrity: z.literal("not_checked"),
  })
  .strict();
export const followupContext = z
  .object({
    ...envelope,
    second_review_task_id: uuid,
    opinion_id: uuid,
    concern_hash: hash,
    context_hash: hash,
    expected_version: z.literal(0),
    original: z
      .object({
        subject,
        first_review: initial,
        result_snapshot: snapshot,
        historical_revision: z.boolean(),
        presentation_hash: hash,
      })
      .strict(),
    opinion,
    target: z
      .object({ ...target.innerType().shape, result_snapshot: snapshot, presentation_hash: hash })
      .strict()
      .nullable(),
    candidates: z
      .array(z.object({ id, revision: id, status: z.enum(["pending", "approved", "rejected", "superseded"]) }).strict())
      .max(1),
    source_health: z.literal("target_source_verified"),
    metric_integrity: z.literal("not_checked"),
  })
  .strict();
export const followupDetail = z
  .object({
    ...envelope,
    id: uuid,
    receipt: followupReceipt,
    source_integrity: z.literal("not_checked"),
    metric_integrity: z.literal("not_checked"),
    original: z.object({ subject, first_review: initial, opinion }).strict(),
    target: target.nullable(),
  })
  .strict();
export const followupAck = z
  .object({
    ...envelope,
    receipt: followupReceipt,
    replayed: z.boolean(),
    source_integrity: z.literal("not_checked"),
  })
  .strict();

export const listMatches = (r, project) =>
  r.results.every((v) => v.opinion.project_id === project && (!v.recorded || v.recorded.project_id === project));
const same = (r, cap) => r.owner_hash === cap.owner_hash && r.viewer_hash === cap.viewer_hash;
// Legacy result_hash preserves Python numeric representations (e.g. 1.0).
// Verify the new RFC 8785 presentation hash without redefining that old pin.
const snapshotMatches = (p, value, project, presentation) =>
  value.project.id === project && value.task.id === p.task_id && presentationDigest(value) === presentation;
// Unlike Object.fromEntries + JSON.stringify, this existing serializer emits
// numeric-string object keys in lexicographic order, matching RFC 8785.
export const presentationDigest = (value) => bytesToHex(sha256(stableStringify(value)));
export const contextMatches = (r, project, task, selectedTarget, cap) => {
  const old = r.original;
  const selected = r.target;
  const proof = targetRef(selected);
  const originOK =
    r.second_review_task_id === task &&
    r.opinion.task_id === task &&
    r.opinion.decision_id === r.opinion_id &&
    r.opinion.project_id === project &&
    old.first_review.submission_id === old.subject.submission_id &&
    old.first_review.reviewer_id !== old.subject.submitter_id &&
    fieldDigest(old.first_review) === r.opinion.first_review_hash &&
    snapshotMatches(old.subject, old.result_snapshot, project, old.presentation_hash);
  const targetOK =
    selectedTarget === null
      ? selected === null
      : selected &&
        selected.subject.submission_id === selectedTarget &&
        target.safeParse(proof).success &&
        selected.subject.revision > old.subject.revision &&
        ["assignment_id", "task_id", "assignee_id", "submitter_id", "task_data_hash", "label_config_sha256"].every(
          (k) => selected.subject[k] === old.subject[k],
        ) &&
        snapshotMatches(selected.subject, selected.result_snapshot, project, selected.presentation_hash);
  return (
    same(r, cap) &&
    originOK &&
    targetOK &&
    fieldDigest({
      schema: "second-review-concern-followup/v1",
      rule: "independent-disposition/v1",
      effect: "supervisory_only",
      project_id: project,
      organization_id: r.opinion.organization_id,
      instance_id: r.opinion.instance_id,
      concern_hash: r.concern_hash,
      target: proof,
    }) === r.context_hash
  );
};
export const detailMatches = (r, project, followup, cap) =>
  same(r, cap) &&
  r.id === followup &&
  r.receipt.followup_id === followup &&
  r.receipt.project_id === project &&
  r.original.opinion.project_id === project &&
  r.receipt.second_review_task_id === r.original.opinion.task_id &&
  r.receipt.opinion_id === r.original.opinion.decision_id &&
  r.original.first_review.submission_id === r.original.subject.submission_id &&
  fieldDigest(r.original.first_review) === r.original.opinion.first_review_hash &&
  r.receipt.target_ref_hash === fieldDigest(r.target) &&
  r.receipt.related_submission_id === (r.target?.subject.submission_id ?? null);
export const receiptOwnerHash = (r) =>
  fieldDigest({
    schema: "second-review-concern-followup/v1",
    owner: fieldDigest({
      principal: r.owner.principal,
      source: r.owner.source,
      actor: r.owner.actor_id,
      project: r.project_id,
      organization: r.organization_id,
      role: "manager",
      instance_id: r.instance_id,
    }),
  });

export const followupAckMatches = (r, pending, cap) =>
  same(r, cap) &&
  receiptOwnerHash(r.receipt) === r.owner_hash &&
  r.owner_hash === pending.owner &&
  r.receipt.key === pending.key &&
  r.receipt.project_id === pending.projectId &&
  r.receipt.request_hash === fieldDigest(pending.body) &&
  r.receipt.second_review_task_id === pending.body.second_review_task_id &&
  r.receipt.concern_hash === pending.body.expected_concern_hash &&
  r.receipt.context_hash === pending.body.expected_context_hash &&
  r.receipt.disposition === pending.body.disposition &&
  r.receipt.reason === pending.body.reason &&
  r.receipt.related_submission_id === pending.body.related_submission_id &&
  r.receipt.target_ref_hash === pending.targetHash;

export const followupDefinitiveRejection = (r) =>
  definitiveRejection(r) ||
  (r?.$meta?.status === 409 &&
    (r?.response?.display_context?.reason ?? r?.display_context?.reason) === "SECOND_REVIEW_FOLLOWUP_EXISTS");
