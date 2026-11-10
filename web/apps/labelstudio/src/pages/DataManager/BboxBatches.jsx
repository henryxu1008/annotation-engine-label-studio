import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { FrozenBatchWorkspace } from "./ClassificationBatches";

const id = z.number().int().positive().safe();
const count = z.number().int().nonnegative().safe();
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const counts = z.object({ agreement: count, disagreement: count, unavailable: count });
const meta = z.object({ id, project_id: id, name: z.string(), schema: z.literal("bbox-quality-batch/v1"),
  rule: z.string(), selection: z.string(), created_at: z.string(), calculated_at: z.string(),
  scope_enumerated_at: z.string(), visible_task_count: count, visibility: z.enum(["project", "visible_subset"]),
  manifest_hash: hash.optional(), projection_hash: hash.optional(), viewer: hash,
}).refine((value) => value.visibility === "project" ? !!value.manifest_hash && !value.projection_hash
  : !!value.projection_hash && !value.manifest_hash);
const cap = z.object({ enabled: z.literal(true), can_create: z.boolean(), project_id: id, viewer: hash,
  schema: z.literal("bbox-quality-batch/v1") });
const row = z.object({ task_id: id, outcome: z.enum(["agreement", "disagreement", "unavailable"]), reason: z.string().nullable() });
const numberText = z.string().regex(/^(0|[1-9][0-9]{0,99})$/);
const coordinate = z.number().int().min(0).max(100000000);
const box = z.tuple([z.string().min(1), coordinate, coordinate, coordinate, coordinate])
  .refine(([, x, y, w, h]) => w > 0 && h > 0 && x + w <= 100000000 && y + h <= 100000000);
export const bboxSource = z.object({ submission_id: id, assignment_id: id, assignee_id: id, task_id: id, revision: id,
  submitter_id: id, result_hash: hash, config_hash: hash, task_data_hash: hash,
  media: z.object({ token: z.string().uuid(), sha256: hash, proof_hash: hash }).nullable() });
export const bboxComparison = z.object({ availability: z.enum(["available", "unavailable"]), reason: z.string().nullable(),
  input_count: count.optional(), boxes: z.array(z.array(box).max(20)).max(20).optional(),
  image: z.object({ sha256: hash, width: id, height: id }).optional(),
  pairs: z.array(z.object({ left: id, right: id, f1: z.object({ numerator: numberText, denominator: numberText }),
    matches: z.array(z.object({ left: count, right: count, iou: z.object({ numerator: numberText, denominator: numberText }) })),
    left_only: z.array(count), right_only: z.array(count) })).max(190).nullable() });
const item = row.extend({ cohort: z.array(z.object({ assignment_id: id, assignee_id: id, source: bboxSource.nullable() })).max(20), comparison: bboxComparison });
const reference = (value) => value?.manifest_hash ?? value?.projection_hash;
const valid = (condition) => { if (!condition) throw new Error("Invalid frozen bbox batch"); };
const reasonKeys = { insufficient_inputs: "bboxReasonInsufficient", missing_submission: "bboxReasonMissing",
  latest_revision_unusable: "bboxReasonRevision", unverified_media: "bboxReasonMedia",
  empty_or_missing_result: "bboxReasonEmpty", invalid_box: "bboxReasonBox", duplicate_box: "bboxReasonBox" };

export const BboxComparison = ({ answer, projectId, imageUrl, preferredPair, onPairSelect, onEvidenceState }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const result = answer.item.comparison;
  const sources = answer.item.cohort.map((entry) => entry.source).filter(Boolean).sort((a, b) => a.submission_id - b.submission_id);
  const [pairIndex, setPairIndex] = useState(0);
  const [imageState, setImageState] = useState("loading");
  useEffect(() => {
    const selected = result.pairs?.findIndex((pair) => pair.left === preferredPair?.[0] && pair.right === preferredPair?.[1]);
    setPairIndex(selected >= 0 ? selected : 0);
  }, [projectId, answer.id, answer.item.task_id, preferredPair?.[0], preferredPair?.[1]]);
  useEffect(() => { setImageState("loading"); }, [projectId, answer.id, answer.item.task_id, imageUrl]);
  const pair = result.pairs?.[pairIndex];
  if (!pair) return <p>{t("bboxUnavailable", {
    reason: t(reasonKeys[result.reason ?? answer.item.reason] ?? "bboxReasonUnsupported"),
  })}</p>;
  const leftIndex = sources.findIndex((value) => value.submission_id === pair.left);
  const rightIndex = sources.findIndex((value) => value.submission_id === pair.right);
  const { width: imageWidth, height: imageHeight } = result.image;
  const image = imageUrl ?? `${window.APP_SETTINGS.hostname}/api/projects/${projectId}/bbox-quality/batches/${answer.id}/items/${answer.item.task_id}/image/`;
  return <>
    <label>{t("bboxPair")}<select value={pairIndex} onChange={(event) => {
      const index = Number(event.target.value), chosen = result.pairs[index];
      if (onPairSelect) onPairSelect([chosen.left, chosen.right]); else setPairIndex(index);
    }}>
      {result.pairs.map((value, index) => <option key={`${value.left}:${value.right}`} value={index}>
        {value.left} / {value.right}</option>)}
    </select></label>
    <p>{t("bboxScore", { numerator: pair.f1.numerator, denominator: pair.f1.denominator })}</p>
    <p>{t("bboxColors", { left: pair.left, right: pair.right })}</p>
    {imageState === "loading" && <p role="status">{t("bboxImageLoading")}</p>}
    {imageState === "error" && <p role="alert">{t("bboxImageUnavailable")}</p>}
    <svg viewBox={`0 0 ${imageWidth} ${imageHeight}`} role="img" aria-label={t("bboxImageLabel")} style={{ width: "100%", maxHeight: "60vh" }}>
      <image href={image} x="0" y="0" width={imageWidth} height={imageHeight}
        onLoad={() => { setImageState("loaded"); onEvidenceState?.(true); }}
        onError={() => { setImageState("error"); onEvidenceState?.(false); }} />
      {imageState === "loaded" && [[leftIndex, "#0072b2"], [rightIndex, "#d55e00"]].flatMap(([index, color]) =>
        result.boxes[index].map(([label, x, y, width, height], boxIndex) =>
          <g key={`${index}:${boxIndex}`}>
            <rect x={x * imageWidth / 100000000} y={y * imageHeight / 100000000}
              width={width * imageWidth / 100000000} height={height * imageHeight / 100000000}
              fill="none" stroke={color} vectorEffect="non-scaling-stroke" strokeWidth="2"><title>{label}</title></rect>
            <text x={x * imageWidth / 100000000} y={y * imageHeight / 100000000}
              fill={color} fontSize={Math.max(imageWidth, imageHeight) * 0.025}>{boxIndex + 1}</text>
          </g>))}
    </svg>
    <h4>{t("bboxMatches")}</h4>
    {pair.matches.length ? <ul>{pair.matches.map((match) => <li key={`${match.left}:${match.right}`}>
      {t("bboxMatched", { left: match.left + 1, right: match.right + 1,
        numerator: match.iou.numerator, denominator: match.iou.denominator })}</li>)}</ul> : <p>{t("bboxNoMatches")}</p>}
    {[[leftIndex, pair.left_only, pair.left], [rightIndex, pair.right_only, pair.right]].map(([index, unmatched, submission]) =>
      <section key={submission}><h4>{t("bboxUnmatched", { submission })}</h4>
        {unmatched.length ? <ul>{unmatched.map((number) => <li key={number}>{t("bboxBox", {
          number: number + 1, label: result.boxes[index][number][0],
        })}</li>)}</ul> : <p>{t("bboxNone")}</p>}
      </section>)}
  </>;
};

export const bboxBatchAdapter = {
  testId: "bbox-batches", guardIntentViewer: true,
  methods: { classificationBatchCapability: "bboxBatchCapability", classificationBatches: "bboxBatches",
    classificationBatchDetail: "bboxBatchDetail", classificationBatchItems: "bboxBatchItems",
    classificationBatchItem: "bboxBatchItem", classificationBatchPreview: "bboxBatchPreview", createClassificationBatch: "createBboxBatch" },
  schemas: { capability: cap,
    preview: z.object({ schema: z.literal("bbox-quality-batch/v1"), project_id: id, viewer: hash,
      preview_token: z.string().min(1).max(16384), plan_hash: hash, task_ids: z.array(id).min(1).max(5),
      display_counts: counts, scope_enumerated_at: z.string() }),
    list: z.object({ project_id: id, viewer: hash, count, next: z.string().nullable(), previous: z.string().nullable(), results: z.array(meta) }),
    detail: z.intersection(meta, z.object({ display_counts: counts })),
    queue: z.intersection(meta, z.object({ batch_id: id, count, next: z.string().nullable(), previous: z.string().nullable(),
      display_counts: counts, results: z.array(row) })),
    item: z.intersection(meta, z.object({ observed_at: z.string(), item })),
    created: z.intersection(meta, z.object({ replayed: z.boolean(), idempotency_key: z.string().uuid(), plan_hash: hash })),
  },
  messages: { batchTitle: "bboxBatchTitle", batchBoundary: "bboxBatchBoundary", batchLimits: "bboxBatchLimits" },
  previewRejections: { BBOX_BATCH_PREVIEW_INVALID: 400, BBOX_BATCH_PREVIEW_CHANGED: 409, BBOX_BATCH_TOO_LARGE: 422 },
  match: (a, b) => {
    valid(a && b && a.project_id === b.project_id && a.viewer === b.viewer);
    if (reference(a)) valid(a.id === b.id && reference(a) === reference(b) && a.calculated_at === b.calculated_at);
  },
  validate: (data, expected) => {
    valid(data.project_id === expected.projectId);
    if (expected.method === "createClassificationBatch") valid(data.plan_hash === expected.body.expected_plan_hash);
    if (expected.batch !== undefined) valid(data.id === expected.batch);
    if (data.results && data.results[0]?.schema) data.results.forEach((value) => valid(value.project_id === data.project_id && value.viewer === data.viewer));
    if (data.display_counts && data.visible_task_count !== undefined)
      valid(Object.values(data.display_counts).reduce((sum, value) => sum + value, 0) === data.visible_task_count);
    if (data.item) {
      const value = data.item, result = value.comparison;
      valid(value.task_id === expected.task && value.cohort.every((entry) => !entry.source ||
        entry.source.task_id === value.task_id && entry.source.assignment_id === entry.assignment_id &&
        entry.source.assignee_id === entry.assignee_id && entry.source.submitter_id === entry.assignee_id));
      const ids = value.cohort.map((entry) => entry.source?.submission_id).filter(Boolean).sort((a, b) => a - b);
      valid(new Set(ids).size === ids.length);
      if (result.availability === "available") {
        valid(result.image && ids.length >= 2 && result.input_count === ids.length && result.boxes?.length === ids.length &&
          result.pairs?.length === ids.length * (ids.length - 1) / 2 && value.cohort.every((entry) =>
            entry.source?.media?.sha256 === result.image.sha256));
        const seen = new Set();
        result.pairs.forEach((pair) => {
          const left = ids.indexOf(pair.left), right = ids.indexOf(pair.right);
          valid(left >= 0 && right > left && BigInt(pair.f1.denominator) > 0n && BigInt(pair.f1.numerator) <= BigInt(pair.f1.denominator));
          const key = `${pair.left}:${pair.right}`;
          valid(!seen.has(key)); seen.add(key);
          valid(pair.matches.every((match) => match.left < result.boxes[left].length && match.right < result.boxes[right].length &&
            BigInt(match.iou.denominator) > 0n && BigInt(match.iou.numerator) <= BigInt(match.iou.denominator)));
          valid(pair.left_only.every((index) => index < result.boxes[left].length) &&
            pair.right_only.every((index) => index < result.boxes[right].length));
        });
        valid(value.outcome === (result.pairs.some((pair) => pair.f1.numerator !== pair.f1.denominator) ? "disagreement" : "agreement"));
      } else valid(result.pairs === null);
    }
  },
  renderItem: (props) => <BboxComparison {...props} />,
};

export const BboxBatchTitle = () => { const { t } = useLocaleTranslation("reviewBatches"); return t("bboxBatchTitle"); };
export const BboxBatchEntry = ({ projectId, onOpen, disabled }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI(), call = useRef(api.callApi);
  call.current = api.callApi;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let generation = 0, disposed = false;
    const read = async () => {
      const current = ++generation; setEnabled(false);
      try {
        const raw = await call.current("bboxBatchCapability", { params: { pk: projectId }, errorFilter: () => true });
        const parsed = cap.safeParse(raw);
        if (!disposed && current === generation) setEnabled(!raw?.error && raw?.$meta?.ok !== false
          && typeof raw?.status !== "number" && parsed.success && parsed.data.project_id === projectId);
      } catch { /* denied */ }
    };
    const blur = () => { generation++; setEnabled(false); };
    read(); window.addEventListener("focus", read); window.addEventListener("blur", blur);
    return () => { disposed = true; generation++; window.removeEventListener("focus", read); window.removeEventListener("blur", blur); };
  }, [projectId]);
  return enabled ? <Button size="small" look="outlined" disabled={disabled} onClick={onOpen} data-testid="open-bbox-batches">
    {t("bboxBatchTitle")}</Button> : null;
};
export const BboxBatches = (props) => <FrozenBatchWorkspace {...props} adapter={bboxBatchAdapter} />;
