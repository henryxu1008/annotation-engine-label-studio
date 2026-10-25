import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Typography } from "@humansignal/ui";
import { Spinner } from "../../components/Spinner/Spinner";
import { useAPI } from "../../providers/ApiProvider";
import { cn } from "../../utils/bem";
import { useLocaleTranslation } from "@humansignal/i18n";
import { collaborationErrorCode, displayCollaborationDate, displayCollaborationError, displaySubmissionStatus } from "./collaborationDisplay";
import "./ReviewerWorkspace.scss";

const displayIdentity = (user, t) =>
  user?.email || [user?.first_name, user?.last_name].filter(Boolean).join(" ") || t("unknownSubmitter");

// A scope change must discard all queue, command and notice state together.
export const ReviewerWorkspace = (props) => <ScopedReviewerWorkspace
  key={JSON.stringify([props.projectId, props.packageId ?? null, props.batchId ?? null, props.viewerHash ?? null])} {...props} />;

const ScopedReviewerWorkspace = ({ projectId, packageId = null, batchId = null, onReviewSettled, reviewEnabled = true, viewerHash = null }) => {
  const { locale, t } = useLocaleTranslation("collaboration");
  const { t: tp } = useLocaleTranslation("taskPackages");
  const api = useAPI();
  const callApiRef = useRef(api.callApi);
  callApiRef.current = api.callApi;

  const [pendingSubmissions, setPendingSubmissions] = useState([]);
  const [historySubmissions, setHistorySubmissions] = useState([]);
  const [pendingPage, setPendingPage] = useState(1);
  const [historyPage, setHistoryPage] = useState(1);
  const [pendingHasNext, setPendingHasNext] = useState(false);
  const [pendingHasPrevious, setPendingHasPrevious] = useState(false);
  const [historyHasNext, setHistoryHasNext] = useState(false);
  const [historyHasPrevious, setHistoryHasPrevious] = useState(false);
  const [selectedId, setSelectedId] = useState(null);
  const [rejectReason, setRejectReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [processing, setProcessing] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const generationRef = useRef(0);

  const loadPages = useCallback(async (nextPendingPage, nextHistoryPage) => {
    return Promise.all([
      callApiRef.current(batchId !== null ? "reviewBatchQueue" : "reviewableSubmissions", {
        params: {
          ...(batchId !== null ? { pk: projectId, batch: batchId } : { project: projectId, reviewable: true }),
          page: nextPendingPage,
          page_size: 50,
          ...(packageId !== null ? { task_package: packageId } : {}),
        },
        errorFilter: () => true,
      }),
      packageId !== null || batchId !== null ? Promise.resolve({ results: [] }) : callApiRef.current("projectSubmissions", {
        params: {
          project: projectId,
          history: true,
          page: nextHistoryPage,
          page_size: 50,
        },
        errorFilter: () => true,
      }),
    ]);
  }, [projectId, packageId, batchId]);

  const refresh = useCallback(async (nextPendingPage = pendingPage, nextHistoryPage = historyPage) => {
    const generation = ++generationRef.current;
    setLoading(true);

    // Transport failures are read failures too; never leave a saved review spinning.
    const readPages = async (pending, history) => {
      try { return await loadPages(pending, history); }
      catch { return [{ error: true }, { error: true }]; }
    };
    let [reviewableResult, historyResult] = await readPages(nextPendingPage, nextHistoryPage);

    if (generationRef.current !== generation) return;

    if (
      nextPendingPage > 1 &&
      reviewableResult?.response?.display_context?.reason === "SUBMISSION_PAGE_OUT_OF_RANGE"
    ) {
      const fallbackPendingPage = 1;
      [reviewableResult, historyResult] = await readPages(fallbackPendingPage, nextHistoryPage);
      if (generationRef.current !== generation) return;
      nextPendingPage = fallbackPendingPage;
      setPendingPage(fallbackPendingPage);
    }

    if (
      !reviewableResult ||
      reviewableResult?.error ||
      reviewableResult?.$meta?.ok === false ||
      (viewerHash !== null && reviewableResult?.viewer_hash !== viewerHash) ||
      !historyResult ||
      historyResult?.error ||
      historyResult?.$meta?.ok === false
    ) {
      setPendingSubmissions([]);
      setHistorySubmissions([]);
      setSelectedId(null);
      setPendingHasNext(false);
      setPendingHasPrevious(false);
      setHistoryHasNext(false);
      setHistoryHasPrevious(false);
      setError(collaborationErrorCode(
        reviewableResult?.error || reviewableResult?.$meta?.ok === false ? reviewableResult : historyResult,
        "reviewLoadFailed",
      ));
      setLoading(false);
      return;
    }

    const pendingItems = reviewableResult?.results ?? [];
    const historyItems = (historyResult?.results ?? []).filter((item) => item.status !== "pending");

    setPendingPage(nextPendingPage);
    setHistoryPage(nextHistoryPage);
    setPendingSubmissions(pendingItems);
    setHistorySubmissions(historyItems);
    setPendingHasNext(Boolean(reviewableResult?.next));
    setPendingHasPrevious(Boolean(reviewableResult?.previous));
    setHistoryHasNext(Boolean(historyResult?.next));
    setHistoryHasPrevious(Boolean(historyResult?.previous));
    setSelectedId((current) => {
      if (current && [...pendingItems, ...historyItems].some((item) => item.id === current)) return current;
      return pendingItems[0]?.id ?? historyItems[0]?.id ?? null;
    });
    setLoading(false);
  }, [historyPage, loadPages, pendingPage, projectId, viewerHash]);

  useEffect(() => {
    refresh(pendingPage, historyPage);
    return () => {
      generationRef.current += 1;
    };
  }, [historyPage, pendingPage, refresh]);

  const selected = useMemo(
    () =>
      [...pendingSubmissions, ...historySubmissions].find((submission) => submission.id === selectedId) ?? null,
    [historySubmissions, pendingSubmissions, selectedId],
  );
  const selectedIsReviewable = selected && reviewEnabled
    ? pendingSubmissions.some((submission) => submission.id === selected.id)
    : false;

  useEffect(() => {
    setRejectReason("");
  }, [selectedId]);

  const decide = async (decision) => {
    if (!selectedIsReviewable) return;
    if (decision === "rejected" && !rejectReason.trim()) {
      setError("rejectionReasonRequired");
      return;
    }

    setProcessing(decision);
    setError("");
    setNotice("");
    const generation = generationRef.current;

    let result;
    let outcomeUnknown = false;
    try {
      result = await callApiRef.current("reviewSubmission", {
        params: { submissionPk: selected.id, ...(packageId !== null ? { task_package: packageId } : {}), ...(batchId !== null ? { review_batch: batchId } : {}) },
        body: {
          decision,
          reason: decision === "rejected" ? rejectReason.trim() : "",
        },
        errorFilter: () => true,
      });
    } catch {
      // The response may have been lost after commit. Recover by reading, never replaying.
      result = { error: true };
      outcomeUnknown = true;
    }
    if (generation !== generationRef.current) return;

    if (!result || result?.error || result?.$meta?.ok === false || result.id !== selected.id || result.status !== decision) {
      // The shared proxy can return a native Response on a fetch failure. Only an
      // acknowledgement for this exact submission and decision confirms a save.
      const responseStatus = result?.$meta?.status ?? (typeof result?.status === "number" ? result.status : null);
      outcomeUnknown ||= !responseStatus || responseStatus >= 500 || result?.$meta?.ok === true;
      await Promise.all([refresh(pendingPage, historyPage), onReviewSettled?.(false)]);
      setError(outcomeUnknown ? "reviewOutcomeUnknown" : collaborationErrorCode(result, "reviewSaveFailed"));
      setProcessing(null);
      return;
    }

    setNotice({ id: selected.id, revision: selected.revision, decision });
    setRejectReason("");
    await Promise.all([refresh(pendingPage, historyPage), onReviewSettled?.(true)]);
    setProcessing(null);
  };

  if (loading) {
    return (
      <div className={cn("reviewer-workspace").toClassName()}>
        <Spinner size={32} />
      </div>
    );
  }

  return (
    <div className={cn("reviewer-workspace").toClassName()} data-testid="reviewer-workspace">
      {packageId !== null && <div data-testid="package-review-scope">
        <p>{tp("reviewContext", { id: packageId })}</p>
        <Button size="small" look="outlined" disabled={processing !== null} onClick={() => refresh()}>{tp("refresh")}</Button>
      </div>}
      {notice && (
        <div className={cn("reviewer-workspace").elem("notice").toClassName()} role="status">
          {notice.decision === "approved"
            ? t("reviewApprovedNotice", { id: notice.id, revision: notice.revision })
            : t("reviewRejectedNotice", { id: notice.id, revision: notice.revision })}
        </div>
      )}
      {error && (
        <div className={cn("reviewer-workspace").elem("error").toClassName()} role="alert" data-testid="review-error">
          {displayCollaborationError(error, t)}
        </div>
      )}

      <div className={cn("reviewer-workspace").elem("layout").toClassName()}>
        <aside className={cn("reviewer-workspace").elem("queue").toClassName()}>
          <Typography variant="headline" size="small">{t("pendingReviews")}</Typography>
          {pendingSubmissions.length === 0 ? (
            <div data-testid="review-queue-empty">{t("noPendingSubmissions")}</div>
          ) : (
            pendingSubmissions.map((submission) => (
              <button
                key={submission.id}
                type="button"
                className={cn("reviewer-workspace").elem("queue-item").mod({ active: submission.id === selectedId }).toClassName()}
                onClick={() => { setSelectedId(submission.id); setError(""); }}
                data-testid={`review-submission-${submission.id}`}
              >
                <strong>{t("revisionNumber", { revision: submission.revision })}</strong>
                <span>{displayIdentity(submission.submitted_by, t)}</span>
                <span>#{submission.id}</span>
              </button>
            ))
          )}
          <div className={cn("reviewer-workspace").elem("pagination").toClassName()}>
            <Button
              size="small"
              look="outlined"
              disabled={!pendingHasPrevious || processing !== null}
              onClick={() => {
                setPendingPage((page) => Math.max(1, page - 1));
                setSelectedId(null);
              }}
            >
              {t("previousPending")}
            </Button>
            <span>{t("pageNumber", { page: pendingPage })}</span>
            <Button
              size="small"
              look="outlined"
              disabled={!pendingHasNext || processing !== null}
              onClick={() => {
                setPendingPage((page) => page + 1);
                setSelectedId(null);
              }}
            >
              {t("nextPending")}
            </Button>
          </div>

          {packageId === null && batchId === null && <>
          <Typography variant="headline" size="small">{t("submissionHistory")}</Typography>
          {historySubmissions.length === 0 ? (
            <div data-testid="review-history-empty">{t("noReviewedSubmissions")}</div>
          ) : (
            historySubmissions.map((submission) => (
              <button
                key={submission.id}
                type="button"
                className={cn("reviewer-workspace").elem("queue-item").mod({ active: submission.id === selectedId }).toClassName()}
                onClick={() => { setSelectedId(submission.id); setError(""); }}
                data-testid={`review-history-${submission.id}`}
              >
                <strong>{t("revisionNumber", { revision: submission.revision })}</strong>
                <span>{displaySubmissionStatus(submission.status, t)}</span>
                <span>{displayIdentity(submission.submitted_by, t)}</span>
              </button>
            ))
          )}
          <div className={cn("reviewer-workspace").elem("pagination").toClassName()}>
            <Button
              size="small"
              look="outlined"
              disabled={!historyHasPrevious || processing !== null}
              onClick={() => {
                setHistoryPage((page) => Math.max(1, page - 1));
                setSelectedId(null);
              }}
            >
              {t("previousHistory")}
            </Button>
            <span>{t("pageNumber", { page: historyPage })}</span>
            <Button
              size="small"
              look="outlined"
              disabled={!historyHasNext || processing !== null}
              onClick={() => {
                setHistoryPage((page) => page + 1);
                setSelectedId(null);
              }}
            >
              {t("nextHistory")}
            </Button>
          </div>
          </>}
        </aside>

        <section className={cn("reviewer-workspace").elem("detail").toClassName()}>
          {selected ? (
            <>
              <div className={cn("reviewer-workspace").elem("meta").toClassName()}>
                <Typography variant="headline" size="small">
                  {t("submissionRevision", { id: selected.id, revision: selected.revision })}
                </Typography>
                <span>{t("submittedBy", { user: displayIdentity(selected.submitted_by, t) })}</span>
                <span>{displayCollaborationDate(selected.submitted_at, locale)}</span>
                <span data-testid="review-status">{displaySubmissionStatus(selected.status, t)}</span>
                <code data-testid="review-result-hash">{selected.result_hash}</code>
              </div>

              <div>
                <Typography variant="body" size="medium">{t("immutableSnapshot")}</Typography>
                <pre data-testid="review-result-snapshot">
                  {JSON.stringify(selected.result_snapshot, null, 2)}
                </pre>
              </div>

              {selectedIsReviewable && (
                <>
                  <label htmlFor="review-reject-reason">{t("rejectionReason")}</label>
                  <textarea
                    id="review-reject-reason"
                    data-testid="review-reject-reason"
                    value={rejectReason}
                    onChange={(event) => setRejectReason(event.target.value)}
                    disabled={processing !== null}
                    rows={3}
                  />

                  <div className={cn("reviewer-workspace").elem("actions").toClassName()}>
                    <Button
                      data-testid="review-approve"
                      disabled={processing !== null}
                      waiting={processing === "approved"}
                      onClick={() => decide("approved")}
                    >
                      {t("approve")}
                    </Button>
                    <Button
                      data-testid="review-reject"
                      variant="negative"
                      look="outlined"
                      disabled={processing !== null}
                      waiting={processing === "rejected"}
                      onClick={() => decide("rejected")}
                    >
                      {t("reject")}
                    </Button>
                  </div>
                </>
              )}
            </>
          ) : (
            <div data-testid="review-detail-empty">{t("selectPendingSubmission")}</div>
          )}
        </section>
      </div>
    </div>
  );
};
