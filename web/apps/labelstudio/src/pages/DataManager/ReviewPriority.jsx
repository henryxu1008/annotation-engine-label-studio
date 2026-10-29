import { useEffect, useRef, useState } from "react";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";

const strategies = {
  classification_conflict: { algorithm: "classification-conflict-ranked/v1", schema: "review-classification-priority/v1",
    api: "classificationBatches", enable: "priorityEnable", batch: "priorityBatch", boundary: "priorityBoundary", name: "priorityClassification" },
  ner_conflict: { algorithm: "ner-conflict-ranked/v1", schema: "review-ner-priority/v1",
    api: "nerBatches", enable: "priorityNerEnable", batch: "priorityNerBatch", boundary: "priorityNerBoundary", name: "priorityNer" },
};
const hex = (s) => typeof s === "string" && /^[0-9a-f]{64}$/.test(s);
const id = (n) => Number.isSafeInteger(n) && n > 0;
export const isNerPriority = (value) => value?.kind === "ner_conflict";
export const validPriorityChoice = (value) => Boolean(strategies[value?.kind])
  && id(value.batch_id) && hex(value.manifest_hash);

export const validPriorityPreview = (result, choice) => {
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

export const ReviewPrioritySelector = ({ projectId, value, onChange, kinds = ["classification_conflict"] }) => {
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
    if (!kinds.includes(kind)) return;
    const current = ++generation.current;
    setLoading(true); setFailed(false); setRows([]);
    try {
      const result = await call.current(strategy.api, {
        params: { pk: projectId, page: target }, errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (result?.error || result?.$meta?.ok === false || !Array.isArray(result?.results)
        || result.results.some((r) => !validPriorityChoice({ kind, batch_id: r.id, manifest_hash: r.manifest_hash })
          || typeof r.name !== "string")) throw new Error("unavailable");
      if (kind === "ner_conflict") {
        if (result.project_id !== projectId || !hex(result.viewer)) throw new Error("identity changed");
        if (viewer.current && viewer.current !== result.viewer) {
          viewer.current = result.viewer;
          throw new Error("identity changed");
        }
        viewer.current = result.viewer;
      }
      setRows(result.results); setPage(target); setNext(Boolean(result.next));
    } catch {
      if (current === generation.current) {
        setFailed(true);
        if (kind === "ner_conflict") onChange({ kind, batch_id: null, manifest_hash: "" });
      }
    } finally { if (current === generation.current) setLoading(false); }
  };
  useEffect(() => {
    viewer.current = null;
    if (enabled) load(1);
    return () => { generation.current += 1; };
  }, [projectId, enabled, kind]);
  return <div data-testid="review-priority-selector">
    <label><input type="checkbox" checked={enabled} onChange={(event) => onChange(event.target.checked
      ? { kind, batch_id: null, manifest_hash: "" } : null)} />{t(strategy.enable)}</label>
    {enabled && <>
      {kinds.length > 1 && <label>{t("priorityType")}<select value={kind} onChange={(event) =>
        onChange({ kind: event.target.value, batch_id: null, manifest_hash: "" })}>
        {kinds.map((name) => <option key={name} value={name}>{t(strategies[name].name)}</option>)}
      </select></label>}
      <p>{t(strategy.boundary)}</p>
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
  </div>;
};

export const ReviewPrioritySummary = ({ summary }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  return summary ? <div data-testid="review-priority-summary">
    <p>{t("priorityCounts", { total: summary.conflict_candidates, selected: summary.selected_conflicts, unknown: summary.unknown_candidates })}</p>
    <p>{t("priorityUnknownBoundary")}</p>
    {summary.selected_conflicts < summary.conflict_candidates && <p>{t("priorityUncovered")}</p>}
  </div> : null;
};
