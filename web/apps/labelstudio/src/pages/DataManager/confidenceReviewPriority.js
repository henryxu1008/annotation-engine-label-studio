import { z } from "zod";
import { fieldDigest } from "./fieldEvidence";

export const confidenceKind = "revision_confidence";
export const confidenceAlgorithm = "revision-confidence-ranked/v1";
export const confidenceSchema = "review-confidence-priority/v1";
// DRF CharField uses Python str.strip(), whose whitespace set differs from JS trim().
export const confidenceText = (value) =>
  value.replace(
    // biome-ignore lint/suspicious/noControlCharactersInRegex: Match Python str.strip whitespace, including C0 separators.
    /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g,
    "",
  );
const hash = z.string().regex(/^[0-9a-f]{64}$/);
const uuid = z.string().regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
const id = z.number().int().positive().safe();
const count = z.number().int().nonnegative().safe();
const units = count.max(1000000);
const text = z.string().min(1).max(512);
const profile = z
  .object({
    schema: z.literal("review-confidence-profile/v1"),
    profile_id: uuid,
    label: text,
    version: text,
    evaluator_id: text,
    evaluator_version: text,
    model_id: text,
    model_artifact_sha256: hash,
    score_definition: z.literal("revision-confidence-lower-uncertain/v1"),
    calibration_version: text,
    integer_rule: z.literal("millionths-int/v1"),
    input_contract: z.literal("static-text-choices-submission/v1"),
  })
  .strict();
const discovery = profile.extend({ digest: hash });
export const validConfidenceProfiles = (values) =>
  z.array(discovery).max(20).safeParse(values).success &&
  new Set(values.map((p) => `${p.profile_id}:${p.digest}`)).size === values.length;
export const isConfidence = (value) => value?.kind === confidenceKind;
export const confidenceMarker = (value) =>
  isConfidence(value) ||
  value?.algorithm?.startsWith("revision-confidence-ranked/") ||
  value?.schema?.startsWith("review-confidence-priority/");
export const validConfidenceChoice = (value) =>
  z
    .object({ kind: z.literal(confidenceKind), profile_id: uuid, profile_digest: hash, threshold_units: units })
    .strict()
    .safeParse(value).success;
export const validConfidenceCapability = (v) => {
  const keys = ["can_read_confidence_priority", "can_prioritize_confidence", "can_recover_confidence"];
  if (keys.some((k) => v[k] !== undefined && typeof v[k] !== "boolean")) return false;
  if (v.confidence_owner_hash !== undefined && !hash.safeParse(v.confidence_owner_hash).success) return false;
  if (v.confidence_profiles !== undefined && !validConfidenceProfiles(v.confidence_profiles)) return false;
  if (
    v.can_recover_confidence === true &&
    (v.can_create !== true ||
      v.can_read_confidence_priority !== true ||
      !hash.safeParse(v.confidence_owner_hash).success ||
      !Array.isArray(v.confidence_profiles))
  )
    return false;
  return (
    v.can_prioritize_confidence !== true || (v.can_recover_confidence === true && v.confidence_profiles.length > 0)
  );
};
const summary = z
  .object({
    kind: z.literal(confidenceKind),
    known_candidates: count,
    unknown_candidates: count,
    low_confidence_candidates: count,
    selected_low_confidence: count,
    uncovered_low_confidence: count,
    fallback_selected: count,
  })
  .strict()
  .refine(
    (s) =>
      s.low_confidence_candidates <= s.known_candidates &&
      s.selected_low_confidence + s.uncovered_low_confidence === s.low_confidence_candidates,
  );
const source = z
  .object({
    schema: z.literal(confidenceSchema),
    kind: z.literal(confidenceKind),
    profile,
    profile_digest: hash,
    threshold_units: units,
  })
  .strict();
const costFields = {
  snapshot_stored_bytes: count.max(64 * 1024 ** 2),
  snapshot_canonical_bytes: count.max(8 * 1024 ** 2),
  metadata_stored_bytes: count.max(16 * 1024 ** 2),
  metadata_canonical_bytes: count.max(4 * 1024 ** 2),
  candidate_count: count.max(1000),
  fact_count: count.max(1000),
};
export const validConfidenceCost = (v, replayed = false) => {
  if (
    !v ||
    v.materialization_passes !== (replayed ? 0 : 3) ||
    v.sorting_passes !== (replayed ? 0 : 2) ||
    v.scorer_calls !== 0
  )
    return false;
  if (replayed)
    return (
      Object.keys(v.per_pass ?? { missing: true }).length === 0 &&
      Object.keys(v.total ?? { missing: true }).length === 0
    );
  return (
    z.object(costFields).strict().safeParse(v.per_pass).success &&
    v.total &&
    Object.keys(costFields).every((k) => v.total[k] === v.per_pass[k] * 3) &&
    Object.keys(v.total).length === 6
  );
};
const identity = z.object({
  submission_id: id,
  assignment_id: id,
  task_id: id,
  revision: id,
  submitter_id: id.nullable(),
  result_hash: hash,
});
const candidate = identity.extend({ selected: z.boolean() }).strict();
const unknownReasons = ["not_covered", "unsupported_input", "revision_binding_changed", "source_unavailable"];
const evidence = z
  .object({
    submission_id: id,
    state: z.enum(["low_confidence", "not_low_confidence", "unknown"]),
    reason: z.string(),
    score_units: units.nullable(),
    fact_id: uuid.nullable(),
    fact_hash: hash.nullable(),
  })
  .strict();
const envelope = (r, projectId) =>
  r?.schema === confidenceSchema &&
  r.kind === confidenceKind &&
  r.algorithm === confidenceAlgorithm &&
  r.project_id === projectId &&
  id.safeParse(r.actor_id).success &&
  hash.safeParse(r.owner_hash).success &&
  hash.safeParse(r.viewer_hash).success;
export const confidenceSummary = (candidates, entries) => {
  const states = new Map(entries.map((e) => [e.submission_id, e.state]));
  const low = candidates.filter((c) => states.get(c.submission_id) === "low_confidence");
  const selected = low.filter((c) => c.selected).length;
  return {
    kind: confidenceKind,
    known_candidates: candidates.filter((c) => states.get(c.submission_id) !== "unknown").length,
    unknown_candidates: candidates.filter((c) => states.get(c.submission_id) === "unknown").length,
    low_confidence_candidates: low.length,
    selected_low_confidence: selected,
    uncovered_low_confidence: low.length - selected,
    fallback_selected: candidates.filter((c) => c.selected).length - selected,
  };
};
export const validConfidencePreview = (r, choice, projectId) => {
  if (
    !validConfidenceChoice(choice) ||
    !envelope(r, projectId) ||
    !hash.safeParse(r.plan_hash).success ||
    !validConfidenceCost(r.processing_cost)
  )
    return false;
  const plan = r.plan;
  const p = plan?.priority;
  if (
    plan?.algorithm !== confidenceAlgorithm ||
    plan.project_id !== projectId ||
    !source.safeParse(p && Object.fromEntries(Object.entries(p).filter(([k]) => !["summary", "evidence"].includes(k))))
      .success ||
    p.profile.profile_id !== choice.profile_id ||
    p.profile_digest !== choice.profile_digest ||
    p.threshold_units !== choice.threshold_units ||
    !summary.safeParse(p.summary).success ||
    !z.array(candidate).min(1).max(1000).safeParse(plan.candidates).success ||
    !z.array(evidence).max(1000).safeParse(p.evidence).success ||
    p.evidence.length !== plan.candidates.length ||
    !z
      .object({
        rate: count.min(1).max(100),
        per_submitter: z.boolean(),
        seed: z
          .string()
          .min(1)
          .refine((value) => Array.from(value).length <= 128),
        package_ids: z.array(id).max(100),
        submitter_ids: z.array(id).max(100),
      })
      .strict()
      .safeParse(plan.rule).success
  )
    return false;
  const groups = new Map();
  for (const [i, c] of plan.candidates.entries()) {
    const e = p.evidence[i];
    if (e.submission_id !== c.submission_id || (i > 0 && plan.candidates[i - 1].submission_id >= c.submission_id))
      return false;
    if (e.state === "unknown") {
      if (!unknownReasons.includes(e.reason) || e.score_units !== null || e.fact_id !== null || e.fact_hash !== null)
        return false;
    } else if (
      e.score_units === null ||
      e.fact_id === null ||
      e.fact_hash === null ||
      e.state !== (e.score_units < choice.threshold_units ? "low_confidence" : "not_low_confidence") ||
      e.reason !== `frozen_${e.state}`
    )
      return false;
    const key = plan.rule.per_submitter ? c.submitter_id : "all";
    const low = e.state === "low_confidence";
    const rank = [
      low ? 0 : 1,
      low ? e.score_units : 0,
      fieldDigest([confidenceAlgorithm, projectId, plan.rule.seed, identity.parse(c)]),
      c.submission_id,
    ];
    groups.set(key, [...(groups.get(key) ?? []), { c, rank }]);
  }
  const selected = new Set();
  for (const group of groups.values()) {
    group.sort((a, b) => {
      for (let i = 0; i < 4; i++) {
        if (a.rank[i] < b.rank[i]) return -1;
        if (a.rank[i] > b.rank[i]) return 1;
      }
      return 0;
    });
    group.slice(0, Math.ceil((group.length * plan.rule.rate) / 100)).forEach(({ c }) => selected.add(c.submission_id));
  }
  return (
    plan.candidates.every((c) => c.selected === selected.has(c.submission_id)) &&
    fieldDigest(p.summary) === fieldDigest(confidenceSummary(plan.candidates, p.evidence)) &&
    r.processing_cost.per_pass.candidate_count === plan.candidates.length
  );
};
export const validConfidenceAck = (r, intent, projectId) =>
  envelope(r, projectId) &&
  r.actor_id === intent.actorId &&
  r.owner_hash === intent.ownerHash &&
  r.viewer_hash === intent.viewerHash &&
  r.idempotency_key === intent.key &&
  r.plan_hash === intent.body.expected_plan_hash &&
  r.request_hash === fieldDigest(intent.body) &&
  typeof r.replayed === "boolean" &&
  validConfidenceCost(r.processing_cost, r.replayed) &&
  (r.replayed || fieldDigest(r.processing_cost) === fieldDigest(intent.processingCost));
export const validConfidenceDetail = (r) =>
  r.algorithm === confidenceAlgorithm &&
  hash.safeParse(r.viewer_hash).success &&
  summary.safeParse(r.priority_summary).success &&
  r.priority_summary.known_candidates + r.priority_summary.unknown_candidates === r.items.length &&
  r.items.every((i) =>
    ["frozen_low_confidence", "frozen_not_low_confidence", ...unknownReasons].includes(i.priority_reason),
  ) &&
  (r.role === "reviewer"
    ? r.priority_source === undefined && r.items.every((i) => i.confidence_score_units === undefined)
    : source.safeParse(r.priority_source).success &&
      r.items.every(
        (i) =>
          units.nullable().safeParse(i.confidence_score_units).success &&
          (unknownReasons.includes(i.priority_reason)
            ? i.confidence_score_units === null
            : i.confidence_score_units !== null &&
              i.confidence_score_units < r.priority_source.threshold_units ===
                (i.priority_reason === "frozen_low_confidence")),
      )) &&
  fieldDigest(r.priority_summary) ===
    fieldDigest(
      confidenceSummary(
        r.items,
        r.items.map((i) => ({
          submission_id: i.submission_id,
          state:
            i.priority_reason === "frozen_low_confidence"
              ? "low_confidence"
              : i.priority_reason === "frozen_not_low_confidence"
                ? "not_low_confidence"
                : "unknown",
        })),
      ),
    );
