import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Typography } from "@humansignal/ui";
import { Spinner } from "../../components/Spinner/Spinner";
import { useAPI } from "../../providers/ApiProvider";
import { cn } from "../../utils/bem";
import { downloadFile } from "../../utils/download";
import { useLocaleTranslation } from "@humansignal/i18n";
import { collaborationErrorCode, displayCollaborationDate, displayCollaborationError, displayReviewDecision, displaySubmissionStatus } from "./collaborationDisplay";
import "./SubmissionReleaseWorkspace.scss";
import { ApprovedDatasetExport } from "./ApprovedDatasetExport";

const displayIdentity = (user, t) =>
  user?.email || [user?.first_name, user?.last_name].filter(Boolean).join(" ") || t("unknownSubmitter");

const stableJson = (value) => JSON.stringify(value);

export const SubmissionReleaseWorkspace = ({ projectId, allowRelease = false }) => {
  const { locale, t } = useLocaleTranslation("collaboration");
  const api = useAPI();
  const callApiRef = useRef(api.callApi);
  callApiRef.current = api.callApi;
  const selectedIdRef = useRef(null);

  const [submissions, setSubmissions] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  selectedIdRef.current = selectedId;
  const [page, setPage] = useState(1);
  const [hasNext, setHasNext] = useState(false);
  const [hasPrevious, setHasPrevious] = useState(false);
  const [loading, setLoading] = useState(true);
  const [releasing, setReleasing] = useState(false);
  const [error, setError] = useState("");
  const [releaseResult, setReleaseResult] = useState(null);
  const generationRef = useRef(0);

  const refresh = useCallback(async (nextPage = page) => {
    const generation = ++generationRef.current;
    setLoading(true);

    let result = await callApiRef.current("projectSubmissions", {
      params: { project: projectId, page: nextPage, page_size: 50 },
      errorFilter: () => true,
    });

    if (generationRef.current !== generation) return;

    while (nextPage > 1 && result?.response?.detail === "Invalid page.") {
      nextPage -= 1;
      result = await callApiRef.current("projectSubmissions", {
        params: { project: projectId, page: nextPage, page_size: 50 },
        errorFilter: () => true,
      });
      if (generationRef.current !== generation) return;
    }

    if (!result || result?.error || result?.$meta?.ok === false) {
      setSubmissions([]);
      setSelectedId(null);
      setHasNext(false);
      setHasPrevious(false);
      setError(collaborationErrorCode(result, "submissionHistoryLoadFailed"));
      setLoading(false);
      return;
    }

    const items = result?.results ?? [];
    setPage(nextPage);
    setSubmissions(items);
    setHasNext(Boolean(result?.next));
    setHasPrevious(Boolean(result?.previous));
    setSelectedId((current) => {
      if (current && items.some((item) => item.id === current)) return current;
      return items[0]?.id ?? null;
    });
    setLoading(false);
  }, [page, projectId]);

  useEffect(() => {
    refresh(page);
    return () => {
      generationRef.current += 1;
    };
  }, [page, refresh]);

  const selected = useMemo(
    () => submissions.find((submission) => submission.id === selectedId) ?? null,
    [selectedId, submissions],
  );

  useEffect(() => {
    setError("");
    setReleaseResult(null);
  }, [selectedId]);

  const release = async () => {
    if (!allowRelease || !selected || selected.status !== "approved") return;

    const releaseSubmissionId = selected.id;
    const generation = generationRef.current;
    const isCurrent = () => selectedIdRef.current === releaseSubmissionId && generationRef.current === generation;
    setReleasing(true);
    setError("");
    setReleaseResult(null);

    try {
      const response = await callApiRef.current("releaseSubmissionRaw", {
        params: { submissionPk: selected.id },
        errorFilter: () => true,
      });
      if (!isCurrent()) return;
      if (!response?.ok) {
        setError(collaborationErrorCode(response, "releaseFailed"));
        return;
      }

      const result = await response.clone().json();
      if (!isCurrent()) return;
      if (
        result.submission_id !== selected.id ||
        result.revision !== selected.revision ||
        result.result_hash !== selected.result_hash ||
        stableJson(result.result_snapshot) !== stableJson(selected.result_snapshot)
      ) {
        setError("releaseMismatch");
        return;
      }

      // Download the server bytes, not JSON.stringify(result): immutable snapshots
      // can contain integers beyond JavaScript's exact Number range.
      const blob = await response.blob();
      if (!isCurrent()) return;
      downloadFile(blob, `submission-${result.submission_id}-revision-${result.revision}.json`);
      setReleaseResult(result);
    } catch {
      if (isCurrent()) setError("releaseFailed");
    } finally {
      if (generationRef.current === generation) setReleasing(false);
    }
  };

  if (loading) {
    return (
      <div className={cn("submission-release-workspace").toClassName()}>
        <Spinner size={32} />
      </div>
    );
  }

  return (
    <div className={cn("submission-release-workspace").toClassName()} data-testid="submission-release-workspace">
      {allowRelease && <ApprovedDatasetExport projectId={projectId} />}
      {error && (
        <div role="alert" data-testid="release-error">
          {displayCollaborationError(error, t)}
        </div>
      )}
      {releaseResult && (
        <div role="status" data-testid="release-success">
          {t("releasedRevision", { revision: releaseResult.revision, hash: releaseResult.result_hash })}
        </div>
      )}

      <div className={cn("submission-release-workspace").elem("layout").toClassName()}>
        <aside className={cn("submission-release-workspace").elem("history").toClassName()}>
          <Typography variant="headline" size="small">{t("submissionHistory")}</Typography>
          {submissions.length === 0 ? (
            <div data-testid="release-history-empty">{t("noSubmissions")}</div>
          ) : (
            submissions.map((submission) => (
              <button
                key={submission.id}
                type="button"
                className={cn("submission-release-workspace").elem("history-item").mod({ active: submission.id === selectedId }).toClassName()}
                onClick={() => setSelectedId(submission.id)}
                data-testid={`release-submission-${submission.id}`}
              >
                <strong>{t("revisionNumber", { revision: submission.revision })}</strong>
                <span>{displaySubmissionStatus(submission.status, t)}</span>
                <span>{displayIdentity(submission.submitted_by, t)}</span>
                <span>{t("taskNumber", { id: submission.result_snapshot?.task?.id ?? "?" })}</span>
              </button>
            ))
          )}

          <div className={cn("submission-release-workspace").elem("pagination").toClassName()}>
            <Button
              size="small"
              look="outlined"
              disabled={!hasPrevious || releasing}
              onClick={() => {
                setSelectedId(null);
                setPage((current) => Math.max(1, current - 1));
              }}
            >
              {t("previous")}
            </Button>
            <span>{t("pageNumber", { page })}</span>
            <Button
              size="small"
              look="outlined"
              disabled={!hasNext || releasing}
              onClick={() => {
                setSelectedId(null);
                setPage((current) => current + 1);
              }}
            >
              {t("next")}
            </Button>
          </div>
        </aside>

        <section className={cn("submission-release-workspace").elem("detail").toClassName()}>
          {selected ? (
            <>
              <Typography variant="headline" size="small">
                {t("submissionRevision", { id: selected.id, revision: selected.revision })}
              </Typography>
              <span data-testid="release-status">{displaySubmissionStatus(selected.status, t)}</span>
              <span>{t("submittedBy", { user: displayIdentity(selected.submitted_by, t) })}</span>
              <span>{displayCollaborationDate(selected.submitted_at, locale)}</span>
              {selected.review && (
                <div data-testid="release-review-metadata">
                  <span>{t("reviewDecision", { decision: displayReviewDecision(selected.review.decision, t) })}</span>
                  <span>{t("reviewerIdentity", { user: displayIdentity(selected.review.reviewer, t) })}</span>
                  {selected.review.reason && <span>{t("reasonText", { reason: selected.review.reason })}</span>}
                </div>
              )}
              <code data-testid="release-result-hash">{selected.result_hash}</code>
              <pre data-testid="release-result-snapshot">{JSON.stringify(selected.result_snapshot, null, 2)}</pre>

              {allowRelease && selected.status === "approved" && (
                <Button
                  data-testid="release-approved-submission"
                  disabled={releasing}
                  waiting={releasing}
                  onClick={release}
                >
                  {t("releaseApprovedRevision")}
                </Button>
              )}
            </>
          ) : (
            <div data-testid="release-detail-empty">{t("selectSubmissionRevision")}</div>
          )}
        </section>
      </div>
    </div>
  );
};
