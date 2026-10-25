import { useEffect, useRef, useState } from "react";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";

export const validPriorityChoice = (value) => value?.kind === "classification_conflict"
  && Number.isSafeInteger(value.batch_id) && value.batch_id > 0 && /^[0-9a-f]{64}$/.test(value.manifest_hash);

export const validPriorityPreview = (result, choice) => {
  const p = result?.plan?.priority;
  const candidates = result?.plan?.candidates;
  const evidence = p?.evidence;
  return /^[0-9a-f]{64}$/.test(result?.plan_hash) && result?.plan?.algorithm === "classification-conflict-ranked/v1"
    && p?.schema === "review-classification-priority/v1" && p.batch_id === choice.batch_id && p.manifest_hash === choice.manifest_hash
    && Array.isArray(candidates) && candidates.length > 0 && candidates.length <= 1000
    && Array.isArray(evidence) && evidence.length === candidates.length
    && new Set(candidates.map((c) => c.submission_id)).size === candidates.length
    && candidates.every((c, index) => Number.isSafeInteger(c.submission_id) && c.submission_id > 0 && typeof c.selected === "boolean"
      && evidence[index]?.submission_id === c.submission_id && ["conflict", "agreement", "unknown"].includes(evidence[index]?.state));
};

export const ReviewPrioritySelector = ({ projectId, value, onChange }) => {
  const api = useAPI();
  const { t } = useLocaleTranslation("reviewBatches");
  const call = useRef(api.callApi); call.current = api.callApi;
  const generation = useRef(0);
  const [rows, setRows] = useState([]);
  const [page, setPage] = useState(1);
  const [next, setNext] = useState(false);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const enabled = value !== null;
  const load = async (target) => {
    const current = ++generation.current;
    setLoading(true); setFailed(false); setRows([]);
    try {
      const result = await call.current("classificationBatches", {
        params: { pk: projectId, page: target }, errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (result?.error || result?.$meta?.ok === false || !Array.isArray(result?.results)
        || result.results.some((r) => !validPriorityChoice({ kind: "classification_conflict", batch_id: r.id, manifest_hash: r.manifest_hash })
          || typeof r.name !== "string")) throw new Error("unavailable");
      setRows(result.results); setPage(target); setNext(Boolean(result.next));
    } catch { if (current === generation.current) setFailed(true); }
    finally { if (current === generation.current) setLoading(false); }
  };
  useEffect(() => {
    if (enabled) load(1);
    return () => { generation.current += 1; };
  }, [projectId, enabled]);
  return <div data-testid="review-priority-selector">
    <label><input type="checkbox" checked={enabled} onChange={(event) => onChange(event.target.checked
      ? { kind: "classification_conflict", batch_id: null, manifest_hash: "" } : null)} />{t("priorityEnable")}</label>
    {enabled && <>
      <p>{t("priorityBoundary")}</p>
      <label>{t("priorityBatch")}<select disabled={loading} value={value.batch_id ?? ""} onChange={(event) => {
        const selected = rows.find((r) => r.id === Number(event.target.value));
        onChange({ kind: "classification_conflict", batch_id: selected?.id ?? null, manifest_hash: selected?.manifest_hash ?? "" });
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
