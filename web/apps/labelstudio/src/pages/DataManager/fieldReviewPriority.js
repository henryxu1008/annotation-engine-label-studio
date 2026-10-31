import { z } from "zod";
import { fieldDigest } from "./fieldEvidence";

const id = z.number().int().positive().safe();
const count = z.number().int().nonnegative().safe();
const hash = z.string().regex(/^[0-9a-f]{64}$/);
const identity = z.object({
  submission_id: id,
  assignment_id: id,
  task_id: id,
  revision: id,
  submitter_id: id,
  result_hash: hash,
});
const pin = identity.extend({ assignee_id: id, config_hash: hash, task_data_hash: hash }).strict();
export const fieldPriorityVersions = {
  source_schema: "field-batches/v1",
  metric_rule: "field-lf-ascii-trim/v1",
  selection: "active-assignment-latest/v1",
  explanation: "field-value-differences/v1",
  engine: "python-string-equality/v1",
  queue_rule: "field-raw-difference-queue/v1",
};
const versions = Object.fromEntries(
  Object.entries(fieldPriorityVersions).map(([key, value]) => [key, z.literal(value)]),
);
const source = z.object({
  schema: z.literal("review-field-priority/v1"),
  kind: z.literal("field_conflict"),
  organization_id: id,
  batch_id: id,
  manifest_hash: hash,
  ...versions,
});
const planSchema = z
  .object({
    algorithm: z.literal("field-conflict-ranked/v1"),
    project_id: id,
    rule: z
      .object({
        rate: z.number().int().min(1).max(100),
        per_submitter: z.boolean(),
        seed: z.string().min(1).max(128),
        package_ids: z.array(id).max(100),
        submitter_ids: z.array(id).max(100),
      })
      .strict(),
    candidates: z
      .array(identity.extend({ selected: z.boolean() }).strict())
      .min(1)
      .max(1000),
    priority: source
      .extend({
        creator: z
          .object({ principal: z.string().min(1).max(512), source: z.string().min(1).max(128), actor_id: id })
          .strict(),
        participants: z
          .array(z.object({ task_id: id, actor_ids: z.array(id).max(40) }).strict())
          .min(1)
          .max(100),
        evidence: z
          .array(
            z
              .object({
                submission_id: id,
                state: z.enum(["conflict", "agreement", "unknown"]),
                reason: z.enum([
                  "frozen_conflict",
                  "frozen_agreement",
                  "not_covered",
                  "revision_mismatch",
                  "metric_unavailable",
                ]),
                calculation_hash: hash.nullable(),
                source_pin: pin.nullable(),
              })
              .strict(),
          )
          .max(1000),
      })
      .strict(),
  })
  .strict();
const counters = z.object({ normalized_values: count.max(400), field_equalities: count.max(7600) }).strict();
const costs = z
  .object({
    hydration_passes: z.union([z.literal(0), z.literal(2)]),
    calculation_passes: z.union([z.literal(0), z.literal(2)]),
    per_pass: counters,
    total: z.object({ normalized_values: count.max(800), field_equalities: count.max(15200) }).strict(),
    scorer_calls: z.literal(0),
  })
  .strict();
const ordered = (values) => values.every((v, i) => i === 0 || values[i - 1] < v);

export const validFieldCost = (value, replayed) =>
  costs.safeParse(value).success &&
  value.hydration_passes === (replayed ? 0 : 2) &&
  value.calculation_passes === (replayed ? 0 : 2) &&
  value.per_pass.field_equalities % 2 === 0 &&
  (value.per_pass.normalized_values === 0
    ? value.per_pass.field_equalities === 0
    : value.per_pass.normalized_values >= 2 &&
      value.per_pass.field_equalities >= value.per_pass.normalized_values &&
      value.per_pass.field_equalities <= 19 * value.per_pass.normalized_values) &&
  (replayed ? Object.values(value.per_pass).every((n) => n === 0) : true) &&
  value.total.normalized_values === 2 * value.per_pass.normalized_values &&
  value.total.field_equalities === 2 * value.per_pass.field_equalities;

export const validFieldPreview = (result, choice, projectId) => {
  const plan = result?.plan;
  if (
    !planSchema.safeParse(plan).success ||
    result.project_id !== plan.project_id ||
    (projectId !== undefined && plan.project_id !== projectId) ||
    !hash.safeParse(result.viewer_hash).success ||
    fieldDigest(plan) !== result.plan_hash ||
    !validFieldCost(result.processing_cost, false)
  )
    return false;
  const p = plan.priority;
  const candidates = plan.candidates;
  const evidence = p.evidence;
  if (
    p.batch_id !== choice.batch_id ||
    p.manifest_hash !== choice.manifest_hash ||
    !ordered(candidates.map((c) => c.submission_id)) ||
    evidence.length !== candidates.length ||
    !ordered(p.participants.map((t) => t.task_id)) ||
    !p.participants.every((t) => ordered(t.actor_ids)) ||
    !ordered(plan.rule.package_ids) ||
    !ordered(plan.rule.submitter_ids)
  )
    return false;
  const people = new Map(p.participants.map((t) => [t.task_id, t.actor_ids]));
  for (let i = 0; i < candidates.length; i++) {
    const c = candidates[i];
    const e = evidence[i];
    if (e.submission_id !== c.submission_id) return false;
    if (["not_covered", "revision_mismatch"].includes(e.reason)) {
      if (
        e.state !== "unknown" ||
        e.source_pin !== null ||
        e.calculation_hash !== null ||
        (e.reason === "not_covered") !== !people.has(c.task_id)
      )
        return false;
    } else {
      const expected = { frozen_conflict: "conflict", frozen_agreement: "agreement", metric_unavailable: "unknown" }[
        e.reason
      ];
      const pinned = e.source_pin;
      if (
        e.state !== expected ||
        !pinned ||
        !hash.safeParse(e.calculation_hash).success ||
        !Object.keys(identity.shape).every((k) => c[k] === pinned[k]) ||
        pinned.assignee_id !== pinned.submitter_id ||
        !people.get(c.task_id)?.includes(pinned.assignee_id)
      )
        return false;
    }
  }
  const groups = new Map();
  for (const c of candidates) {
    const key = plan.rule.per_submitter ? c.submitter_id : "all";
    groups.set(key, [...(groups.get(key) ?? []), c]);
  }
  const states = new Map(evidence.map((e) => [e.submission_id, e.state]));
  const chosen = new Set();
  for (const group of groups.values()) {
    const ranked = group
      .map((c) => {
        const sourceIdentity = identity.parse(c);
        return {
          c,
          first: states.get(c.submission_id) === "conflict" ? 0 : 1,
          rank: fieldDigest([plan.algorithm, plan.project_id, plan.rule.seed, sourceIdentity]),
        };
      })
      .sort(
        (a, b) =>
          a.first - b.first ||
          (a.rank < b.rank ? -1 : a.rank > b.rank ? 1 : 0) ||
          a.c.submission_id - b.c.submission_id,
      );
    for (const row of ranked.slice(0, Math.ceil((group.length * plan.rule.rate) / 100)))
      chosen.add(row.c.submission_id);
  }
  return (
    candidates.every((c) => c.selected === chosen.has(c.submission_id)) &&
    (!evidence.some((e) => e.state !== "unknown") || result.processing_cost.per_pass.normalized_values >= 2)
  );
};

export const validFieldAck = (result, intent, projectId) =>
  result?.project_id === projectId &&
  typeof result.replayed === "boolean" &&
  validFieldCost(result.processing_cost, result.replayed) &&
  (result.replayed || fieldDigest(result.processing_cost) === fieldDigest(intent.processingCost));

export const validFieldDetail = (result) =>
  hash.safeParse(result.viewer_hash).success &&
  result.items.every((i) => typeof i.priority_reason === "string") &&
  (result.role === "reviewer"
    ? result.priority_source === undefined
    : source.safeParse(result.priority_source).success);
