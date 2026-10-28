import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { FrozenBatchWorkspace } from "./ClassificationBatches";
import { nerComparisonSchema, NerResult, validateNerComparison } from "./NerDisagreement";
import { NerQualityOverview } from "./NerQualityOverview";

const id = z.number().int().positive().safe(), count = z.number().int().nonnegative().safe();
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const counts = z.object({ agreement: count, disagreement: count, unavailable: count });
const pin = z.object({ submission_id: id, assignment_id: id, assignee_id: id, task_id: id, revision: id,
  submitter_id: id, result_hash: hash, config_hash: hash, task_data_hash: hash });
const metadata = z.object({ id, project_id: id, name: z.string(), schema: z.literal("ner-batches/v1"),
  rule: z.literal("ner-strict-entities/v1"), explanation: z.literal("ner-overlap-explanation/v1"),
  engine: z.literal("nervaluate/0.2.0/strict-correct-counter"), selection: z.literal("active-assignment-latest/v1"),
  created_at: z.string(), calculated_at: z.string(), scope_enumerated_at: z.string(), visible_task_count: count,
  visibility: z.enum(["project", "visible_subset"]), manifest_hash: hash.optional(), projection_hash: hash.optional(), viewer: hash,
}).refine((r) => r.visibility === "project" ? Boolean(r.manifest_hash) && !r.projection_hash : Boolean(r.projection_hash) && !r.manifest_hash);
const cap = z.object({ enabled: z.literal(true), can_create: z.boolean(), project_id: id, viewer: hash,
  can_view_quality_overview: z.boolean().optional(),
  schema: z.literal("ner-batches/v1"), rule: z.literal("ner-strict-entities/v1"), selection: z.literal("active-assignment-latest/v1") });
const row = z.object({ task_id: id, outcome: z.enum(["agreement", "disagreement", "unavailable"]),
  original_outcome: z.enum(["agreement", "disagreement", "unavailable"]), reason: z.string().nullable(),
  source_status: z.enum(["verified", "unavailable"]), input_count: z.number().int().min(0).max(20),
  total_pairs: z.number().int().min(0).max(190), comparable_pairs: z.number().int().min(0).max(190),
  calculation_hash: hash.nullable() });
const valid = (v) => { if (!v) throw new Error("Invalid frozen NER batch"); };
const reference = (value) => value?.manifest_hash ?? value?.projection_hash;

const NerBatchComparison = ({ answer, busy, onSelectPair }) => {
  const [detail, setDetail] = useState(true), matrixRefs = useRef({}), heading = useRef(null);
  useEffect(() => {
    if (detail) heading.current?.focus();
    else {
      const pair = answer.item.comparison.selected_pair;
      if (pair) matrixRefs.current[`${pair.left}-${pair.right}`]?.focus();
    }
  }, [detail, answer]);
  const item = answer.item;
  const report = { ...item.comparison, rule: answer.rule, source_status: item.source_status,
    calculated_at: answer.calculated_at, observed_at: answer.observed_at, manifest_hash: reference(answer),
    manifest: { inputs: item.inputs } };
  return <NerResult report={report} busy={busy} detail={detail} onSelectPair={onSelectPair}
    onBack={() => setDetail(false)} matrixRefs={matrixRefs} detailHeading={heading} />;
};

export const nerBatchAdapter = {
  testId: "ner-batches",
  methods: {
    classificationBatchCapability: "nerBatchCapability", classificationBatches: "nerBatches",
    classificationBatchDetail: "nerBatchDetail", classificationBatchItems: "nerBatchItems",
    classificationBatchItem: "nerBatchItem", classificationBatchPreview: "nerBatchPreview", createClassificationBatch: "createNerBatch",
  },
  schemas: {
    capability: cap,
    preview: z.object({ schema: z.literal("ner-batches/v1"), project_id: id, viewer: hash, preview_token: z.string().min(1).max(16384),
      plan_hash: hash, task_ids: z.array(id).min(1).max(100), display_counts: counts, scope_enumerated_at: z.string() }),
    list: z.object({ count, next: z.string().nullable(), previous: z.string().nullable(), results: z.array(metadata), project_id: id, viewer: hash }),
    detail: z.intersection(metadata, z.object({ display_counts: counts })),
    queue: z.intersection(metadata, z.object({ batch_id: id, count, next: z.string().nullable(), previous: z.string().nullable(),
      display_counts: counts, results: z.array(row) })),
    item: z.intersection(metadata, z.object({ observed_at: z.string(), item: row.extend({ inputs: z.array(pin).max(20), comparison: nerComparisonSchema }) })),
    created: z.intersection(metadata, z.object({ replayed: z.boolean(), idempotency_key: z.string().uuid(), plan_hash: hash })),
  },
  messages: { batchTitle: "nerBatchTitle", batchBoundary: "nerBatchBoundary", batchLimits: "nerBatchLimits" },
  previewRejections: { NER_BATCH_PREVIEW_INVALID: 400, NER_BATCH_PREVIEW_CHANGED: 409, NER_BATCH_SCOPE_CHANGED: 409,
    NER_BATCH_SOURCE_INVALID: 409, NER_BATCH_SOURCE_CHANGED: 409, NER_BATCH_TOO_LARGE: 422, NER_TOO_LARGE: 422,
    NER_SOURCE_INTEGRITY_FAILED: 409 },
  match: (a, b) => {
    valid(a && b && a.project_id === b.project_id && a.viewer === b.viewer);
    if (reference(a)) valid(a.id === b.id && reference(a) === reference(b) && a.calculated_at === b.calculated_at);
  },
  validate: (data, expected) => {
    valid(data.project_id === expected.projectId);
    if (expected.method === "createClassificationBatch") valid(data.plan_hash === expected.body.expected_plan_hash);
    if (expected.batch !== undefined) valid(data.id === expected.batch);
    if (data.results && data.results[0]?.schema) data.results.forEach((r) => valid(r.project_id === expected.projectId && r.viewer === data.viewer));
    if (data.display_counts && data.visible_task_count !== undefined) valid(
      Object.values(data.display_counts).reduce((a, b) => a + b, 0) === data.visible_task_count);
    if (data.item) {
      const r = data.item, comparison = r.comparison, ids = r.inputs.map((p) => p.submission_id);
      valid(r.task_id === expected.task && r.total_pairs === r.input_count * (r.input_count - 1) / 2
        && comparison.input_count === r.input_count && comparison.total_pairs === r.total_pairs
        && r.inputs.every((p) => p.task_id === expected.task && p.assignee_id === p.submitter_id)
        && ids.every((v, i) => !i || ids[i - 1] < v) && new Set(r.inputs.map((p) => p.submitter_id)).size === ids.length);
      if (r.source_status === "unavailable") valid(r.outcome === "unavailable" && r.calculation_hash === null && !r.inputs.length
        && comparison.pairs === null && comparison.selected_pair === null && comparison.reason === "source_unavailable");
      else {
        valid(r.calculation_hash !== null && (r.outcome === "unavailable" || ids.length === r.input_count));
        validateNerComparison(comparison, { ids, pair: expected.left ? [expected.left, expected.right].sort((a,b) => a-b) : ids.slice(0,2) });
        if (r.outcome !== "unavailable") {
          const disagreement = comparison.pairs.some((p) => p.f1 && p.f1.numerator !== p.f1.denominator);
          valid(r.outcome === (disagreement ? "disagreement" : "agreement") && comparison.reason === null);
        }
      }
    }
  },
  renderItem: (props) => <NerBatchComparison {...props} />,
  renderOverview: (props) => <NerQualityOverview {...props} />,
};

export const NerBatchTitle = () => { const { t } = useLocaleTranslation("reviewBatches"); return t("nerBatchTitle"); };
export const NerBatchEntry = ({ projectId, onOpen, disabled }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI(), call = useRef(api.callApi);
  call.current = api.callApi;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let generation = 0, disposed = false;
    const read = async () => {
      const current = ++generation; setEnabled(false);
      try {
        const raw = await call.current("nerBatchCapability", { params: { pk: projectId }, errorFilter: () => true });
        const result = cap.safeParse(raw);
        if (!disposed && current === generation) setEnabled(!raw?.error && raw?.$meta?.ok !== false
          && typeof raw?.status !== "number" && result.success && result.data.project_id === projectId);
      } catch { /* denied */ }
    };
    const blur = () => { generation++; setEnabled(false); };
    read(); window.addEventListener("focus", read); window.addEventListener("blur", blur);
    return () => { disposed = true; generation++; window.removeEventListener("focus", read); window.removeEventListener("blur", blur); };
  }, [projectId]);
  return enabled ? <Button size="small" look="outlined" disabled={disabled} onClick={onOpen} data-testid="open-ner-batches">
    {t("nerBatchTitle")}</Button> : null;
};
export const NerBatches = (props) => <FrozenBatchWorkspace {...props} adapter={nerBatchAdapter} />;
