import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { cn } from "../../utils/bem";
import { collaborationErrorCode, displayCollaborationError, displaySubmissionStatus } from "./collaborationDisplay";
import "./MyWorkWorkspace.scss";

const myWork = cn("my-work-workspace");

export const displayWorkState = (state, t) => {
  switch (state) {
    case "assigned":
      return t("myWorkAssigned");
    case "in_progress":
      return t("myWorkInProgress");
    case "rework":
      return t("myWorkRework");
    case "submitted":
      return t("myWorkSubmitted");
    case "approved":
      return t("myWorkApproved");
    case "conflict":
      return t("myWorkConflict");
    case "pending":
      return t("myWorkPendingReview");
    default:
      return state;
  }
};

export const MyWorkWorkspace = ({ projectId, onOpenReviewQueue }) => {
  const { t } = useLocaleTranslation("collaboration");
  const api = useAPI();
  const callApi = useRef(api.callApi);
  callApi.current = api.callApi;
  const generation = useRef(0);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState("");

  const load = useCallback(
    async (nextPage = 1, state = "") => {
      const current = ++generation.current;
      setLoading(true);
      // Clear old authorization-derived rows immediately, including on refresh.
      setData(null);
      setError("");
      try {
        const fetchPage = (value) =>
          callApi.current("myWork", {
            params: { pk: projectId, page: value, page_size: 50, ...(state ? { state } : {}) },
            errorFilter: () => true,
          });
        let requestedPage = nextPage;
        let result = await fetchPage(requestedPage);
        if (current !== generation.current) return;
        // Work can disappear after submission/reassignment. Recover an empty
        // page once, without treating revoked access as a pagination problem.
        if (requestedPage > 1 && result?.response?.display_context?.reason === "WORK_PAGE_OUT_OF_RANGE") {
          requestedPage = 1;
          result = await fetchPage(requestedPage);
        }
        if (current !== generation.current) return;
        if (!result || result.error || result.$meta?.ok === false) {
          setError(collaborationErrorCode(result, "myWorkLoadFailed"));
        } else {
          setData(result);
          setPage(requestedPage);
          setFilter(state);
        }
      } catch {
        if (current === generation.current) setError("myWorkLoadFailed");
      } finally {
        if (current === generation.current) setLoading(false);
      }
    },
    [projectId],
  );

  useEffect(() => {
    setPage(1);
    setFilter("");
    load();
    return () => {
      generation.current += 1;
    };
  }, [load]);

  return (
    <div className={myWork.toClassName()}>
      <p>{t("myWorkDescription")}</p>
      <Button disabled={loading} onClick={() => load(page, filter)}>
        {t("myWorkRefresh")}
      </Button>
      {loading && <p role="status">{t("myWorkLoading")}</p>}
      {error && <p role="alert">{displayCollaborationError(error, t)}</p>}
      {data && (
        <>
          {data.kind === "annotation" && (
            <div className={myWork.elem("filters").toClassName()}>
              <Button disabled={loading} aria-pressed={!filter} onClick={() => load(1)}>
                {t("myWorkAll")}
              </Button>
              {Object.entries(data.counts).map(([state, count]) => (
                <Button key={state} disabled={loading} aria-pressed={filter === state} onClick={() => load(1, state)}>
                  {displayWorkState(state, t)} ({count})
                </Button>
              ))}
            </div>
          )}
          {data.kind === "review" && (
            <>
              <p>{t("myWorkReviewCount", { count: data.counts.pending })}</p>
              {onOpenReviewQueue && data.results.some((row) => row.allowed_actions.includes("open_review_queue")) && (
                <Button onClick={onOpenReviewQueue}>{t("myWorkOpenReviewQueue")}</Button>
              )}
            </>
          )}
          {!data.results.length && <p>{t("myWorkEmpty")}</p>}
          <ul className={myWork.elem("rows").toClassName()}>
            {data.results.map((row) => (
              <li key={row.submission_id ?? row.assignment_id}>
                <strong>{t("taskNumber", { id: row.task_id })}</strong>
                <span>{displayWorkState(row.state, t)}</span>
                {row.latest_status && (
                  <span>
                    {t("myWorkPreviousSubmission", { status: displaySubmissionStatus(row.latest_status, t) })}
                  </span>
                )}
                {row.latest_revision && <span>{t("revisionNumber", { revision: row.latest_revision })}</span>}
                {row.revision && <span>{t("revisionNumber", { revision: row.revision })}</span>}
                {row.has_draft && <span>{t("myWorkSavedDraft")}</span>}
                {row.source === "direct_assignment" && <span>{t("myWorkDirectAssignment")}</span>}
                {row.source_cancelled && <span>{t("myWorkSourceCancelled")}</span>}
                {row.rejection_reason && <p>{row.rejection_reason}</p>}
                {row.blocked_reason && (
                  <p>
                    {row.blocked_reason === "WORK_SOURCE_CONFLICT" ? t("myWorkSourceConflict") : t("myWorkReadOnly")}
                  </p>
                )}
                {/* Reuse AsyncPage's native navigation opt-out: query-only
                    changes otherwise leave this modal and data manager alive.
                    Native navigation also preserves the unsaved-work guard. */}
                {row.allowed_actions.includes("open_editor") && row.editor_url && (
                  <a href={row.editor_url} data-external>
                    {t("myWorkOpenEditor")}
                  </a>
                )}
              </li>
            ))}
          </ul>
          <div className={myWork.elem("filters").toClassName()}>
            <Button disabled={!data.previous || loading} onClick={() => load(page - 1, filter)}>
              {t("previous")}
            </Button>
            <span>{t("myWorkPage", { page })}</span>
            <Button disabled={!data.next || loading} onClick={() => load(page + 1, filter)}>
              {t("next")}
            </Button>
          </div>
        </>
      )}
    </div>
  );
};
