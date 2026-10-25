import { useEffect, useRef, useState } from "react";
import { diffJson } from "diff";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { cn } from "../../utils/bem";
import { useAPI } from "../../providers/ApiProvider";

const failed = (r) => !r || r.error || r.$meta?.ok === false || !r.results?.source || !Array.isArray(r.results.revisions);

// jsdiff provides canonical JSON key ordering and bounded line comparison.
export const snapshotDiff = (left, right) => diffJson(left, right, { timeout: 100, maxEditLength: 2000 });

export const ReworkComparison = ({ projectId, batchId, sourceId, onClose, viewerHash = null }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const api = useAPI();
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const generation = useRef(0);
  const heading = useRef(null);
  const [data, setData] = useState(null);
  const [selected, setSelected] = useState(null);
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const load = async (targetPage = 1) => {
    const request = ++generation.current;
    setBusy(true); setError(false); setData(null); setSelected(null);
    try {
      const result = await call.current("reviewBatchRework", {
        params: { pk: projectId, batch: batchId, source: sourceId, page: targetPage }, errorFilter: () => true,
      });
      if (request !== generation.current) return;
      if (failed(result) || (viewerHash !== null && result.viewer_hash !== viewerHash)) throw new Error("comparison unavailable");
      setData(result); setSelected(result.results.revisions[0]?.id ?? null); setPage(targetPage);
    } catch { if (request === generation.current) setError(true); }
    finally { if (request === generation.current) setBusy(false); }
  };
  useEffect(() => {
    load(); heading.current?.focus();
    return () => { generation.current += 1; };
  }, [projectId, batchId, sourceId, viewerHash]);
  // No cached answers survive a failed read, scope change, refresh or tab blur.
  useEffect(() => {
    const clear = () => { generation.current += 1; setData(null); setSelected(null); setBusy(false); };
    window.addEventListener("blur", clear);
    return () => window.removeEventListener("blur", clear);
  }, []);
  const source = data?.results.source;
  const revision = data?.results.revisions.find((r) => r.id === selected);
  const changes = revision ? snapshotDiff(source.result_snapshot, revision.result_snapshot) : null;
  const answerChanges = revision ? snapshotDiff(source.result_snapshot.annotation?.result, revision.result_snapshot.annotation?.result) : null;
  const state = (s) => t(["pending", "approved", "rejected", "superseded"].includes(s) ? s : "unknownStatus");
  const evidence = (row, label) => <section data-testid={label === "originalRevision" ? "rework-original" : "rework-selected"}>
    <h5>{t(label)}</h5><p>{t("revisionIdentity", { id: row.id, revision: row.revision })}</p>
    <p>{state(row.status)}</p><code>{row.result_hash}</code>
    {row.review && <><p>{row.review.reason || t("noReviewReason")}</p>
      <p>{t("reviewEvidence", { id: row.review.id, reviewer: row.review.reviewer?.id, time: row.review.created_at })}</p></>}
    <pre>{JSON.stringify(row.result_snapshot, null, 2)}</pre>
  </section>;
  return <section data-testid="rework-comparison" aria-busy={busy}>
    <h4 ref={heading} tabIndex={-1}>{t("compareRework")}</h4>
    <p>{t("comparisonBoundary")}</p>
    <div><Button onClick={onClose}>{t("backToBatch")}</Button>
      <Button disabled={busy} onClick={() => load(page)}>{t("refreshComparison")}</Button></div>
    {busy && <p role="status">{t("loadingComparison")}</p>}
    {!busy && !error && !data && <p role="status">{t("comparisonNeedsRefresh")}</p>}
    {error && <p role="alert">{t("comparisonUnavailable")}</p>}
    {source && <>
      <label>{t("chooseRevision")}<select value={selected ?? ""} onChange={(e) => setSelected(Number(e.target.value))}>
        {data.results.revisions.map((r) => <option key={r.id} value={r.id}>
          {t("revisionIdentity", { id: r.id, revision: r.revision })} · {state(r.status)}
        </option>)}
      </select></label>
      <div><Button disabled={busy || page <= 1} onClick={() => load(page - 1)}>{t("newerRevisions")}</Button>
        <Button disabled={busy || !data.next} onClick={() => load(page + 1)}>{t("olderRevisions")}</Button></div>
      {!revision && <p>{t("noReworkRevisions")}</p>}
      {revision && <>
        <p role="status">{t(answerChanges ? (answerChanges.some((part) => part.added || part.removed) ? "answersChanged" : "answersUnchanged") : "diffUnavailable")}</p>
        <p>{t("resultComparisonBoundary")}</p>
        <div className={cn("review-batches").elem("comparison").toClassName()}>{evidence(source, "originalRevision")}{evidence(revision, "comparedRevision")}</div>
        <h5>{t("snapshotChanges")}</h5>
        <p>{t("diffLegend")}</p>
        {changes ? <pre data-testid="rework-diff">{changes.map((part, index) =>
          <span key={index} className={part.added ? cn("review-batches").elem("added").toClassName() : part.removed ? cn("review-batches").elem("removed").toClassName() : undefined}>
            {part.value.split("\n").filter((line, i, lines) => i < lines.length - 1 || line).map((line, i) =>
              <span key={i}>{part.added ? "+ " : part.removed ? "− " : "  "}{line}{"\n"}</span>)}
          </span>)}</pre> : <p>{t("diffUnavailable")}</p>}
      </>}
    </>}
  </section>;
};
