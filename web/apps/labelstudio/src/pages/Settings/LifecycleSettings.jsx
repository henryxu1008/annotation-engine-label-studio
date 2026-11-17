import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { useProject } from "../../providers/ProjectProvider";
import { apiSucceeded, lifecycleTargets, requiresVerifiedRecovery, resolutionFor } from "./lifecycleState";

const legacyExportTypes = [
  "exports",
  "conversions",
  "releases",
  "release_attempts",
  "snapshots",
  "audit_packages",
  "audit_attempts",
  "storage_local_import",
  "storage_local_export",
  "storage_redis_import",
  "storage_redis_export",
  "storage_s3_import",
  "storage_s3_export",
  "storage_gcs_import",
  "storage_gcs_export",
  "storage_azure_import",
  "storage_azure_export",
];

const validLegacyCounts = (counts) =>
  counts && legacyExportTypes.every((key) => Number.isInteger(counts[key]) && counts[key] >= 0);

const validLegacyDetails = (details) =>
  details &&
  Array.isArray(details.items) &&
  typeof details.possibly_truncated === "boolean" &&
  (details.possibly_truncated
    ? typeof details.next_cursor === "string" && !!details.next_cursor
    : details.next_cursor === null) &&
  details.items.every(
    (item) =>
      legacyExportTypes.includes(item.kind) &&
      typeof item.id === "string" &&
      typeof item.state === "string" &&
      (item.parent_id === null || typeof item.parent_id === "string"),
  );

const validStoragePage = (pending) =>
  pending &&
  Array.isArray(pending.operations) &&
  typeof pending.possibly_truncated === "boolean" &&
  (pending.possibly_truncated
    ? typeof pending.operations_next_cursor === "string" && !!pending.operations_next_cursor
    : pending.operations_next_cursor === null);

export const LifecycleSettings = () => {
  const { t } = useLocaleTranslation("projects");
  const api = useAPI();
  const callRef = useRef(api.callApi);
  callRef.current = api.callApi;
  const { project } = useProject();
  const activeProject = useRef(project?.id);
  activeProject.current = project?.id;
  const sequence = useRef(0);
  const submitting = useRef(false);
  const paging = useRef(null);
  const [state, setState] = useState(null);
  const [operations, setOperations] = useState([]);
  const [legacyExports, setLegacyExports] = useState(null);
  const [legacyDetails, setLegacyDetails] = useState(null);
  const [legacyPage, setLegacyPage] = useState(1);
  const [legacyCursors, setLegacyCursors] = useState([null]);
  const [truncated, setTruncated] = useState(false);
  const [storageInventoryValid, setStorageInventoryValid] = useState(false);
  const [storageNext, setStorageNext] = useState(null);
  const [storagePage, setStoragePage] = useState(1);
  const [storageCursors, setStorageCursors] = useState([null]);
  const [reason, setReason] = useState("");
  const [target, setTarget] = useState("");
  const [selected, setSelected] = useState("");
  const [resolutionReason, setResolutionReason] = useState("");
  const [attested, setAttested] = useState(false);
  const [busy, setBusy] = useState(false);
  const [locked, setLocked] = useState(true);
  const [error, setError] = useState("");

  const refresh = useCallback(async (projectId) => {
    const request = ++sequence.current;
    setBusy(true);
    let lifecycle;
    let pending;
    try {
      [lifecycle, pending] = await Promise.all([
        callRef.current("projectLifecycle", { params: { pk: projectId }, errorFilter: () => true }),
        callRef.current("projectStorageOperations", { params: { pk: projectId }, errorFilter: () => true }),
      ]);
    } catch (_) {
      lifecycle = pending = null;
    }
    if (activeProject.current !== projectId || sequence.current !== request) return;
    setBusy(false);
    if (!apiSucceeded(lifecycle) || !apiSucceeded(pending) || !Array.isArray(pending.operations)) {
      setState(null);
      setLocked(true);
      setError("lifecycleLoadFailed");
      return;
    }
    setState(lifecycle);
    setOperations(pending.operations);
    setStorageInventoryValid(Boolean(validStoragePage(pending)));
    setStorageNext(validStoragePage(pending) ? pending.operations_next_cursor : null);
    setStoragePage(1);
    setStorageCursors([null]);
    const legacy = pending.legacy_export_blockers;
    setLegacyExports(validLegacyCounts(legacy) ? legacy : null);
    const details = pending.legacy_export_details;
    setLegacyDetails(validLegacyDetails(details) ? details : null);
    setLegacyPage(1);
    setLegacyCursors([null]);
    setTruncated(Boolean(pending.possibly_truncated));
    setSelected("");
    setAttested(false);
    setTarget("");
    setLocked(false);
    setError("");
  }, []);

  useEffect(() => {
    paging.current = null;
    setState(null);
    setOperations([]);
    setStorageInventoryValid(false);
    setStorageNext(null);
    setStoragePage(1);
    setStorageCursors([null]);
    setLegacyExports(null);
    setLegacyDetails(null);
    setLegacyPage(1);
    setLegacyCursors([null]);
    setReason("");
    setResolutionReason("");
    setLocked(true);
    if (project?.id) refresh(project.id);
    return () => {
      sequence.current += 1;
    };
  }, [project?.id, refresh]);

  const loadLegacyPage = async (cursor, page) => {
    if (busy || locked || paging.current !== null || !project?.id || page < 1 || (page > 1 && !cursor)) return;
    const projectId = project.id;
    const request = ++sequence.current;
    paging.current = request;
    setBusy(true);
    let pending;
    try {
      const storageCursor = storageCursors[storagePage - 1];
      pending = await callRef.current("projectStorageOperations", {
        params: {
          pk: projectId,
          ...(cursor ? { legacy_cursor: cursor } : {}),
          ...(storageCursor ? { storage_cursor: storageCursor } : {}),
        },
        errorFilter: () => true,
      });
    } catch (_) {
      pending = null;
    }
    if (paging.current === request) paging.current = null;
    if (activeProject.current !== projectId || sequence.current !== request) return;
    setBusy(false);
    if (
      !apiSucceeded(pending) ||
      !validStoragePage(pending) ||
      !validLegacyCounts(pending.legacy_export_blockers) ||
      !validLegacyDetails(pending.legacy_export_details) ||
      (cursor && pending.legacy_export_details.next_cursor === cursor)
    ) {
      setLegacyDetails(null);
      setLocked(true);
      setError("lifecycleLoadFailed");
      return;
    }
    setOperations(pending.operations);
    setStorageInventoryValid(true);
    setStorageNext(pending.operations_next_cursor);
    setTruncated(Boolean(pending.possibly_truncated));
    setLegacyExports(pending.legacy_export_blockers);
    setLegacyDetails(pending.legacy_export_details);
    setLegacyPage(page);
    setLegacyCursors((cursors) => [...cursors.slice(0, page - 1), cursor]);
    setSelected("");
    setAttested(false);
    setTarget("");
    setError("");
  };

  const loadStoragePage = async (cursor, page) => {
    if (busy || locked || paging.current !== null || !project?.id || page < 1 || (page > 1 && !cursor)) return;
    const projectId = project.id;
    const request = ++sequence.current;
    paging.current = request;
    setBusy(true);
    let pending;
    try {
      const legacyCursor = legacyCursors[legacyPage - 1];
      pending = await callRef.current("projectStorageOperations", {
        params: {
          pk: projectId,
          ...(cursor ? { storage_cursor: cursor } : {}),
          ...(legacyCursor ? { legacy_cursor: legacyCursor } : {}),
        },
        errorFilter: () => true,
      });
    } catch (_) {
      pending = null;
    }
    if (paging.current === request) paging.current = null;
    if (activeProject.current !== projectId || sequence.current !== request) return;
    setBusy(false);
    if (
      !apiSucceeded(pending) ||
      !validStoragePage(pending) ||
      !validLegacyCounts(pending.legacy_export_blockers) ||
      !validLegacyDetails(pending.legacy_export_details) ||
      (cursor && pending.operations_next_cursor === cursor)
    ) {
      setStorageInventoryValid(false);
      setLocked(true);
      setError("lifecycleLoadFailed");
      return;
    }
    setOperations(pending.operations);
    setTruncated(pending.possibly_truncated);
    setStorageInventoryValid(true);
    setStorageNext(pending.operations_next_cursor);
    setStoragePage(page);
    setStorageCursors((cursors) => [...cursors.slice(0, page - 1), cursor]);
    setLegacyExports(pending.legacy_export_blockers);
    setLegacyDetails(pending.legacy_export_details);
    setSelected("");
    setAttested(false);
    setTarget("");
    setError("");
  };

  const mutate = async (method, body, params) => {
    if (submitting.current || locked || !project?.id) return false;
    const projectId = project.id;
    const request = sequence.current;
    submitting.current = true;
    setBusy(true);
    setError("");
    let result;
    try {
      result = await callRef.current(method, { params: { pk: projectId, ...params }, body, errorFilter: () => true });
    } catch (_) {
      result = null;
    }
    submitting.current = false;
    if (activeProject.current !== projectId || request !== sequence.current) return false;
    setBusy(false);
    if (!apiSucceeded(result)) {
      setLocked(true);
      setError("lifecycleOutcomeUnknown");
      return false;
    }
    await refresh(projectId);
    return true;
  };

  const transition = async (event) => {
    event.preventDefault();
    if (
      !state ||
      !storageInventoryValid ||
      hasStorageOperations ||
      !legacyExports ||
      !legacyDetails ||
      hasLegacyExports ||
      legacyPage > 1 ||
      !lifecycleTargets[state.state]?.includes(target) ||
      !reason.trim()
    )
      return;
    const projectId = project.id;
    if (
      await mutate("projectLifecycleTransition", { target, expected_revision: state.revision, reason: reason.trim() })
    ) {
      if (activeProject.current === projectId) setReason("");
    }
  };
  const operation = operations.find((entry) => String(entry.id) === selected);
  // A later page does not prove earlier ledger entries were resolved.
  const hasStorageOperations = storagePage > 1 || operations.length > 0 || truncated;
  const hasLegacyExports = Boolean(
    (legacyExports && legacyExportTypes.some((key) => legacyExports[key] > 0)) ||
      (legacyDetails && (legacyDetails.items.length > 0 || legacyDetails.possibly_truncated)),
  );
  const resolution = operation && resolutionFor(operation);
  const noAttestationNeeded = [
    "cancel_queued",
    "cancel_s3_preflight",
    "recover_webhook_pre_send",
    "recover_storage_pre_effect",
    "recover_s3_delete_pre_effect",
  ].includes(resolution);
  const resolve = async (event) => {
    event.preventDefault();
    if (!resolution || !resolutionReason.trim() || (!noAttestationNeeded && !attested)) return;
    const projectId = project.id;
    if (
      (await mutate(
        "projectStorageOperationResolve",
        {
          action: resolution,
          reason: resolutionReason.trim(),
          attested: noAttestationNeeded ? false : attested,
        },
        { operation: operation.id },
      )) &&
      activeProject.current === projectId
    )
      setResolutionReason("");
  };

  return (
    <div className="settings-wrapper">
      <h1>{t("lifecycleTitle")}</h1>
      <p>{t("lifecycleDescription")}</p>
      {busy && <p role="status">{t("lifecycleLoading")}</p>}
      <Button type="button" look="outlined" disabled={busy || !project?.id} onClick={() => refresh(project.id)}>
        {t("lifecycleRefresh")}
      </Button>
      {error && <p role="alert">{t(error)}</p>}
      {state && (
        <>
          <p>
            {t("lifecycleCurrent")}: <strong>{t(`lifecycleState_${state.state}`)}</strong> ({t("lifecycleRevision")}:{" "}
            {state.revision})
          </p>
          <form onSubmit={transition}>
            <label htmlFor="lifecycle-target">{t("lifecycleTarget")}</label>{" "}
            <select
              id="lifecycle-target"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              disabled={busy || locked}
            >
              <option value="">{t("lifecycleChoose")}</option>
              {(lifecycleTargets[state.state] ?? []).map((value) => (
                <option key={value} value={value}>
                  {t(`lifecycleState_${value}`)}
                </option>
              ))}
            </select>{" "}
            <label htmlFor="lifecycle-reason">{t("lifecycleReason")}</label>{" "}
            <input
              id="lifecycle-reason"
              maxLength={2000}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              disabled={busy || locked}
            />{" "}
            <Button
              type="submit"
              disabled={
                busy ||
                locked ||
                !storageInventoryValid ||
                hasStorageOperations ||
                !legacyExports ||
                !legacyDetails ||
                hasLegacyExports ||
                legacyPage > 1 ||
                !target ||
                !reason.trim()
              }
            >
              {t("lifecycleApply")}
            </Button>
          </form>
          {(storagePage > 1 || legacyPage > 1) && <p role="status">{t("lifecycleRefreshBeforeTransition")}</p>}
          <h2>{t("lifecyclePending")}</h2>
          <h3>{t("lifecycleLegacyExports")}</h3>
          {!legacyExports ? (
            <p role="status">{t("lifecycleLegacyExportsUnknown")}</p>
          ) : hasLegacyExports ? (
            <>
              <p>{t("lifecycleLegacyExportsBlocked")}</p>
              <ul>
                {legacyExportTypes
                  .filter((key) => legacyExports[key] > 0)
                  .map((key) => (
                    <li key={key}>
                      {t(`lifecycleLegacyExport_${key}`)}: {legacyExports[key]}
                    </li>
                  ))}
              </ul>
              <h4>{t("lifecycleLegacyDetails")}</h4>
              {!legacyDetails ? (
                <p role="status">{t("lifecycleLegacyDetailsUnknown")}</p>
              ) : (
                <>
                  <p>
                    {t("lifecycleLegacyPage")}: {legacyPage}
                  </p>
                  {legacyDetails.possibly_truncated && <p role="status">{t("lifecycleLegacyDetailsTruncated")}</p>}
                  {!legacyDetails.items.length && <p role="status">{t("lifecycleLegacyDetailsChanged")}</p>}
                  <ul>
                    {legacyDetails.items.map((item) => (
                      <li key={`${item.kind}:${item.id}`}>
                        {t(`lifecycleLegacyExport_${item.kind}`)} #{item.id} · {item.state}
                        {item.parent_id ? ` · ${t("lifecycleLegacyParent")} #${item.parent_id}` : ""}
                      </li>
                    ))}
                  </ul>
                  {legacyPage > 1 && (
                    <Button
                      type="button"
                      disabled={busy || locked}
                      onClick={() => loadLegacyPage(legacyCursors[legacyPage - 2], legacyPage - 1)}
                    >
                      {t("lifecycleLegacyPrevious")}
                    </Button>
                  )}
                  {legacyDetails.next_cursor && (
                    <Button
                      type="button"
                      disabled={busy || locked}
                      onClick={() => loadLegacyPage(legacyDetails.next_cursor, legacyPage + 1)}
                    >
                      {t("lifecycleLegacyNext")}
                    </Button>
                  )}
                </>
              )}
            </>
          ) : (
            <>
              <p>{t("lifecycleLegacyExportsNone")}</p>
              {!legacyDetails && <p role="status">{t("lifecycleLegacyDetailsUnknown")}</p>}
            </>
          )}
          {!storageInventoryValid && <p role="status">{t("lifecycleStorageInventoryUnknown")}</p>}
          {truncated && <p role="status">{t("lifecycleTruncated")}</p>}
          <p>
            {t("lifecycleStoragePage")}: {storagePage}
          </p>
          {!operations.length ? (
            <p>{t("lifecycleNoPending")}</p>
          ) : (
            <ul>
              {operations.map((entry) => (
                <li key={entry.id}>
                  <label>
                    <input
                      type="radio"
                      name="operation"
                      value={entry.id}
                      checked={selected === String(entry.id)}
                      disabled={busy || locked || !resolutionFor(entry)}
                      onChange={() => {
                        setSelected(String(entry.id));
                        setAttested(false);
                        setResolutionReason("");
                      }}
                    />
                    {entry.operation} · {entry.state} · {entry.storage_model} #{entry.storage_id}
                    {entry.target_key ? ` · ${entry.target_key}` : ""}
                    {requiresVerifiedRecovery(entry) ? ` · ${t("lifecycleCloudRecoveryUnavailable")}` : ""}
                    {entry.state === "queued" && !resolutionFor(entry) && !requiresVerifiedRecovery(entry)
                      ? ` · ${t("lifecycleQueueRecoveryUnavailable")}`
                      : ""}
                  </label>
                </li>
              ))}
            </ul>
          )}
          {storagePage > 1 && (
            <Button
              type="button"
              disabled={busy || locked}
              onClick={() => loadStoragePage(storageCursors[storagePage - 2], storagePage - 1)}
            >
              {t("lifecycleStoragePrevious")}
            </Button>
          )}
          {storageNext && (
            <Button
              type="button"
              disabled={busy || locked}
              onClick={() => loadStoragePage(storageNext, storagePage + 1)}
            >
              {t("lifecycleStorageNext")}
            </Button>
          )}
          {operation && resolution && (
            <form onSubmit={resolve}>
              <p>{t(`lifecycleResolution_${resolution}`)}</p>
              <label htmlFor="lifecycle-resolution-reason">{t("lifecycleReason")}</label>{" "}
              <input
                id="lifecycle-resolution-reason"
                maxLength={2000}
                value={resolutionReason}
                onChange={(e) => setResolutionReason(e.target.value)}
                disabled={busy || locked}
              />
              {!noAttestationNeeded && (
                <p>
                  <label>
                    <input
                      type="checkbox"
                      checked={attested}
                      onChange={(e) => setAttested(e.target.checked)}
                      disabled={busy || locked}
                    />{" "}
                    {t(`lifecycleAttest_${resolution}`)}
                  </label>
                </p>
              )}
              <Button
                type="submit"
                disabled={busy || locked || !resolutionReason.trim() || (!noAttestationNeeded && !attested)}
              >
                {t("lifecycleResolve")}
              </Button>
            </form>
          )}
        </>
      )}
    </div>
  );
};

LifecycleSettings.title = "Lifecycle";
LifecycleSettings.menuItem = "Lifecycle";
LifecycleSettings.path = "/lifecycle";
LifecycleSettings.exact = true;
