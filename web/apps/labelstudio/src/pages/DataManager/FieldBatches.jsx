import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { FrozenBatchWorkspace } from "./ClassificationBatches";
import { FieldResult, fieldComparisonSchema, validateFieldComparison } from "./FieldDisagreement";
import { fieldDigest } from "./fieldEvidence";

const id = z.number().int().positive().safe();
const count = z.number().int().nonnegative().safe();
const hash = z.string().regex(/^[0-9a-f]{64}$/);
const state = z.enum(["agreement", "disagreement", "unavailable"]);
const cost = z.object({ normalized_values: count.max(400), field_equalities: count.max(7600) }).strict();
const processing = z
  .object({
    validation_passes: z.literal(2),
    per_pass: cost,
    total: z.object({ normalized_values: count.max(800), field_equalities: count.max(15200) }).strict(),
    scorer_calls: z.literal(0),
  })
  .refine((v) => Object.keys(v.per_pass).every((k) => v.total[k] === 2 * v.per_pass[k]));
const counts = z.object({ agreement: count, disagreement: count, unavailable: count });
const versions = {
  schema: z.literal("field-batches/v1"),
  rule: z.literal("field-lf-ascii-trim/v1"),
  explanation: z.literal("field-value-differences/v1"),
  engine: z.literal("python-string-equality/v1"),
  selection: z.literal("active-assignment-latest/v1"),
  queue_rule: z.literal("field-raw-difference-queue/v1"),
};
const sourcePin = z
  .object({
    submission_id: id,
    assignment_id: id,
    assignee_id: id,
    task_id: id,
    revision: id,
    submitter_id: id,
    result_hash: hash,
    config_hash: hash,
    task_data_hash: hash,
  })
  .strict();
const cohort = z.array(z.object({ assignment_id: id, assignee_id: id, source: sourcePin.nullable() }).strict()).max(20);
const frozenResult = z
  .object({
    outcome: state,
    reason: z.string().nullable(),
    input_count: count.max(20),
    configured_fields: id.max(20).nullable(),
    total_pairs: count.max(190),
    comparable_pairs: count.max(190),
    unavailable_pairs: count.max(190),
    has_value_difference: z.boolean(),
    has_format_difference: z.boolean(),
    cost_per_pass: cost,
    calculation_hash: hash,
  })
  .strict();
const evidence = z
  .object({
    schema: z.literal("field-batch-item-evidence/v1"),
    project_id: id,
    batch_id: id,
    task_id: id,
    viewer: hash,
    reference: hash,
    rule: versions.rule,
    explanation: versions.explanation,
    engine: versions.engine,
    queue_rule: versions.queue_rule,
    calculated_at: z.string(),
    cohort,
    result: frozenResult,
  })
  .strict();
const meta = z.object({
  ...versions,
  id,
  project_id: id,
  name: z.string(),
  viewer: hash,
  visible_task_count: count.max(100),
  plan_cost_per_pass: cost,
  visibility: z.enum(["project", "visible_subset"]),
  manifest_hash: hash.optional(),
  projection_hash: hash.optional(),
  created_at: z.string(),
  calculated_at: z.string(),
  scope_enumerated_at: z.string(),
});
const cap = z.object({
  ...versions,
  enabled: z.literal(true),
  can_create: z.boolean(),
  can_view_quality_overview: z.literal(false),
  project_id: id,
  viewer: hash,
  processing_cost: processing,
});
const row = z.object({
  task_id: id,
  outcome: state,
  original_outcome: state,
  reason: z.string().nullable(),
  source_status: z.enum(["verified", "unavailable"]),
  input_count: count.max(20),
  total_pairs: count.max(190),
  configured_fields: id.max(20).nullable(),
  frozen_comparable_pairs: count.max(190),
  comparable_pairs: count.max(190).nullable(),
  has_value_difference: z.boolean().nullable(),
  has_format_difference: z.boolean().nullable(),
  calculation_hash: hash.nullable(),
});
const item = row.extend({
  inputs: z.array(sourcePin).max(20),
  comparison: fieldComparisonSchema,
  evidence: evidence.nullable(),
  evidence_hash: hash.nullable(),
});
const extraReasons = ["insufficient_inputs", "missing_submission", "latest_revision_unusable"];
const reasons = [
  ...extraReasons,
  "unsupported_config",
  "config_mismatch",
  "task_data_mismatch",
  "empty_or_missing_text",
  "cancelled_or_missing",
  "missing_or_malformed",
  "missing_field",
  "no_values",
  "source_unavailable",
];
const valid = (value) => {
  if (!value) throw new Error("Invalid frozen field batch");
};
const reference = (value) => value.manifest_hash ?? value.projection_hash;
const same = (a, b) => fieldDigest(a) === fieldDigest(b);
const checkMeta = (v) => {
  valid(
    v.visibility === "project"
      ? Boolean(v.manifest_hash) && !v.projection_hash
      : Boolean(v.projection_hash) && !v.manifest_hash,
  );
};
const checkRow = (r) => {
  valid(r.total_pairs === (r.input_count * (r.input_count - 1)) / 2 && r.frozen_comparable_pairs <= r.total_pairs);
  if (r.source_status === "unavailable") {
    valid(
      r.outcome === "unavailable" &&
        r.reason === "source_unavailable" &&
        r.calculation_hash === null &&
        r.comparable_pairs === null &&
        r.has_value_difference === null &&
        r.has_format_difference === null,
    );
  } else
    valid(
      r.calculation_hash !== null &&
        r.comparable_pairs === r.frozen_comparable_pairs &&
        r.outcome === r.original_outcome &&
        typeof r.has_value_difference === "boolean" &&
        typeof r.has_format_difference === "boolean" &&
        (r.outcome === "unavailable"
          ? r.comparable_pairs === 0 &&
            reasons.includes(r.reason) &&
            r.reason !== "source_unavailable" &&
            !r.has_value_difference &&
            !r.has_format_difference
          : r.comparable_pairs > 0 &&
            r.reason === null &&
            (r.outcome === "disagreement") === (r.has_value_difference || r.has_format_difference)),
    );
};

export const validateFieldBatchItem = (answer, expected) => {
  const r = answer.item;
  const c = r.comparison;
  checkRow(r);
  valid(
    r.task_id === expected.task &&
      c.input_count === r.input_count &&
      c.total_pairs === r.total_pairs &&
      c.configured_fields === r.configured_fields &&
      c.availability === (r.outcome === "unavailable" ? "unavailable" : "available") &&
      c.reason === r.reason,
  );
  if (r.source_status === "unavailable") {
    valid(
      r.inputs.length === 0 &&
        r.evidence === null &&
        r.evidence_hash === null &&
        c.pairs === null &&
        c.selected_pair === null &&
        c.reason === "source_unavailable",
    );
    return;
  }
  const e = r.evidence;
  valid(
    e &&
      fieldDigest(e) === r.evidence_hash &&
      e.project_id === expected.projectId &&
      e.batch_id === answer.id &&
      e.task_id === r.task_id &&
      e.viewer === answer.viewer &&
      e.reference === reference(answer) &&
      e.calculated_at === answer.calculated_at,
  );
  const pins = e.cohort.flatMap((v) => (v.source ? [v.source] : [])).sort((a, b) => a.submission_id - b.submission_id);
  valid(e.cohort.length === r.input_count && new Set(e.cohort.map((v) => v.assignee_id)).size === e.cohort.length);
  valid(e.cohort.every((v, index) => index === 0 || e.cohort[index - 1].assignment_id < v.assignment_id));
  valid(
    e.cohort.every(
      (v) =>
        !v.source ||
        (v.source.assignment_id === v.assignment_id &&
          v.source.assignee_id === v.assignee_id &&
          v.source.submitter_id === v.assignee_id &&
          v.source.task_id === r.task_id),
    ),
  );
  valid(same(pins, r.inputs) && new Set(pins.map((v) => v.submission_id)).size === pins.length);
  const f = e.result;
  valid(
    f.calculation_hash === r.calculation_hash &&
      f.input_count === r.input_count &&
      f.outcome === r.outcome &&
      f.reason === r.reason &&
      f.configured_fields === r.configured_fields &&
      f.total_pairs === r.total_pairs &&
      f.comparable_pairs === r.comparable_pairs &&
      f.unavailable_pairs === r.total_pairs - r.comparable_pairs &&
      f.has_value_difference === r.has_value_difference &&
      f.has_format_difference === r.has_format_difference,
  );
  const ids = pins.map((v) => v.submission_id);
  validateFieldComparison(c, {
    ids,
    pair: expected.left ? [expected.left, expected.right].sort((a, b) => a - b) : ids.slice(0, 2),
    reasons,
  });
  if (c.pairs) {
    const comparable = c.pairs.filter((p) => p.denominator);
    const rawDifference = comparable.some((p) => p.raw_matches < p.denominator);
    valid(r.outcome === (!comparable.length ? "unavailable" : rawDifference ? "disagreement" : "agreement"));
    valid(r.has_value_difference === comparable.some((p) => p.normalized_matches < p.denominator));
    valid(r.has_format_difference === comparable.some((p) => p.normalized_matches > p.raw_matches));
    valid(c.comparable_pairs === r.comparable_pairs);
    valid(same(f.cost_per_pass, c.cost_per_pass));
  } else valid(f.reason !== "no_values" && same(f.cost_per_pass, { normalized_values: 0, field_equalities: 0 }));
};

const FieldBatchComparison = ({ answer, busy, onSelectPair }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const [detail, setDetail] = useState(true);
  const matrixRefs = useRef({});
  const heading = useRef(null);
  useEffect(() => {
    if (detail) heading.current?.focus();
  }, [detail, answer]);
  const r = answer.item;
  return (
    <>
      {r.source_status === "verified" && (
        <p>
          {r.has_value_difference && <span>{t("fieldBatchValueDifference")} </span>}
          {r.has_format_difference && <span>{t("fieldBatchFormatDifference")}</span>}
        </p>
      )}
      <FieldResult
        report={{
          ...r.comparison,
          rule: answer.rule,
          calculated_at: answer.calculated_at,
          observed_at: answer.observed_at,
        }}
        evidence={{ reference: reference(answer), inputs: r.inputs }}
        extraReasons={extraReasons}
        busy={busy}
        detail={detail}
        onSelectPair={(pair) => {
          setDetail(true);
          onSelectPair(pair);
        }}
        onBack={() => {
          setDetail(false);
          const pair = r.comparison.selected_pair;
          if (pair) matrixRefs.current[`${pair.left}-${pair.right}`]?.focus();
        }}
        matrixRefs={matrixRefs}
        detailHeading={heading}
      />
    </>
  );
};

export const fieldBatchAdapter = {
  testId: "field-batches",
  refreshTestId: "field-batch-refresh",
  guardIntentViewer: true,
  methods: {
    classificationBatchCapability: "fieldBatchCapability",
    classificationBatches: "fieldBatches",
    classificationBatchDetail: "fieldBatchDetail",
    classificationBatchItems: "fieldBatchItems",
    classificationBatchItem: "fieldBatchItem",
    classificationBatchPreview: "fieldBatchPreview",
    createClassificationBatch: "createFieldBatch",
  },
  schemas: {
    capability: cap,
    preview: z.object({
      schema: versions.schema,
      project_id: id,
      viewer: hash,
      preview_token: z.string().min(1).max(16384),
      plan_hash: hash,
      task_ids: z.array(id).min(1).max(100),
      display_counts: counts,
      scope_enumerated_at: z.string(),
      processing_cost: processing,
    }),
    list: z.object({
      count,
      next: z.string().nullable(),
      previous: z.string().nullable(),
      results: z.array(meta).max(8),
      project_id: id,
      viewer: hash,
      processing_cost: processing,
    }),
    detail: meta.extend({ display_counts: counts, processing_cost: processing }),
    queue: meta.extend({
      batch_id: id,
      count,
      next: z.string().nullable(),
      previous: z.string().nullable(),
      display_counts: counts,
      results: z.array(row).max(100),
      processing_cost: processing,
    }),
    item: meta.extend({ observed_at: z.string(), item, processing_cost: processing }),
    created: meta.extend({
      replayed: z.boolean(),
      idempotency_key: z.string().uuid(),
      plan_hash: hash,
      processing_cost: processing,
    }),
  },
  messages: { batchTitle: "fieldBatchTitle", batchBoundary: "fieldBatchBoundary", batchLimits: "fieldBatchLimits" },
  previewRejections: {
    FIELD_BATCH_PREVIEW_INVALID: 400,
    FIELD_BATCH_PREVIEW_CHANGED: 409,
    FIELD_BATCH_SCOPE_CHANGED: 409,
    FIELD_BATCH_SOURCE_INVALID: 409,
    FIELD_BATCH_SOURCE_CHANGED: 409,
    FIELD_BATCH_TOO_LARGE: 422,
    FIELD_TOO_LARGE: 422,
    FIELD_SOURCE_INTEGRITY_FAILED: 409,
  },
  match: (a, b) => {
    valid(a && b && a.project_id === b.project_id && a.viewer === b.viewer);
    if (reference(a)) valid(a.id === b.id && reference(a) === reference(b) && a.calculated_at === b.calculated_at);
  },
  validate: (data, expected) => {
    valid(data.project_id === expected.projectId);
    if (data.visibility) checkMeta(data);
    if (expected.batch !== undefined) valid(data.id === expected.batch);
    if (expected.method === "createClassificationBatch") valid(data.plan_hash === expected.body.expected_plan_hash);
    if (data.results?.[0]?.schema)
      data.results.forEach((r) => {
        checkMeta(r);
        valid(r.project_id === expected.projectId && r.viewer === data.viewer);
      });
    else data.results?.forEach(checkRow);
    if (data.display_counts && data.visible_task_count !== undefined)
      valid(Object.values(data.display_counts).reduce((a, b) => a + b, 0) === data.visible_task_count);
    if (data.item) validateFieldBatchItem(data, expected);
  },
  renderItem: (props) => <FieldBatchComparison {...props} />,
};

export const FieldBatchTitle = () => {
  const { t } = useLocaleTranslation("reviewBatches");
  return t("fieldBatchTitle");
};
export const FieldBatchEntry = ({ projectId, onOpen, disabled }) => {
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
        const raw = await call.current("fieldBatchCapability", { params: { pk: projectId }, errorFilter: () => true });
        const result = cap.safeParse(raw);
        if (!disposed && current === generation)
          setEnabled(
            !raw?.error &&
              raw?.$meta?.ok !== false &&
              typeof raw?.status !== "number" &&
              result.success &&
              result.data.project_id === projectId,
          );
      } catch {
        /* deny */
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
  }, [projectId]);
  return enabled ? (
    <Button size="small" look="outlined" disabled={disabled} onClick={onOpen} data-testid="open-field-batches">
      {t("fieldBatchTitle")}
    </Button>
  ) : null;
};
export const FieldBatches = (props) => {
  const adapter = useRef(null);
  if (!adapter.current) {
    let scope = null;
    const items = new Map();
    adapter.current = {
      ...fieldBatchAdapter,
      retainItem: (data) => {
        const current = JSON.stringify([data.project_id, data.id, data.viewer, reference(data)]);
        if (scope !== current) {
          scope = current;
          items.clear();
        }
        if (!data.item.evidence) return;
        const previous = items.get(data.item.task_id);
        valid(previous === undefined || previous === data.item.evidence_hash);
        items.set(data.item.task_id, data.item.evidence_hash);
      },
    };
  }
  return <FrozenBatchWorkspace {...props} adapter={adapter.current} />;
};
