import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { cn } from "../../utils/bem";
import { collaborationErrorCode, displayCollaborationError, displaySubmissionStatus } from "./collaborationDisplay";
import { TaskPackages } from "./TaskPackages";
import { displayWorkState } from "./workDisplay";
import "./MyWorkWorkspace.scss";
export { displayWorkState } from "./workDisplay";

const myWork = cn("my-work-workspace");

export const MyWorkWorkspace = ({ projectId, onOpenReviewQueue }) => {
  const { t } = useLocaleTranslation("collaboration");
  const { t: tp } = useLocaleTranslation("taskPackages");
  const api = useAPI();
  const callApi = useRef(api.callApi);
  callApi.current = api.callApi;
  const generation = useRef(0);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState("");
  const [packageId, setPackageId] = useState(null);
  const [choosingPackage, setChoosingPackage] = useState(false);

  const load = useCallback(
    async (nextPage = 1, state = "", selectedPackage = null) => {
      const current = ++generation.current;
      setLoading(true);
      // Clear old authorization-derived rows immediately, including on refresh.
      setData(null);
      setError("");
      setPage(nextPage);
      setFilter(state);
      setPackageId(selectedPackage);
      try {
        const fetchPage = (value) =>
          callApi.current("myWork", {
            params: {
              pk: projectId,
              page: value,
              page_size: 50,
              ...(state ? { state } : {}),
              ...(selectedPackage !== null ? { task_package: selectedPackage } : {}),
            },
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
        if (
          !result ||
          result.error ||
          result.$meta?.ok === false ||
          (selectedPackage !== null && result.task_package?.id !== selectedPackage)
        ) {
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
    setPackageId(null);
    setChoosingPackage(false);
    load();
    return () => {
      generation.current += 1;
    };
  }, [load]);

  if (choosingPackage)
    return (
      <div className={myWork.toClassName()}>
        <h3>{tp("choosePackage")}</h3>
        <Button
          onClick={() => {
            setChoosingPackage(false);
            load(page, filter, packageId);
          }}
        >
          {tp("cancelPicker")}
        </Button>
        <TaskPackages
          projectId={projectId}
          onSelectPackage={(item) => {
            if (!Number.isSafeInteger(item.id) || item.id <= 0) return;
            setChoosingPackage(false);
            load(1, "", item.id);
          }}
        />
      </div>
    );

  return (
    <div className={myWork.toClassName()}>
      <p>{t("myWorkDescription")}</p>
      {data?.packages_enabled && (
        <Button
          disabled={loading}
          onClick={() => {
            generation.current += 1;
            setData(null);
            setError("");
            setChoosingPackage(true);
          }}
        >
          {tp("choosePackage")}
        </Button>
      )}
      {packageId !== null && (
        <div className={myWork.elem("filters").toClassName()}>
          <span>
            {data?.task_package
              ? tp("selectedWork", { name: data.task_package.name })
              : tp("selectedWorkId", { id: packageId })}
          </span>
          <Button disabled={loading} onClick={() => load(1)}>
            {tp("clearPackage")}
          </Button>
        </div>
      )}
      <Button disabled={loading} onClick={() => load(page, filter, packageId)}>
        {t("myWorkRefresh")}
      </Button>
      {loading && <p role="status">{t("myWorkLoading")}</p>}
      {error && <p role="alert">{displayCollaborationError(error, t)}</p>}
      {data && (
        <>
          {data.kind === "annotation" && (
            <div className={myWork.elem("filters").toClassName()}>
              <Button
                disabled={loading}
                look={filter ? "outlined" : undefined}
                aria-pressed={!filter}
                onClick={() => load(1, "", packageId)}
              >
                {t("myWorkAll")}
              </Button>
              {Object.entries(data.counts).map(([state, count]) => (
                <Button
                  key={state}
                  disabled={loading}
                  aria-pressed={filter === state}
                  look={filter === state ? undefined : "outlined"}
                  onClick={() => load(1, state, packageId)}
                >
                  {displayWorkState(state, t)} ({count})
                </Button>
              ))}
            </div>
          )}
          {data.kind === "review" && (
            <>
              <p>{t("myWorkReviewCount", { count: data.counts.pending })}</p>
              {packageId !== null && data.results.some((row) => row.allowed_actions.includes("open_review_queue")) && (
                <a href={`/projects/${projectId}/data?task_package=${packageId}&package_review=1`} data-external>
                  {t("myWorkOpenReviewQueue")}
                </a>
              )}
              {packageId === null &&
                onOpenReviewQueue &&
                data.results.some((row) => row.allowed_actions.includes("open_review_queue")) && (
                  <Button onClick={onOpenReviewQueue}>{t("myWorkOpenReviewQueue")}</Button>
                )}
            </>
          )}
          {!filter && data.continue_url && (
            <a href={data.continue_url} data-external>
              {tp("continue")}
            </a>
          )}
          {filter && packageId !== null && data.kind === "annotation" && <p>{tp("filterHint")}</p>}
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
            <Button disabled={!data.previous || loading} onClick={() => load(page - 1, filter, packageId)}>
              {t("previous")}
            </Button>
            <span>{t("myWorkPage", { page })}</span>
            <Button disabled={!data.next || loading} onClick={() => load(page + 1, filter, packageId)}>
              {t("next")}
            </Button>
          </div>
        </>
      )}
    </div>
  );
};
