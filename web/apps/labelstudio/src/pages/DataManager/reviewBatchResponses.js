import { z } from "zod";

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
});
const detail = metadata.extend({
  role: z.enum(["manager", "reviewer"]),
  plan_hash: z.string().regex(/^[0-9a-f]{64}$/),
  as_of: z.string(),
  algorithm: z.string(),
  rule: z.object({ rate: z.number().int().min(1).max(100), seed: z.string() }),
  candidate_count: count,
  selected_count: count,
  items: z.array(item).max(1000),
}).refine((r) => r.candidate_count === r.items.length
  && r.selected_count === r.items.filter((i) => i.selected).length);
const list = z.object({ results: z.object({ batches: z.array(metadata) }), next: z.string().nullable().optional() });
const capability = z.object({ enabled: z.literal(true), can_create: z.boolean() });

// Validate without coercion: malformed scalars must never become visible content
// or truthy authority. The API remains the source of authorization and evidence.
export const validBatchDetail = (value, expectedId) => detail.safeParse(value).success && value.id === expectedId;
export const validBatchList = (value) => list.safeParse(value).success;
export const validBatchCapability = (value) => capability.safeParse(value).success;
