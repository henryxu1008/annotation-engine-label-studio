import { useEffect, useRef, useState } from "react";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { validFieldPreview } from "./fieldReviewPriority";
import { isConfidence, validConfidenceChoice, validConfidencePreview } from "./confidenceReviewPriority";
import { useAPI } from "../../providers/ApiProvider";

const strategies = {
  revision_confidence: { enable: "priorityConfidenceEnable", boundary: "priorityConfidenceBoundary", name: "priorityConfidence" },
  field_conflict: { algorithm: "field-conflict-ranked/v1", schema: "review-field-priority/v1",
    api: "fieldBatches", enable: "priorityFieldEnable", batch: "priorityFieldBatch", boundary: "priorityFieldBoundary", name: "priorityField" },
  classification_conflict: { algorithm: "classification-conflict-ranked/v1", schema: "review-classification-priority/v1",
    api: "classificationBatches", enable: "priorityEnable", batch: "priorityBatch", boundary: "priorityBoundary", name: "priorityClassification" },
  ner_conflict: { algorithm: "ner-conflict-ranked/v1", schema: "review-ner-priority/v1",
    api: "nerBatches", enable: "priorityNerEnable", batch: "priorityNerBatch", boundary: "priorityNerBoundary", name: "priorityNer" },
};
const hex = (s) => typeof s === "string" && /^[0-9a-f]{64}$/.test(s);
const id = (n) => Number.isSafeInteger(n) && n > 0;
export const isNerPriority = (value) => value?.kind === "ner_conflict";
export const isBoundedPriority = (value) => ["ner_conflict", "field_conflict", "revision_confidence"].includes(value?.kind);
export const priorityCapability = (value) => isConfidence(value) ? "can_prioritize_confidence"
  : value?.kind === "field_conflict" ? "can_prioritize_field" : "can_prioritize_ner";
export const validPriorityChoice = (value) => isConfidence(value) ? validConfidenceChoice(value)
  : Boolean(strategies[value?.kind]) && id(value.batch_id) && hex(value.manifest_hash);

export const validPriorityPreview = (result, choice, projectId) => {
  if (isConfidence(choice)) return validConfidencePreview(result, choice, projectId);
  if (choice?.kind === "field_conflict") return validFieldPreview(result, choice, projectId);
  const p = result?.plan?.priority;
  const candidates = result?.plan?.candidates;
  const evidence = p?.evidence;
  const strategy = strategies[choice?.kind];
  const common = Boolean(strategy) && hex(result?.plan_hash) && result?.plan?.algorithm === strategy.algorithm
    && p?.schema === strategy.schema && p.batch_id === choice.batch_id && p.manifest_hash === choice.manifest_hash
    && Array.isArray(candidates) && candidates.length > 0 && candidates.length <= 1000
    && Array.isArray(evidence) && evidence.length === candidates.length
    && new Set(candidates.map((c) => c.submission_id)).size === candidates.length
    && candidates.every((c, index) => id(c.submission_id) && typeof c.selected === "boolean"
      && evidence[index]?.submission_id === c.submission_id && ["conflict", "agreement", "unknown"].includes(evidence[index]?.state));
  if (!common || !isNerPriority(choice)) return common;
  return p.kind === "ner_conflict" && p.metric_rule === "ner-strict-entities/v1"
    && p.selection === "active-assignment-latest/v1" && p.explanation === "ner-overlap-explanation/v1"
    && p.engine === "nervaluate/0.2.0/strict-correct-counter" && hex(result.viewer_hash)
    && id(p.creator?.actor_id) && typeof p.creator?.principal === "string" && typeof p.creator?.source === "string"
    && candidates.every((c) => [c.assignment_id, c.task_id, c.revision, c.submitter_id].every(id) && hex(c.result_hash))
    && evidence.every((e) => ["frozen_conflict", "frozen_agreement", "not_covered", "revision_mismatch", "metric_unavailable"].includes(e.reason)
      && (e.calculation_hash === null || hex(e.calculation_hash)));
};

export const ReviewPrioritySelector = ({ projectId, value, onChange, kinds = ["classification_conflict"], confidenceProfiles = [] }) => {
  const api = useAPI();
  const { t } = useLocaleTranslation("reviewBatches");
  const call = useRef(api.callApi); call.current = api.callApi;
  const generation = useRef(0);
  const viewer = useRef(null);
  const [rows, setRows] = useState([]);
  const [page, setPage] = useState(1);
  const [next, setNext] = useState(false);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const enabled = value !== null;
  const kind = value?.kind ?? kinds[0];
  const strategy = strategies[kind];
  const load = async (target) => {
    if (!kinds.includes(kind) || isConfidence({ kind })) return;
    const current = ++generation.current;
    setLoading(true); setFailed(false); setRows([]);
    try {
      let fieldViewer = null;
      if (kind === "field_conflict") {
        const capability = await call.current("fieldBatchCapability", { params: { pk: projectId }, errorFilter: () => true });
        if (current !== generation.current) return;
        if (capability?.project_id !== projectId || capability?.schema !== "field-batches/v1" || !hex(capability.viewer)) throw new Error("identity changed");
        fieldViewer = capability.viewer;
      }
      const result = await call.current(strategy.api, {
        params: { pk: projectId, page: target }, errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (result?.error || result?.$meta?.ok === false || !Array.isArray(result?.results)
        || result.results.some((r) => !validPriorityChoice({ kind, batch_id: r.id, manifest_hash: r.manifest_hash })
          || typeof r.name !== "string")) throw new Error("unavailable");
      if (isBoundedPriority({ kind })) {
        if (result.project_id !== projectId || !hex(result.viewer)) throw new Error("identity changed");
        if (viewer.current && viewer.current !== result.viewer) {
          viewer.current = result.viewer;
          throw new Error("identity changed");
        }
        viewer.current = result.viewer;
      }
      if (kind === "field_conflict") {
        const capability = await call.current("fieldBatchCapability", { params: { pk: projectId }, errorFilter: () => true });
        if (current !== generation.current) return;
        if (fieldViewer !== result.viewer || capability?.project_id !== projectId || capability?.viewer !== fieldViewer) throw new Error("identity changed");
      }
      setRows(result.results); setPage(target); setNext(Boolean(result.next));
    } catch {
      if (current === generation.current) {
        setFailed(true);
        if (isBoundedPriority({ kind })) onChange({ kind, batch_id: null, manifest_hash: "" });
      }
    } finally { if (current === generation.current) setLoading(false); }
  };
  useEffect(() => {
    viewer.current = null;
    if (enabled) load(1);
    return () => { generation.current += 1; };
  }, [projectId, enabled, kind]);
  const emptyChoice = (type) => isConfidence({ kind: type })
    ? { kind: type, profile_id: "", profile_digest: "", threshold_units: 400000 }
    : { kind: type, batch_id: null, manifest_hash: "" };
  return <div data-testid="review-priority-selector">
    <label><input type="checkbox" checked={enabled} onChange={(event) => onChange(event.target.checked
      ? emptyChoice(kind) : null)} />{t(strategy.enable)}</label>
    {enabled && <>
      {kinds.length > 1 && <label>{t("priorityType")}<select value={kind} onChange={(event) =>
        onChange(emptyChoice(event.target.value))}>
        {kinds.map((name) => <option key={name} value={name}>{t(strategies[name].name)}</option>)}
      </select></label>}
      <p>{t(strategy.boundary)}</p>
      {isConfidence(value) ? <>
        <label>{t("priorityConfidenceProfile")}<select value={`${value.profile_id}:${value.profile_digest}`} onChange={(event) => {
          const profile = confidenceProfiles.find((p) => `${p.profile_id}:${p.digest}` === event.target.value);
          onChange({ ...value, profile_id: profile?.profile_id ?? "", profile_digest: profile?.digest ?? "" });
        }}>
          <option value=":">{t("priorityChoose")}</option>
          {confidenceProfiles.map((p) => <option key={`${p.profile_id}:${p.digest}`} value={`${p.profile_id}:${p.digest}`}>
            {p.label} · {p.version} · {p.digest}
          </option>)}
        </select></label>
        <label>{t("priorityConfidenceThreshold")}<input type="number" min="0" max="1000000" step="1"
          value={value.threshold_units} onChange={(event) => onChange({ ...value,
            threshold_units: event.target.value === "" ? "" : Number(event.target.value) })} /></label>
        <p>{t("priorityConfidenceThresholdHelp")}</p>
      </> : <>
      <label>{t(strategy.batch)}<select disabled={loading} value={value.batch_id ?? ""} onChange={(event) => {
        const selected = rows.find((r) => r.id === Number(event.target.value));
        onChange({ kind, batch_id: selected?.id ?? null, manifest_hash: selected?.manifest_hash ?? "" });
      }}>
        <option value="">{t("priorityChoose")}</option>
        {value.batch_id && !rows.some((r) => r.id === value.batch_id) && <option value={value.batch_id}>#{value.batch_id}</option>}
        {rows.map((row) => <option key={row.id} value={row.id}>#{row.id} · {row.name}</option>)}
      </select></label>
      {failed && <p role="alert">{t("priorityLoadFailed")}</p>}
      {!loading && !failed && !rows.length && <p>{t("priorityEmpty")}</p>}
      <Button disabled={loading} onClick={() => load(page)}>{t("priorityReload")}</Button>
      <Button disabled={loading || page <= 1} onClick={() => load(page - 1)}>{t("previous")}</Button>
      <Button disabled={loading || !next} onClick={() => load(page + 1)}>{t("next")}</Button>
      </>}
    </>}
  </div>;
};

export const ReviewPrioritySummary = ({ summary }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  return summary ? <div data-testid="review-priority-summary">
    {isConfidence(summary) ? <>
      <p>{t("priorityConfidenceCounts", { known: summary.known_candidates, unknown: summary.unknown_candidates,
        low: summary.low_confidence_candidates, selected: summary.selected_low_confidence,
        uncovered: summary.uncovered_low_confidence, fallback: summary.fallback_selected })}</p>
      <p>{t("priorityConfidenceUnknownBoundary")}</p>
      {summary.known_candidates === 0 && <p>{t("priorityConfidenceZeroCoverage")}</p>}
    </> : <>
    <p>{t("priorityCounts", { total: summary.conflict_candidates, selected: summary.selected_conflicts, unknown: summary.unknown_candidates })}</p>
    <p>{t("priorityUnknownBoundary")}</p>
    {summary.selected_conflicts < summary.conflict_candidates && <p>{t("priorityUncovered")}</p>}
    </>}
  </div> : null;
};

export const ReviewConfidenceSource = ({ source }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  return isConfidence(source) ? <div data-testid="review-confidence-source">
    <p>{t("priorityConfidenceProvenance", { label: source.profile.label, version: source.profile.version,
      threshold: source.threshold_units })}</p>
    <p>{t("priorityConfidenceVersions", { model: source.profile.model_id, evaluator: source.profile.evaluator_id,
      version: source.profile.evaluator_version, calibration: source.profile.calibration_version })}</p>
    <p>{t("priorityConfidenceThresholdHelp")}</p>
    <code>{source.profile_digest}</code>
  </div> : null;
};
