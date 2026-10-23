import { useCallback, useEffect, useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { cn } from "../../utils/bem";
import { ReviewerWorkspace } from "./ReviewerWorkspace";
import { batchProgress, filterBatchItems } from "./reviewBatchProgress";
import "./ReviewBatches.scss";

const failed = (r) => !r || r.error || r.$meta?.ok === false;
const code = (r) => r?.response?.display_context?.reason ?? r?.display_context?.reason;

export const ReviewBatchTitle = () => {
  const { t } = useLocaleTranslation("reviewBatches");
  return t("title");
};

export const ReviewBatchEntry = ({ projectId, onOpen, disabled }) => {
  const api = useAPI();
  const { t } = useLocaleTranslation("reviewBatches");
  const [enabled, setEnabled] = useState(false);
  const call = useRef(api.callApi);
  call.current = api.callApi;
  useEffect(() => {
    let disposed = false;
    setEnabled(false);
    Promise.resolve(call.current("reviewBatchCapability", { params: { pk: projectId }, errorFilter: () => true }))
      .then((r) => { if (!disposed) setEnabled(!failed(r) && r.enabled === true); }).catch(() => {});
    return () => { disposed = true; };
  }, [projectId]);
  return enabled ? <Button size="small" look="outlined" disabled={disabled} onClick={onOpen}
    data-testid="open-review-batches">{t("title")}</Button> : null;
};

export const ReviewBatches = ({ projectId }) => {
  const api = useAPI();
  const { t } = useLocaleTranslation("reviewBatches");
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const generation = useRef(0);
  const creation = useRef(null);
  const selection = useRef(null);
  const progressGeneration = useRef(0);
  const successorGeneration = useRef(0);
  const [batches, setBatches] = useState([]);
  const [detail, setDetail] = useState(null);
  const [canCreate, setCanCreate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);
  const [next, setNext] = useState(false);
  const [form, setForm] = useState({ name: "", rate: 10, per_submitter: false, seed: "review-v1", package_ids: "", submitter_ids: "" });
  const [preview, setPreview] = useState(null);
  const [reviewing, setReviewing] = useState(false);
  const [successor, setSuccessor] = useState(null);
  const [selectedBatchId, setSelectedBatchId] = useState(null);
  const [progressBusy, setProgressBusy] = useState(false);
  const [savedReview, setSavedReview] = useState(false);
  const [filter, setFilter] = useState("all");

  const load = useCallback(async (id = null, targetPage = 1) => {
    const current = ++generation.current;
    selection.current = { id, projectId };
    setSelectedBatchId(id); setFilter("all"); setSavedReview(false); setProgressBusy(false);
    setBusy(true); setError(""); setDetail(null); setReviewing(false); setBatches([]); setSuccessor(null);
    try {
      const [list, capability, selected] = await Promise.all([
        call.current("reviewBatches", { params: { pk: projectId, page: targetPage }, errorFilter: () => true }),
        call.current("reviewBatchCapability", { params: { pk: projectId }, errorFilter: () => true }),
        id === null ? Promise.resolve(null) : call.current("reviewBatchDetail", {
          params: { pk: projectId, batch: id }, errorFilter: () => true,
        }),
      ]);
      if (current !== generation.current) return;
      if (failed(list) || failed(capability) || (id !== null && failed(selected))) {
        setCanCreate(false); setPreview(null); creation.current = null;
        setError("loadFailed"); return;
      }
      setBatches(list.results?.batches ?? []);
      setCanCreate(capability.can_create === true);
      setPage(targetPage); setNext(Boolean(list.next)); setDetail(selected);
    } catch {
      if (current === generation.current) { setError("loadFailed"); setCanCreate(false); setPreview(null); creation.current = null; }
    } finally { if (current === generation.current) setBusy(false); }
  }, [projectId]);

  // Refresh only the detail: keep the reviewer queue, selection and batch context mounted.
  const refreshProgress = async (id, reviewSaved = null, context = selection.current) => {
    if (!context || context !== selection.current) return;
    const current = generation.current;
    const request = ++progressGeneration.current;
    const isCurrent = () => current === generation.current && request === progressGeneration.current;
    successorGeneration.current += 1;
    setProgressBusy(true); setError(""); setSuccessor(null);
    if (reviewSaved !== null) setSavedReview(reviewSaved);
    try {
      const [result, capability] = await Promise.all([
        call.current("reviewBatchDetail", { params: { pk: projectId, batch: id }, errorFilter: () => true }),
        call.current("reviewBatchCapability", { params: { pk: projectId }, errorFilter: () => true }),
      ]);
      if (!isCurrent()) return;
      if (failed(result) || failed(capability)) throw new Error("detail unavailable");
      setDetail(result);
      setCanCreate(capability.can_create === true);
    } catch {
      if (!isCurrent()) return;
      // A failed read cannot establish current authority. Keep only the retry context.
      setDetail(null); setReviewing(false); setBatches([]);
      setCanCreate(false); setPreview(null); creation.current = null;
      setError(reviewSaved ? "savedRefreshFailed" : "progressRefreshFailed");
    } finally { if (isCurrent()) setProgressBusy(false); }
  };

  useEffect(() => {
    setPreview(null); creation.current = null; load();
    return () => { generation.current += 1; selection.current = null; };
  }, [load]);

  const change = (key, value) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    setPreview(null); creation.current = null; setError("");
  };
  const requestPreview = async () => {
    setBusy(true); setError(""); setPreview(null); creation.current = null;
    const current = ++generation.current;
    try {
      const ids = (value) => value.trim() ? value.split(/[\s,]+/).map(Number) : [];
      const body = { ...form, rate: Number(form.rate), package_ids: ids(form.package_ids), submitter_ids: ids(form.submitter_ids) };
      const result = await call.current("reviewBatchPreview", { params: { pk: projectId }, body, errorFilter: () => true });
      if (current !== generation.current) return;
      if (failed(result)) { setError(code(result) === "REVIEW_BATCH_TOO_LARGE" ? "tooLarge" : "previewFailed"); return; }
      creation.current = { body: { ...body, expected_plan_hash: result.plan_hash }, key: uuidv4() };
      setPreview(result);
    } catch { if (current === generation.current) setError("previewFailed"); }
    finally { if (current === generation.current) setBusy(false); }
  };
  const create = async () => {
    if (!creation.current) return;
    const current = ++generation.current;
    setBusy(true); setError("");
    try {
      const result = await call.current("reviewBatchCreate", { params: { pk: projectId },
        body: creation.current.body, headers: { "Idempotency-Key": creation.current.key }, errorFilter: () => true });
      if (current !== generation.current) return;
      if (failed(result)) {
        if (code(result) === "REVIEW_BATCH_PREVIEW_CHANGED") {
          setPreview(null); creation.current = null; setError("changed");
        } else setError("createFailed");
        return;
      }
      setPreview(null); creation.current = null;
      await load(result.id);
    } catch { if (current === generation.current) setError("createFailed"); }
    finally { if (current === generation.current) setBusy(false); }
  };
  const inspectSuccessor = async (id) => {
    const current = ++generation.current;
    const request = ++successorGeneration.current;
    setBusy(true); setError(""); setSuccessor(null);
    try {
      const result = await call.current("submissionDetail", { params: { submissionPk: id }, errorFilter: () => true });
      if (current !== generation.current || request !== successorGeneration.current) return;
      if (failed(result)) setError("loadFailed");
      else setSuccessor(result);
    } catch { if (current === generation.current && request === successorGeneration.current) setError("loadFailed"); }
    finally { if (current === generation.current) setBusy(false); }
  };

  const progress = detail ? batchProgress(detail.items) : null;
  const visibleItems = detail ? filterBatchItems(detail.items, filter) : [];
  const controlsBusy = busy || progressBusy;
  const reviewEnabled = detail?.items.some((i) => i.can_review) ?? false;
  const detailContext = selection.current;
  return <div className={cn("review-batches").toClassName()} data-testid="review-batches">
    <p>{t("boundary")}</p>
    {error && <p role="alert">{t(error)}</p>}
    {savedReview && <p role="status">{t("reviewSaved")}</p>}
    {progressBusy && <p role="status">{t("refreshingProgress")}</p>}
    <Button disabled={controlsBusy} onClick={() => selectedBatchId === null ? load(null, page) : refreshProgress(selectedBatchId)}>{t("refresh")}</Button>
    {canCreate && <fieldset disabled={controlsBusy}>
      <legend>{t("newBatch")}</legend>
      <label>{t("name")}<input value={form.name} onChange={(e) => change("name", e.target.value)} /></label>
      <label>{t("rate")}<input type="number" min="1" max="100" value={form.rate} onChange={(e) => change("rate", e.target.value)} /></label>
      <label><input type="checkbox" checked={form.per_submitter} onChange={(e) => change("per_submitter", e.target.checked)} />{t("perSubmitter")}</label>
      <label>{t("seed")}<input value={form.seed} onChange={(e) => change("seed", e.target.value)} /></label>
      <label>{t("packages")}<input value={form.package_ids} onChange={(e) => change("package_ids", e.target.value)} /></label>
      <label>{t("submitters")}<input value={form.submitter_ids} onChange={(e) => change("submitter_ids", e.target.value)} /></label>
      <Button onClick={requestPreview}>{t("preview")}</Button>
      {preview && <div data-testid="review-batch-preview">
        <p>{t("sampleCount", { selected: preview.plan.candidates.filter((i) => i.selected).length, total: preview.plan.candidates.length })}</p>
        <code>{preview.plan_hash}</code>
        <Button onClick={create}>{t("create")}</Button>
      </div>}
    </fieldset>}
    <nav aria-label={t("title")}>
      {selectedBatchId !== null && <Button disabled={controlsBusy} onClick={() => load(null, page)}>{t("backToBatches")}</Button>}
      {selectedBatchId === null && !busy && !error && !batches.length && <p>{t("noBatches")}</p>}
      {batches.map((b) => <Button key={b.id} disabled={controlsBusy} onClick={() => load(b.id, page)}>{b.name}</Button>)}
      <Button disabled={controlsBusy || page <= 1} onClick={() => load(null, page - 1)}>{t("previous")}</Button>
      <Button disabled={controlsBusy || !next} onClick={() => load(null, page + 1)}>{t("next")}</Button>
    </nav>
    {detail && <section>
      <h3>{detail.name}</h3>
      {detail.role === "reviewer" && <p>{t("visibleScope")}</p>}
      <p>{t("sampleCount", { selected: detail.selected_count, total: detail.candidate_count })}
        {" · "}{t("actualRate", { rate: detail.candidate_count ? (100 * detail.selected_count / detail.candidate_count).toFixed(1) : "—" })}</p>
      <code>{detail.plan_hash}</code>
      <p>{t("asOf", { time: detail.as_of })}</p>
      <p>{t("rule", { rate: detail.rule.rate, seed: detail.rule.seed, algorithm: detail.algorithm })}</p>
      <dl className="review-batches__progress" data-testid="review-batch-progress">
        <div><dt>{t("selectedTotal")}</dt><dd>{progress.selected}</dd></div>
        <div><dt>{t("uncheckedTotal")}</dt><dd>{progress.unchecked}</dd></div>
        <div><dt>{t("selectedPending")}</dt><dd>{progress.pending}</dd></div>
        <div><dt>{t("selectedApproved")}</dt><dd>{progress.approved}</dd></div>
        <div><dt>{t("selectedRejected")}</dt><dd>{progress.rejected}</dd></div>
        <div><dt>{t("selectedSuperseded")}</dt><dd>{progress.superseded}</dd></div>
        <div><dt>{t("selectedUnavailable")}</dt><dd>{progress.unavailable}</dd></div>
      </dl>
      <p>{t("progressBoundary")}</p>
      {detail.role === "reviewer" && reviewEnabled &&
        <Button disabled={controlsBusy} onClick={() => setReviewing((value) => !value)}>{t("review")}</Button>}
      {detail.role === "reviewer" && !reviewEnabled &&
        <p>{t(progress.pending ? "reviewUnavailable" : "noReviewableItems")}</p>}
      {reviewing && <ReviewerWorkspace projectId={projectId} batchId={detail.id}
        reviewEnabled={reviewEnabled} onReviewSettled={(saved) => refreshProgress(detail.id, saved, detailContext)} />}
      <div role="group" aria-label={t("filters")} className="review-batches__filters">
        <Button disabled={controlsBusy} aria-pressed={filter === "all"} onClick={() => setFilter("all")}>{t("allItems")}</Button>
        <Button disabled={controlsBusy} aria-pressed={filter === "selectedPending"} onClick={() => setFilter("selectedPending")}>{t("selectedPending")}</Button>
        <Button disabled={controlsBusy} aria-pressed={filter === "selectedReviewed"} onClick={() => setFilter("selectedReviewed")}>{t("selectedReviewed")}</Button>
        <Button disabled={controlsBusy} aria-pressed={filter === "reworkRelated"} onClick={() => setFilter("reworkRelated")}>{t("reworkRelated")}</Button>
        <Button disabled={controlsBusy} aria-pressed={filter === "unselected"} onClick={() => setFilter("unselected")}>{t("uncheckedTotal")}</Button>
      </div>
      {!visibleItems.length && <p>{t("noMatchingItems")}</p>}
      <table>
        <thead><tr><th>{t("revision")}</th><th>{t("selection")}</th><th>{t("status")}</th><th>{t("rework")}</th></tr></thead>
        <tbody>{visibleItems.map((i) => <tr key={i.submission_id}>
          <td>#{i.submission_id} / {i.revision}</td>
          <td>{t(i.selected ? "selected" : "unchecked")}</td>
          <td>{t(i.status)}{i.review_reason && <p>{i.review_reason}</p>}</td>
          <td>{i.successor ? <><p>{t("successor", { id: i.successor.id, revision: i.successor.revision })}</p>
            <p>{t("successorStatus")}: {t(["pending", "approved", "rejected", "superseded"].includes(i.successor.status) ? i.successor.status : "unknownStatus")}</p>
            <Button disabled={controlsBusy} onClick={() => inspectSuccessor(i.successor.id)}>{t("inspectSuccessor")}</Button>
          </> : t(i.status === "rejected" ? "noSuccessor" : "notRework")}</td>
        </tr>)}</tbody>
      </table>
      {successor && <section data-testid="review-batch-successor">
        <h4>{t("successor", { id: successor.id, revision: successor.revision })}</h4>
        <p>{t("successorReadOnly")}</p><code>{successor.result_hash}</code>
        <pre>{JSON.stringify(successor.result_snapshot, null, 2)}</pre>
      </section>}
    </section>}
  </div>;
};
