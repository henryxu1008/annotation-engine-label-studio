import { z } from "zod";
import { validFieldDetail } from "./fieldReviewPriority";
import { confidenceKind, confidenceMarker, confidenceSchema, validConfidenceCapability, validConfidenceDetail } from "./confidenceReviewPriority";

const id = z.number().int().positive().safe();
const count = z.number().int().nonnegative().safe();
const metadata = z.object({ id, name: z.string() });
const item = z.object({
  submission_id: id,
  revision: id,
  selected: z.boolean(),
  status: z.enum(["pending", "approved", "rejected", "superseded", "unavailable"]),
  can_review: z.boolean(),
  review_reason: z.string().nullish(),
  successor: z.object({ id, revision: id, status: z.string().nullish() }).nullish(),
  priority_reason: z.enum(["frozen_conflict", "frozen_agreement", "not_covered", "revision_mismatch", "metric_unavailable",
    "frozen_low_confidence", "frozen_not_low_confidence", "unsupported_input", "revision_binding_changed", "source_unavailable"]).optional(),
});
const detail = metadata.extend({
  viewer_hash: z.string().regex(/^[0-9a-f]{64}$/).optional(),
  role: z.enum(["manager", "reviewer"]),
  plan_hash: z.string().regex(/^[0-9a-f]{64}$/),
  as_of: z.string(),
  algorithm: z.string(),
  rule: z.object({ rate: z.number().int().min(1).max(100), seed: z.string() }),
  candidate_count: count,
  selected_count: count,
  items: z.array(item).max(1000),
  priority_summary: z.union([z.object({ conflict_candidates: count, selected_conflicts: count, unknown_candidates: count })
    .refine((s) => s.selected_conflicts <= s.conflict_candidates), z.object({ kind: z.literal("revision_confidence") })]).optional(),
  priority_source: z.union([z.object({ batch_id: id, manifest_hash: z.string().regex(/^[0-9a-f]{64}$/) }),
    z.object({ kind: z.literal("revision_confidence") })]).optional(),
}).refine((r) => r.candidate_count === r.items.length
  && r.selected_count === r.items.filter((i) => i.selected).length);
const list = z.object({ results: z.object({ batches: z.array(metadata) }), next: z.string().nullable().optional() });
const capability = z.object({ enabled: z.literal(true), can_create: z.boolean(), can_prioritize: z.boolean().optional(),
  can_read_priority: z.boolean().optional(),
  can_prioritize_field: z.boolean().optional(), can_read_field_priority: z.boolean().optional(),
  can_prioritize_ner: z.boolean().optional(), can_read_ner_priority: z.boolean().optional(),
  can_read_handoffs: z.boolean().optional(), can_handoff: z.boolean().optional(),
  viewer_hash: z.string().regex(/^[0-9a-f]{64}$/).optional() });

// Validate without coercion: malformed scalars must never become visible content
// or truthy authority. The API remains the source of authorization and evidence.
export const validBatchDetail = (value, expectedId) => detail.safeParse(value).success && value.id === expectedId
  && (!(confidenceMarker(value) || confidenceMarker(value.priority_source) || confidenceMarker(value.priority_summary)) || validConfidenceDetail(value))
  && (value.algorithm !== "field-conflict-ranked/v1" || validFieldDetail(value));
export const validBatchList = (value, projectId, capability) => list.safeParse(value).success
  && (!confidenceMarker(value) || (value.schema === confidenceSchema && value.kind === confidenceKind
    && value.project_id === projectId && capability?.can_read_confidence_priority === true
    && typeof value.viewer_hash === "string" && /^[0-9a-f]{64}$/.test(value.viewer_hash) && value.viewer_hash === capability.viewer_hash));
export const validBatchCapability = (value) => capability.safeParse(value).success && validConfidenceCapability(value);
