import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { cn } from "../../utils/bem";
import { FrozenArbitrationWorkspace } from "./ClassificationArbitrations";
import { FieldResult, fieldComparisonSchema, validateFieldComparison } from "./FieldDisagreement";
import { fieldDigest } from "./fieldEvidence";
import "./FieldArbitrations.scss";

const SCHEMA = "field-arbitrations/v1";
const RULE = "adopt-exact-field-submission/v1";
const id = z.number().int().positive().safe();
const count = z.number().int().nonnegative().safe();
const hash = z.string().regex(/^[0-9a-f]{64}$/);
const state = z.enum(["pending", "resolved", "cancelled"]);
const pin = z.object({
  submission_id: id,
  assignment_id: id,
  assignee_id: id,
  task_id: id,
  revision: id,
  submitter_id: id,
  result_hash: hash,
  config_hash: hash,
  task_data_hash: hash,
});
const caps = z.object({
  enabled: z.literal(true),
  schema: z.literal(SCHEMA),
  decision_rule: z.literal(RULE),
  actor_id: id,
  viewer_hash: hash,
  can_read: z.boolean(),
  can_create: z.boolean(),
  can_decide: z.boolean(),
  can_cancel: z.boolean(),
});
const meta = z.object({
  id,
  schema: z.literal(SCHEMA),
  decision_rule: z.literal(RULE),
  field_batch_id: id,
  task_id: id,
  batch_name: z.string(),
  assignee_id: id,
  assignee_id_frozen: id,
  input_set_hash: hash,
  task_evidence_hash: hash,
  viewer_hash: hash,
  status: state,
  version: id,
  created_at: z.string(),
  calculated_at: z.string(),
  can_decide: z.boolean(),
  can_cancel: z.boolean(),
  origin_manifest_hash: hash.optional(),
});
const pagination = { count, next: z.string().nullable(), previous: z.string().nullable() };
const roster = z.object({
  ...pagination,
  field_batch_id: id,
  task_id: id,
  batch_name: z.string(),
  origin_manifest_hash: hash,
  expected_manifest_hash: hash,
  input_set_hash: hash,
  viewer_hash: hash,
  results: z.array(z.object({ id, name: z.string() })).max(100),
});
const receipt = z.object({
  schema: z.literal(SCHEMA),
  operation: z.enum(["create", "decision", "cancel"]),
  project_id: id,
  arbitration_id: id,
  principal: z.string().min(1),
  source: z.string().min(1),
  actor_id: id,
  key: z.string().uuid(),
  created_at: z.string(),
  request_hash: hash,
  payload_hash: hash,
  input_set_hash: hash,
  status_at_commit: state,
  version_at_commit: id,
});
const created = meta.extend({
  action: z.literal("create"),
  replayed: z.boolean(),
  receipt,
  current_status: state,
  current_version: id,
  origin_manifest_hash: hash,
});
const command = z.object({
  id,
  schema: z.literal(SCHEMA),
  action: z.enum(["decision", "cancel"]),
  input_set_hash: hash,
  viewer_hash: hash,
  status: state,
  version: id,
  current_status: state,
  current_version: id,
  replayed: z.boolean(),
  receipt,
  decision_id: id.optional(),
  winner_submission_id: id.optional(),
  winner_revision: id.optional(),
});
const detail = meta.extend({
  observed_at: z.string(),
  source_status: z.enum(["verified", "unavailable"]),
  reason: z.string().nullable(),
  comparison: fieldComparisonSchema.nullable(),
  inputs: z.array(pin).max(20),
  frozen_counts: z.object({
    outcome: z.literal("disagreement"),
    reason: z.null(),
    input_count: z.number().int().min(2).max(20),
    configured_fields: z.number().int().min(1).max(20),
    total_pairs: count,
    comparable_pairs: count,
    unavailable_pairs: count,
    has_value_difference: z.boolean(),
    has_format_difference: z.boolean(),
    cost_per_pass: z.object({ normalized_values: count, field_equalities: count }),
    calculation_hash: hash,
  }),
  decision: z
    .object({
      id,
      actor_id: id,
      created_at: z.string(),
      winner_submission_id: id,
      winner_revision: id,
      reason: z.string(),
      rule: z.literal(RULE),
    })
    .nullable(),
  cancellation: z.object({ actor_id: id, created_at: z.string(), reason: z.string() }).nullable(),
});
const methods = {
  classificationArbitrationCapability: "fieldArbitrationCapability",
  classificationArbitrationAssignees: "fieldArbitrationAssignees",
  classificationArbitrations: "fieldArbitrations",
  classificationArbitrationDetail: "fieldArbitrationDetail",
  createClassificationArbitration: "createFieldArbitration",
  resolveClassificationArbitration: "resolveFieldArbitration",
  cancelClassificationArbitration: "cancelFieldArbitration",
};
const schemas = {
  classificationArbitrationCapability: caps,
  classificationArbitrationAssignees: roster,
  classificationArbitrations: z.object({ ...pagination, viewer_hash: hash, results: z.array(meta).max(100) }),
  classificationArbitrationDetail: detail,
  createClassificationArbitration: created,
  resolveClassificationArbitration: command,
  cancelClassificationArbitration: command,
};
const valid = (value) => {
  if (!value) throw new Error("Invalid field arbitration scope");
};
const normalize = (v) => (v.field_batch_id ? { ...v, batch_id: v.field_batch_id } : v);
const fieldBody = (value) => {
  if (!value || !("batch_id" in value)) return value;
  const { batch_id, ...rest } = value;
  return { ...rest, field_batch_id: batch_id };
};

const Choices = ({ data, winner, onChoose }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  return data.inputs.map((source) => (
    <label key={source.submission_id} style={{ display: "block" }}>
      <input
        type="radio"
        name="arbitration-winner"
        value={source.submission_id}
        checked={winner === String(source.submission_id)}
        onChange={() => onChoose(source.submission_id)}
      />
      {t("classificationSource", { id: source.submission_id, revision: source.revision, actor: source.submitter_id })}
      {" — "}
      {t("fieldArbitrationFields", { count: data.frozen_counts.configured_fields })}
    </label>
  ));
};

const Evidence = ({ data, busy, onSelectPair }) => {
  const [showDetail, setShowDetail] = useState(true);
  const matrixRefs = useRef({});
  const heading = useRef(null);
  useEffect(() => {
    if (showDetail) heading.current?.focus();
  }, [data.id, data.comparison.selected_pair, showDetail]);
  return (
    <div className={cn("field-arbitration-evidence").toClassName()}>
      <FieldResult
        report={{
          ...data.comparison,
          rule: "field-lf-ascii-trim/v1",
          calculated_at: data.calculated_at,
          observed_at: data.observed_at,
        }}
        evidence={{ reference: data.task_evidence_hash, inputs: data.inputs }}
        busy={busy}
        detail={showDetail}
        onSelectPair={(pair) => {
          setShowDetail(true);
          onSelectPair(pair);
        }}
        onBack={() => setShowDetail(false)}
        matrixRefs={matrixRefs}
        detailHeading={heading}
      />
    </div>
  );
};

const processing = z.object({
  validation_passes: z.literal(2),
  hydration_passes: z.union([z.literal(0), z.literal(2)]),
  calculation_passes: z.union([z.literal(0), z.literal(2)]),
  per_pass: z.object({ normalized_values: count.max(400), field_equalities: count.max(7600) }),
  total: z.object({ normalized_values: count.max(800), field_equalities: count.max(15200) }),
  scorer_calls: z.literal(0),
});
const checkedCost = (raw) => {
  const c = processing.parse(raw);
  valid(
    c.total.normalized_values === 2 * c.per_pass.normalized_values &&
      c.total.field_equalities === 2 * c.per_pass.field_equalities,
  );
  return c;
};

export const fieldArbitrationAdapter = {
  testId: "field-arbitrations",
  handoff: null,
  maskUnavailable: (detail) => Object.assign(detail, { comparison: null, inputs: [], source_status: "unavailable" }),
  messages: {
    arbitrationTitle: "fieldArbitrationTitle",
    arbitrationBoundary: "fieldArbitrationBoundary",
    arbitrationDispatch: "fieldArbitrationDispatch",
    arbitrationAdopt: "fieldArbitrationAdopt",
    arbitrationStartHint: "fieldArbitrationStartHint",
  },
  rejected: Object.fromEntries(
    [
      ...["INVALID_INPUT", "INVALID_KEY"].map((k) => [k, 400]),
      ...[
        "ORIGIN_CHANGED",
        "INELIGIBLE",
        "ASSIGNEE_UNAVAILABLE",
        "DUPLICATE_ACTIVE",
        "SOURCE_UNAVAILABLE",
        "SOURCE_CHANGED",
        "CAS_CONFLICT",
        "INPUT_SET_CHANGED",
        "TERMINAL",
        "KEY_CONFLICT",
      ].map((k) => [k, 409]),
      ["BUDGET_EXCEEDED", 422],
    ].map(([k, v]) => [`FIELD_ARBITRATION_${k}`, v]),
  ),
  request: (method, params, body) => ({ method: methods[method], params: fieldBody(params), body: fieldBody(body) }),
  parse: (method, raw, expected) => {
    valid(schemas[method]);
    const cost = checkedCost(raw.processing_cost);
    const v = schemas[method].parse(raw);
    v.processing_cost = cost;
    const sourceFree =
      [
        "classificationArbitrationCapability",
        "classificationArbitrationAssignees",
        "classificationArbitrations",
      ].includes(method) ||
      v.replayed ||
      v.action === "cancel";
    if (sourceFree)
      valid(
        cost.hydration_passes === 0 &&
          cost.calculation_passes === 0 &&
          cost.total.normalized_values === 0 &&
          cost.total.field_equalities === 0,
      );
    else if (v.action === "create" || v.action === "decision")
      valid(
        cost.hydration_passes === 2 &&
          cost.calculation_passes === 2 &&
          cost.per_pass.normalized_values >= 2 &&
          cost.per_pass.field_equalities >= 2,
      );
    const check = (row) => {
      valid(row.assignee_id === row.assignee_id_frozen && row.version === (row.status === "pending" ? 1 : 2));
      valid(row.status === "pending" || (!row.can_decide && !row.can_cancel));
    };
    if (v.schema === SCHEMA && v.task_id) check(v);
    if (method === "classificationArbitrations") {
      v.results.forEach((row) => {
        check(row);
        valid(row.viewer_hash === v.viewer_hash);
      });
      return { ...v, results: v.results.map(normalize) };
    }
    if (method === "classificationArbitrationDetail") {
      valid(v.id === expected.arbitration);
      valid(
        (v.status === "resolved") === Boolean(v.decision) && (v.status === "cancelled") === Boolean(v.cancellation),
      );
      if (v.decision) valid(v.decision.actor_id === v.assignee_id_frozen);
      const ids = v.inputs.map((p) => p.submission_id);
      const n = v.frozen_counts.input_count;
      valid(n >= 2 && n <= 20 && v.frozen_counts.total_pairs === (n * (n - 1)) / 2);
      if (v.source_status === "unavailable")
        valid(!v.comparison && !v.inputs.length && !v.can_decide && v.reason === "source_unavailable");
      else {
        valid(
          v.comparison &&
            ids.length === n &&
            v.reason === null &&
            v.inputs.every((p) => p.task_id === v.task_id && p.assignee_id === p.submitter_id) &&
            ids.every((id, i) => !i || ids[i - 1] < id),
        );
        validateFieldComparison(v.comparison, {
          ids,
          pair: expected.left ? [expected.left, expected.right].sort((a, b) => a - b) : ids.slice(0, 2),
        });
        const f = v.frozen_counts;
        const c = v.comparison;
        const cohort = v.inputs
          .map((p) => ({ assignment_id: p.assignment_id, assignee_id: p.assignee_id, source: p }))
          .sort((a, b) => a.assignment_id - b.assignment_id);
        valid(
          new Set(cohort.map((e) => e.assignment_id)).size === n &&
            new Set(v.inputs.map((p) => p.submitter_id)).size === n &&
            fieldDigest({ schema: SCHEMA, item: { task_id: v.task_id, cohort, selection_reason: null, result: f } }) ===
              v.task_evidence_hash,
        );
        valid(
          c.availability === "available" &&
            c.comparable_pairs === f.comparable_pairs &&
            c.unavailable_pairs === f.unavailable_pairs &&
            c.configured_fields === f.configured_fields &&
            c.pairs.some((p) => p.denominator > 0 && p.raw_matches < p.denominator),
        );
        valid(
          f.has_value_difference === c.pairs.some((p) => p.denominator > p.normalized_matches) &&
            f.has_format_difference === c.pairs.some((p) => p.normalized_matches > p.raw_matches),
        );
        valid(
          fieldDigest(f.cost_per_pass) === fieldDigest(c.cost_per_pass) &&
            fieldDigest(cost.per_pass) === fieldDigest(c.cost_per_pass) &&
            cost.calculation_passes === 2 &&
            cost.hydration_passes === 2,
        );
        if (v.decision)
          valid(
            v.inputs.some(
              (p) => p.submission_id === v.decision.winner_submission_id && p.revision === v.decision.winner_revision,
            ),
          );
      }
      return {
        ...normalize(v),
        available: v.source_status === "verified",
        texts: [],
        report: v.comparison ? { inputs: v.inputs } : null,
      };
    }
    return normalize(v);
  },
  validateAcknowledgement: (result, intent) => {
    const r = result.receipt;
    valid(
      r.schema === SCHEMA &&
        result.schema === SCHEMA &&
        result.action === intent.action &&
        result.viewer_hash === intent.viewerHash &&
        r.arbitration_id === result.id &&
        r.project_id === intent.projectId &&
        r.actor_id === intent.actorId &&
        r.input_set_hash === intent.inputHash &&
        result.input_set_hash === intent.inputHash &&
        result.current_status === result.status &&
        result.current_version === result.version,
    );
    const { payload_hash, ...payload } = r;
    valid(
      payload_hash === fieldDigest(payload) &&
        r.request_hash ===
          fieldDigest({
            schema: SCHEMA,
            action: intent.action,
            project_id: intent.projectId,
            id: intent.identifier ?? null,
            body: fieldBody(intent.body),
          }),
    );
    if (intent.action === "decision") valid(result.winner_revision === intent.winnerRevision);
  },
  canAdopt: (data, winner) =>
    data.comparison?.selected_pair &&
    [data.comparison.selected_pair.left, data.comparison.selected_pair.right].includes(winner),
  pairForWinner: (data, winner) =>
    [data.comparison.selected_pair?.left, data.comparison.selected_pair?.right].includes(winner)
      ? null
      : [data.inputs.find((p) => p.submission_id !== winner).submission_id, winner].sort((a, b) => a - b),
  renderChoices: (props) => <Choices {...props} />,
  renderEvidence: (props) => <Evidence {...props} />,
};

export const FieldArbitrationTitle = () => {
  const { t } = useLocaleTranslation("reviewBatches");
  return t("fieldArbitrationTitle");
};
export const FieldArbitrationEntry = ({ projectId, onOpen, disabled, dispatch = false }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const api = useAPI();
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let generation = 0;
    let disposed = false;
    const read = async () => {
      const current = ++generation;
      setEnabled(false);
      try {
        const raw = await call.current("fieldArbitrationCapability", {
          params: { pk: projectId },
          errorFilter: () => true,
        });
        const result = caps.safeParse(raw);
        if (!disposed && current === generation)
          setEnabled(
            Boolean(
              onOpen &&
                !raw?.error &&
                raw?.$meta?.ok !== false &&
                typeof raw?.status !== "number" &&
                result.success &&
                result.data[dispatch ? "can_create" : "can_read"],
            ),
          );
      } catch {
        /* unavailable */
      }
    };
    const blur = () => {
      generation++;
      setEnabled(false);
    };
    read();
    window.addEventListener("focus", read);
    window.addEventListener("blur", blur);
    return () => {
      disposed = true;
      generation++;
      window.removeEventListener("focus", read);
      window.removeEventListener("blur", blur);
    };
  }, [projectId, dispatch, Boolean(onOpen)]);
  return enabled ? (
    <Button
      disabled={disabled}
      onClick={onOpen}
      data-testid={dispatch ? "dispatch-field-arbitration" : "open-field-arbitrations"}
    >
      {t(dispatch ? "fieldArbitrationDispatch" : "fieldArbitrationTitle")}
    </Button>
  ) : null;
};
export const FieldArbitrations = ({ initialOrigin, ...props }) => {
  const adapter = useRef(null);
  if (!adapter.current) {
    let scope = null;
    const retained = new Map();
    adapter.current = {
      ...fieldArbitrationAdapter,
      retainDetail: (detail, projectId) => {
        const next = JSON.stringify([projectId, detail.viewer_hash]);
        if (scope !== next) {
          scope = next;
          retained.clear();
        }
        const frozen = fieldDigest({
          input: detail.input_set_hash,
          evidence: detail.task_evidence_hash,
          batch: detail.field_batch_id,
          task: detail.task_id,
          calculated_at: detail.calculated_at,
          counts: detail.frozen_counts,
        });
        valid(!retained.has(detail.id) || retained.get(detail.id) === frozen);
        retained.set(detail.id, frozen);
      },
    };
  }
  return (
    <FrozenArbitrationWorkspace
      {...props}
      initialOrigin={
        initialOrigin
          ? {
              batch_id: initialOrigin.field_batch_id,
              task_id: initialOrigin.task_id,
              expected_manifest_hash: initialOrigin.expected_manifest_hash,
            }
          : null
      }
      adapter={adapter.current}
    />
  );
};
