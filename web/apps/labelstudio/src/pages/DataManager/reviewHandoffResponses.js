import { z } from "zod";

const id = z.number().int().positive().safe(), hash = z.string().regex(/^[0-9a-f]{64}$/);
const schema = z.literal("review-arbitration-handoffs/v1"), mode = z.enum(["dispatch", "link_existing"]);
const state = z.enum(["pending", "resolved", "cancelled"]);
export const handoffContextSchema = z.object({ schema, viewer_hash: hash, review_batch_id: id, submission_id: id,
  revision: id, expected_plan_hash: hash, expected_input_set_hash: hash, can_handoff: z.boolean(),
  dispatch_origin: z.object({ batch_id: id, task_id: id, expected_manifest_hash: hash }),
  existing_arbitration: z.object({ id, input_set_hash: hash, viewer_hash: hash, status: state, version: id }).passthrough().nullable(),
});
export const handoffDetailSchema = z.object({ schema, id, review_batch_id: id, submission_id: id, revision: id,
  arbitration_id: id, mode, created_at: z.string(), actor_id: id, viewer_hash: hash, available: z.boolean(),
  reason: z.string().nullable(), source_status: z.enum(["pending", "approved", "rejected", "superseded", "unavailable"]),
  arbitration_status: state.nullable(), arbitration_version: id.nullable(),
  target: z.object({ id, input_set_hash: hash, created_at: z.string(), assignee_id: id, batch_id: id }),
});
export const handoffListSchema = z.object({ schema, viewer_hash: hash, review_batch_id: id, submission_id: id,
  count: z.number().int().nonnegative().safe(), next: z.string().nullable(), previous: z.string().nullable(),
  results: z.array(handoffDetailSchema).max(100),
});
export const handoffReceiptSchema = z.object({ schema, id, review_batch_id: id, submission_id: id, arbitration_id: id,
  input_set_hash: hash, viewer_hash: hash, replayed: z.boolean(),
  receipt: z.object({ operation: z.literal("handoff"), key: z.string().uuid(), mode, plan_hash: hash,
    arbitration_version_at_commit: id, arbitration_status_at_commit: state }),
});
export const handoffScopeMatches = (row, source) => row.review_batch_id === source.batch && row.submission_id === source.source;
