import { useCallback, useEffect, useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { cn } from "../../utils/bem";
import { ReviewerWorkspace } from "./ReviewerWorkspace";
import { ReworkComparison } from "./ReworkComparison";
import { ReviewArbitrationHandoff } from "./ReviewArbitrationHandoff";
import { ReviewPrioritySelector, ReviewPrioritySummary, validPriorityChoice, validPriorityPreview, isNerPriority } from "./ReviewPriority";
import { batchProgress, filterBatchItems } from "./reviewBatchProgress";
import { validBatchCapability, validBatchDetail, validBatchList } from "./reviewBatchResponses";
import "./ReviewBatches.scss";

// The shared proxy returns an unparsed native Response when fetch rejects.
// These endpoints return JSON projections, never a numeric HTTP status field.
const failed = (r) => !r || r.error || r.$meta?.ok === false || typeof r.status === "number";
const failedDetail = (r, id) => failed(r) || !validBatchDetail(r, id);
const priorityViewMatches = (r, capability) => {
  const permission = r?.algorithm === "ner-conflict-ranked/v1" ? "can_read_ner_priority"
    : r?.algorithm === "classification-conflict-ranked/v1" ? "can_read_priority" : null;
  return !permission || (capability?.[permission] === true && typeof r.viewer_hash === "string"
    && r.viewer_hash === capability.viewer_hash);
};
const code = (r) => r?.response?.display_context?.reason ?? r?.display_context?.reason;
const priorityRejection = (r) => ({
  REVIEW_BATCH_PREVIEW_CHANGED: 409, REVIEW_PRIORITY_MANIFEST_CHANGED: 409,
  REVIEW_PRIORITY_SOURCE_UNAVAILABLE: 409, CLASSIFICATION_BATCH_SOURCE_INVALID: 409,
  CLASSIFICATION_BATCH_MANIFEST_INVALID: 409, REVIEW_BATCH_TOO_LARGE: 422,
  REVIEW_PRIORITY_TOO_LARGE: 422, CLASSIFICATION_BATCH_TOO_LARGE: 422,
  REVIEW_NER_PRIORITY_TOO_LARGE: 422, NER_TOO_LARGE: 422, NER_BATCH_TOO_LARGE: 422,
  NER_BATCH_MANIFEST_INVALID: 409, NER_BATCH_RESULT_CHANGED: 409, NER_SOURCE_INTEGRITY_FAILED: 409,
  REVIEW_BATCH_SOURCE_INVALID: 409, REVIEW_NER_PRIORITY_UNAVAILABLE: 403,
}[code(r)] === r?.$meta?.status && r?.$meta?.status !== undefined);

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
      .then((r) => { if (!disposed) setEnabled(!failed(r) && validBatchCapability(r)); }).catch(() => {});
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
  const comparisonTrigger = useRef(null);
  const returnFocus = useRef(false);
  const root = useRef(null);
  const [batches, setBatches] = useState([]);
  const [detail, setDetail] = useState(null);
  const [canCreate, setCanCreate] = useState(false);
  const [canPrioritize, setCanPrioritize] = useState(false);
  const [canPrioritizeNer, setCanPrioritizeNer] = useState(false);
  const viewer = useRef(null);
  const nerScope = useRef(false);
  const nerConfirmed = useRef(false);
  const [canReadHandoffs, setCanReadHandoffs] = useState(false), [handoff, setHandoff] = useState(null);
  const [canReadNerHandoffs, setCanReadNerHandoffs] = useState(false);
  const handoffReturn = useRef(null);
  const [priorityChoice, setPriorityChoice] = useState(null);
  const [creationUnknown, setCreationUnknown] = useState(false);
  const [createdNotice, setCreatedNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);
  const [next, setNext] = useState(false);
  const [form, setForm] = useState({ name: "", rate: 10, per_submitter: false, seed: "review-v1", package_ids: "", submitter_ids: "" });
  const [preview, setPreview] = useState(null);
  const [reviewing, setReviewing] = useState(false);
  const [comparison, setComparison] = useState(null);
  const [selectedBatchId, setSelectedBatchId] = useState(null);
  const [progressBusy, setProgressBusy] = useState(false);
  const [savedReview, setSavedReview] = useState(false);
  const [filter, setFilter] = useState("all");

  nerScope.current = isNerPriority(priorityChoice) || isNerPriority(creation.current?.body?.priority)
    || detail?.algorithm === "ner-conflict-ranked/v1" || nerConfirmed.current;
  const canUsePriority = isNerPriority(priorityChoice) ? canPrioritizeNer : canPrioritize;
  const acceptViewer = (capability, forceNer = false) => {
    const next = capability?.viewer_hash;
    if ((forceNer || nerScope.current) && (typeof next !== "string" || !/^[0-9a-f]{64}$/.test(next)
      || (viewer.current && viewer.current !== next))) {
      creation.current = null; nerConfirmed.current = false; setCreationUnknown(false); setCreatedNotice(null);
      setPriorityChoice(null); setPreview(null); setDetail(null); setBatches([]); setReviewing(false);
      setCanCreate(false); setCanPrioritize(false); setCanPrioritizeNer(false);
      if (typeof next === "string") viewer.current = next;
      return false;
    }
    if (typeof next === "string") viewer.current = next;
    return true;
  };
  const load = useCallback(async (id = null, targetPage = 1, targetFilter = "all") => {
    const current = ++generation.current;
    returnFocus.current = false;
    selection.current = { id, projectId };
    setSelectedBatchId(id); setFilter(targetFilter); setSavedReview(false); setProgressBusy(false); setCanReadHandoffs(false); setCanReadNerHandoffs(false);
    setBusy(true); setError(""); setDetail(null); setReviewing(false); setBatches([]); setComparison(null);
    try {
      const [list, capability, selected] = await Promise.all([
        call.current("reviewBatches", { params: { pk: projectId, page: targetPage }, errorFilter: () => true }),
        call.current("reviewBatchCapability", { params: { pk: projectId }, errorFilter: () => true }),
        id === null ? Promise.resolve(null) : call.current("reviewBatchDetail", {
          params: { pk: projectId, batch: id }, errorFilter: () => true,
        }),
      ]);
      if (current !== generation.current) return;
      if (!failed(capability) && validBatchCapability(capability)
        && !acceptViewer(capability, selected?.algorithm === "ner-conflict-ranked/v1")) throw new Error("identity changed");
      if (failed(list) || !validBatchList(list) || failed(capability) || !validBatchCapability(capability)
        || (id !== null && (failedDetail(selected, id) || !priorityViewMatches(selected, capability)))) {
        setCanCreate(false); setCanPrioritize(false); setCanPrioritizeNer(false); setPreview(null);
        if (!creation.current?.unknown) creation.current = null;
        setError("loadFailed"); return;
      }
      setBatches(list.results?.batches ?? []);
      setCanCreate(capability.can_create === true);
      setCanPrioritize(capability.can_prioritize === true); setCanPrioritizeNer(capability.can_prioritize_ner === true);
      setCanReadHandoffs(capability.can_read_handoffs === true);
      setCanReadNerHandoffs(capability.can_read_ner_handoffs === true);
      setPage(targetPage); setNext(Boolean(list.next)); setDetail(selected);
    } catch {
      if (current === generation.current) { setError("loadFailed"); setCanCreate(false); setCanPrioritize(false); setCanPrioritizeNer(false); setPreview(null);
        if (!creation.current?.unknown) creation.current = null; }
    } finally { if (current === generation.current) setBusy(false); }
  }, [projectId]);

  // Refresh only the detail: keep the reviewer queue, selection and batch context mounted.
  const refreshProgress = async (id, reviewSaved = null, context = selection.current) => {
    if (!context || context !== selection.current) return;
    const current = generation.current;
    const request = ++progressGeneration.current;
    const isCurrent = () => current === generation.current && request === progressGeneration.current;
    setProgressBusy(true); setError(""); setComparison(null);
    setCanReadHandoffs(false); setCanReadNerHandoffs(false);
    if (reviewSaved !== null) setSavedReview(reviewSaved);
    try {
      const [result, capability] = await Promise.all([
        call.current("reviewBatchDetail", { params: { pk: projectId, batch: id }, errorFilter: () => true }),
        call.current("reviewBatchCapability", { params: { pk: projectId }, errorFilter: () => true }),
      ]);
      if (!isCurrent()) return;
      if (!failed(capability) && validBatchCapability(capability)
        && !acceptViewer(capability, result?.algorithm === "ner-conflict-ranked/v1")) throw new Error("identity changed");
      if (failedDetail(result, id) || failed(capability) || !validBatchCapability(capability)
        || !priorityViewMatches(result, capability)) throw new Error("detail unavailable");
      setDetail(result);
      setCanCreate(capability.can_create === true);
      setCanPrioritize(capability.can_prioritize === true); setCanPrioritizeNer(capability.can_prioritize_ner === true);
      setCanReadHandoffs(capability.can_read_handoffs === true);
      setCanReadNerHandoffs(capability.can_read_ner_handoffs === true);
    } catch {
      if (!isCurrent()) return;
      // A failed read cannot establish current authority. Keep only the retry context.
      setDetail(null); setReviewing(false); setBatches([]);
      setCanCreate(false); setCanPrioritize(false); setCanPrioritizeNer(false); setPreview(null);
      if (!creation.current?.unknown) creation.current = null;
      setError(reviewSaved ? "savedRefreshFailed" : "progressRefreshFailed");
    } finally { if (isCurrent()) setProgressBusy(false); }
  };

  useEffect(() => {
    setPreview(null); creation.current = null; setCreationUnknown(false); setCreatedNotice(null); setPriorityChoice(null);
    viewer.current = null; nerConfirmed.current = false; setHandoff(null); handoffReturn.current = null; load();
    return () => { generation.current += 1; selection.current = null; };
  }, [load]);

  const change = (key, value) => {
    if (creation.current?.unknown) return;
    setForm((prev) => ({ ...prev, [key]: value }));
    setPreview(null); creation.current = null; setError("");
  };
  useEffect(() => {
    if (canUsePriority || creation.current?.unknown || priorityChoice === null) return;
    setPriorityChoice(null); setPreview(null); creation.current = null;
  }, [canPrioritize, canPrioritizeNer, priorityChoice]);
  const requestPreview = async () => {
    if (creation.current?.unknown) return;
    if (priorityChoice !== null && (!canUsePriority || !validPriorityChoice(priorityChoice))) { setError("priorityChoose"); return; }
    setBusy(true); setError(""); setPreview(null); creation.current = null;
    const current = ++generation.current;
    try {
      const ids = (value) => value.trim() ? value.split(/[\s,]+/).map(Number) : [];
      const body = { ...form, rate: Number(form.rate), package_ids: ids(form.package_ids), submitter_ids: ids(form.submitter_ids) };
      if (priorityChoice !== null) body.priority = priorityChoice;
      let expectedViewer = null;
      if (isNerPriority(body.priority)) {
        const capability = await call.current("reviewBatchCapability", { params: { pk: projectId }, errorFilter: () => true });
        if (current !== generation.current) return;
        if (failed(capability) || !validBatchCapability(capability) || !acceptViewer(capability, true)
          || capability.can_create !== true || capability.can_prioritize_ner !== true) throw new Error("authority changed");
        expectedViewer = capability.viewer_hash;
      }
      const result = await call.current("reviewBatchPreview", { params: { pk: projectId }, body, errorFilter: () => true });
      if (isNerPriority(body.priority)) {
        const capability = await call.current("reviewBatchCapability", { params: { pk: projectId }, errorFilter: () => true });
        if (current !== generation.current) return;
        if (failed(capability) || !validBatchCapability(capability) || !acceptViewer(capability, true)
          || capability.viewer_hash !== expectedViewer || capability.can_prioritize_ner !== true
          || (!failed(result) && result.viewer_hash !== expectedViewer)) throw new Error("authority changed");
      }
      if (current !== generation.current) return;
      if (failed(result)) { setError(code(result) === "REVIEW_BATCH_TOO_LARGE" ? "tooLarge" : "previewFailed"); return; }
      if (body.priority && !validPriorityPreview(result, body.priority)) { setError("previewFailed"); return; }
      creation.current = { body: { ...body, expected_plan_hash: result.plan_hash }, key: uuidv4(), viewerHash: expectedViewer };
      setPreview(result);
    } catch { if (current === generation.current) {
      setError("previewFailed");
      if (isNerPriority(priorityChoice)) { setCanCreate(false); setCanPrioritize(false); setCanPrioritizeNer(false); }
    } }
    finally { if (current === generation.current) setBusy(false); }
  };
  const create = async () => {
    if (!creation.current) return;
    const intent = creation.current;
    let posted = false;
    intent.inFlight = true;
    const current = ++generation.current;
    setBusy(true); setError("");
    try {
      if (isNerPriority(intent.body.priority)) {
        const capability = await call.current("reviewBatchCapability", { params: { pk: projectId }, errorFilter: () => true });
        if (current !== generation.current) return;
        if (failed(capability) || !validBatchCapability(capability) || !acceptViewer(capability, true)
          || capability.viewer_hash !== intent.viewerHash || capability.can_create !== true
          || capability.can_prioritize_ner !== true) throw new Error("authority changed");
      }
      posted = true;
      const result = await call.current("reviewBatchCreate", { params: { pk: projectId },
        body: intent.body, headers: { "Idempotency-Key": intent.key }, errorFilter: () => true });
      if (current !== generation.current) return;
      if (failed(result)) {
        const status = result?.$meta?.status ?? (typeof result?.status === "number" ? result.status : null);
        if (!status || status < 400 || status >= 500) intent.uncertain = true;
        if (!intent.unknown && (intent.body.priority ? priorityRejection(result) : code(result) === "REVIEW_BATCH_PREVIEW_CHANGED")) {
          setPreview(null); creation.current = null; setError("changed");
        } else {
          if (intent.body.priority) { intent.unknown = true; setCreationUnknown(true); }
          setError(intent.body.priority ? "priorityUnknownWrite" : "createFailed");
        }
        return;
      }
      if (intent.body.priority && (!Number.isSafeInteger(result.id) || result.id < 1
        || result.idempotency_key !== intent.key || result.plan_hash !== intent.body.expected_plan_hash
        || (isNerPriority(intent.body.priority) && result.viewer_hash !== intent.viewerHash))) {
        intent.unknown = true; setCreationUnknown(true); setError("priorityUnknownWrite"); return;
      }
      if (intent.body.priority) { nerConfirmed.current = isNerPriority(intent.body.priority); setCreatedNotice(result.id); }
      setCreationUnknown(false);
      setPreview(null); creation.current = null;
      await load(result.id);
    } catch { if (current === generation.current) {
      if (isNerPriority(intent.body.priority) && (!posted || creation.current !== intent)) {
        if (creation.current === intent && !intent.unknown) { creation.current = null; setPreview(null); }
        setCanCreate(false); setCanPrioritizeNer(false); setError(intent.unknown && creation.current === intent ? "priorityUnknownWrite" : "loadFailed");
        return;
      }
      intent.uncertain = true;
      if (intent.body.priority) { intent.unknown = true; setCreationUnknown(true); }
      setError(intent.body.priority ? "priorityUnknownWrite" : "createFailed");
    } }
    finally { intent.inFlight = false; if (current === generation.current) setBusy(false); }
  };

  useEffect(() => {
    const blur = () => {
      const intent = creation.current;
      // Pending reads have no rendered detail/creation intent yet. Revoke their
      // generations too, so a late response cannot republish evidence after blur.
      if (intent?.inFlight || intent?.unknown || intent?.uncertain) { intent.unknown = true; setCreationUnknown(true); }
      generation.current += 1; progressGeneration.current += 1;
      returnFocus.current = false; setComparison(null);
      setBusy(false); setProgressBusy(false); setDetail(null); setBatches([]); setReviewing(false);
      setCanCreate(false); setCanPrioritize(false); setCanPrioritizeNer(false); setPreview(null);
      setCanReadHandoffs(false); setCanReadNerHandoffs(false);
      if (!intent?.unknown) creation.current = null;
      setError(intent?.unknown ? "priorityUnknownWrite" : "loadFailed");
    };
    window.addEventListener("blur", blur);
    return () => window.removeEventListener("blur", blur);
  }, [projectId]);

  const progress = detail ? batchProgress(detail.items) : null;
  const visibleItems = detail ? filterBatchItems(detail.items, filter) : [];
  const controlsBusy = busy || progressBusy;
  useEffect(() => {
    if (!creationUnknown || controlsBusy) return;
    const recovery = root.current?.querySelector('[data-testid="review-priority-recover"]');
    recovery?.scrollIntoView?.({ block: "nearest" });
    recovery?.focus({ preventScroll: true });
  }, [creationUnknown, controlsBusy]);
  const reviewEnabled = detail?.items.some((i) => i.can_review) ?? false;
  const detailContext = selection.current;
  const previewPriority = preview?.plan?.priority;
  const previewSummary = previewPriority ? {
    conflict_candidates: previewPriority.evidence.filter((e) => e.state === "conflict").length,
    selected_conflicts: preview.plan.candidates.filter((c) => c.selected
      && previewPriority.evidence.some((e) => e.submission_id === c.submission_id && e.state === "conflict")).length,
    unknown_candidates: previewPriority.evidence.filter((e) => e.state === "unknown").length,
  } : null;
  useEffect(() => {
    if (comparison !== null || controlsBusy || !returnFocus.current) return;
    returnFocus.current = false;
    const trigger = comparisonTrigger.current;
    const target = trigger?.isConnected && !trigger.disabled ? trigger : root.current?.querySelector('[data-testid="review-batch-refresh"]');
    target?.focus();
  }, [comparison, controlsBusy]);
  useEffect(() => {
    if (handoff || controlsBusy || !handoffReturn.current?.focus) return;
    const saved = handoffReturn.current; handoffReturn.current = null;
    (root.current?.querySelector(`[data-handoff-source="${saved.source}"]`)
      ?? root.current?.querySelector('[data-testid="review-batch-refresh"]'))?.focus();
  }, [handoff, controlsBusy]);
  if (handoff) return <ReviewArbitrationHandoff projectId={projectId} source={handoff} onBack={() => {
    const saved = handoffReturn.current;
    if (!saved) return;
    saved.focus = true; setHandoff(null); load(saved.batch, saved.page, saved.filter);
  }} />;
  return <div ref={root} className={cn("review-batches").toClassName()} data-testid="review-batches">
    {comparison !== null && detail && <ReworkComparison key={`${projectId}:${detail.id}:${comparison}`}
      projectId={projectId} batchId={detail.id} sourceId={comparison} onClose={() => {
        returnFocus.current = true; setComparison(null); refreshProgress(detail.id);
      }} viewerHash={detail.viewer_hash} />}
    <div hidden={comparison !== null}>
    <p>{t("boundary")}</p>
    {error && <p role="alert">{t(error)}</p>}
    {createdNotice && <p role="status">{t("prioritySaved", { id: createdNotice })}</p>}
    {creationUnknown && <Button data-testid="review-priority-recover" disabled={controlsBusy} onClick={create}>{t("priorityRecover")}</Button>}
    {savedReview && <p role="status">{t("reviewSaved")}</p>}
    {progressBusy && <p role="status">{t("refreshingProgress")}</p>}
    <Button data-testid="review-batch-refresh" disabled={controlsBusy} onClick={() => selectedBatchId === null ? load(null, page) : refreshProgress(selectedBatchId)}>{t("refresh")}</Button>
    {canCreate && <fieldset disabled={controlsBusy || creationUnknown}>
      <legend>{t("newBatch")}</legend>
      <label>{t("name")}<input value={form.name} onChange={(e) => change("name", e.target.value)} /></label>
      <label>{t("rate")}<input type="number" min="1" max="100" value={form.rate} onChange={(e) => change("rate", e.target.value)} /></label>
      <label><input type="checkbox" checked={form.per_submitter} onChange={(e) => change("per_submitter", e.target.checked)} />{t("perSubmitter")}</label>
      <label>{t("seed")}<input value={form.seed} onChange={(e) => change("seed", e.target.value)} /></label>
      <label>{t("packages")}<input value={form.package_ids} onChange={(e) => change("package_ids", e.target.value)} /></label>
      <label>{t("submitters")}<input value={form.submitter_ids} onChange={(e) => change("submitter_ids", e.target.value)} /></label>
      {(canPrioritize || canPrioritizeNer) && <ReviewPrioritySelector projectId={projectId}
        kinds={[...(canPrioritize ? ["classification_conflict"] : []), ...(canPrioritizeNer ? ["ner_conflict"] : [])]}
        value={priorityChoice} onChange={(value) => {
        if (creation.current?.unknown) return;
        setPriorityChoice(value); setPreview(null); creation.current = null; setError("");
      }} />}
      <Button disabled={priorityChoice !== null && !validPriorityChoice(priorityChoice)} onClick={requestPreview}>{t("preview")}</Button>
      {preview && <div data-testid="review-batch-preview">
        <p>{t("sampleCount", { selected: preview.plan.candidates.filter((i) => i.selected).length, total: preview.plan.candidates.length })}</p>
        <code>{preview.plan_hash}</code>
        <ReviewPrioritySummary summary={previewSummary} />
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
      {detail.role === "reviewer" && ["classification-conflict-ranked/v1", "ner-conflict-ranked/v1"].includes(detail.algorithm) && <p>{t("priorityReviewerScope")}</p>}
      <p>{t("sampleCount", { selected: detail.selected_count, total: detail.candidate_count })}
        {" · "}{t("actualRate", { rate: detail.candidate_count ? (100 * detail.selected_count / detail.candidate_count).toFixed(1) : "—" })}</p>
      <code>{detail.plan_hash}</code>
      <p>{t("asOf", { time: detail.as_of })}</p>
      <p>{t("rule", { rate: detail.rule.rate, seed: detail.rule.seed, algorithm: detail.algorithm })}</p>
      <ReviewPrioritySummary summary={detail.priority_summary} />
      {detail.priority_source && <p>{t(detail.algorithm === "ner-conflict-ranked/v1" ? "priorityNerProvenance" : "priorityProvenance", { id: detail.priority_source.batch_id })} <code>{detail.priority_source.manifest_hash}</code></p>}
      <dl className={cn("review-batches").elem("progress").toClassName()} data-testid="review-batch-progress">
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
        viewerHash={detail.viewer_hash}
        reviewEnabled={reviewEnabled} onReviewSettled={(saved) => refreshProgress(detail.id, saved, detailContext)} />}
      <div role="group" aria-label={t("filters")} className={cn("review-batches").elem("filters").toClassName()}>
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
          <td>{t(i.selected ? "selected" : "unchecked")}{i.priority_reason && <p>{t(`priority_${i.priority_reason}`)}</p>}</td>
          <td>{t(i.status)}{i.review_reason && <p>{i.review_reason}</p>}</td>
          <td>{i.successor ? <><p>{t("successor", { id: i.successor.id, revision: i.successor.revision })}</p>
            <p>{t("successorStatus")}: {t(["pending", "approved", "rejected", "superseded"].includes(i.successor.status) ? i.successor.status : "unknownStatus")}</p>

          </> : t(i.status === "rejected" ? "noSuccessor" : "notRework")}
            {i.status === "rejected" && <Button disabled={controlsBusy} onClick={(event) => {
              comparisonTrigger.current = event.currentTarget; setComparison(i.submission_id);
            }}>{t("compareRework")}</Button>}
            {((canReadHandoffs && detail.algorithm === "classification-conflict-ranked/v1")
              || (canReadNerHandoffs && detail.algorithm === "ner-conflict-ranked/v1"))
              && i.selected && i.priority_reason === "frozen_conflict" && <Button data-handoff-source={i.submission_id}
              disabled={controlsBusy || creationUnknown || reviewing} onClick={() => {
                if (creation.current?.inFlight || creation.current?.unknown) return;
                handoffReturn.current = { batch: detail.id, source: i.submission_id, page, filter };
                ++generation.current; ++progressGeneration.current; selection.current = null;
                setDetail(null); setBatches([]); setCanCreate(false); setCanPrioritize(false); setCanPrioritizeNer(false); setCanReadHandoffs(false); setCanReadNerHandoffs(false);
                setPreview(null); creation.current = null;
                setHandoff({ batch: detail.id, source: i.submission_id, pending: i.status === "pending",
                  kind: detail.algorithm === "ner-conflict-ranked/v1" ? "ner" : "classification" });
              }}>{t(detail.algorithm === "ner-conflict-ranked/v1" ? "nerHandoffTitle" : "handoffTitle")}</Button>}
          </td>
        </tr>)}</tbody>
      </table>

    </section>}
    </div>
  </div>;
};
