import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { sha256 } from "@noble/hashes/sha256";
import { bytesToHex } from "@noble/hashes/utils";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { downloadFile } from "../../utils/download";

const PROFILE = "review-audit-redacted/v1";
const EFFECT = "supervisory_only";
const hex = (value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const uuid = (value) => typeof value === "string" && /^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value);
const counts = (value) =>
  value &&
  typeof value === "object" &&
  !Array.isArray(value) &&
  Object.entries(value).length <= 40 &&
  Object.values(value).every((n) => Number.isSafeInteger(n) && n >= 0 && n <= 15000);
const envelope = (value, viewer) =>
  value?.schema === "review-audit-response/v1" &&
  value.profile === PROFILE &&
  value.effect === EFFECT &&
  hex(value.viewer_hash) &&
  hex(value.owner_hash) &&
  (!viewer || value.viewer_hash === viewer);
const receipt = (value) =>
  uuid(value?.package_id) &&
  [
    "dispatch_pending",
    "queued",
    "running",
    "ready",
    "failed_retryable",
    "blocked_auth",
    "blocked_runtime",
    "failed_final",
    "quarantined",
  ].includes(value.state) &&
  counts(value.counts) &&
  counts(value.gaps) &&
  value.coverage === "complete" &&
  ["complete", "with_gaps"].includes(value.evidence) &&
  typeof value.captured_at === "string" &&
  value.captured_at.length <= 128;
const definitive = (error) =>
  (({
    AUDIT_INVALID_REQUEST: 400,
    AUDIT_INVALID_KEY: 400,
    AUDIT_PREVIEW_CHANGED: 409,
    AUDIT_IDEMPOTENCY_CONFLICT: 409,
    AUDIT_ACTIVE_LIMIT: 429,
    AUDIT_FORBIDDEN: 403,
    AUDIT_MODE_FORBIDDEN: 403,
    AUDIT_VIEWER_CHANGED: 403,
  })[error?.reason] ?? 0) === error?.status;

export const ReviewAuditDownload = ({ projectId, batchId, ownerKey = "", fallbackRef }) => {
  const api = useAPI();
  const { t } = useLocaleTranslation("reviewBatches");
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const context = `${projectId}/${batchId}/${ownerKey}`;
  const current = useRef(context);
  current.current = context;
  const epoch = useRef(0);
  const viewer = useRef(null);
  const owner = useRef(null);
  const intent = useRef(null);
  const selected = useRef(null);
  const title = useRef(null);
  const region = useRef(null);
  const trigger = useRef(null);
  const focusOpened = useRef(false);
  const [enabled, setEnabled] = useState(false);
  const [opened, setOpened] = useState(false);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const [detail, setDetail] = useState(null);
  const [history, setHistory] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [error, setError] = useState("");
  const [unknown, setUnknown] = useState(false);
  const fresh = (generation, scope) => generation === epoch.current && scope === current.current;
  const clearRead = () => {
    setPreview(null);
    setConfirmed(false);
    setDetail(null);
    setHistory([]);
    setCursor(null);
  };
  const leaveRegion = () => {
    if (region.current?.contains(document.activeElement) && fallbackRef?.current?.isConnected)
      fallbackRef.current.focus({ preventScroll: true });
  };

  const request = async (method, options = {}, expected = viewer.current) => {
    const generation = epoch.current;
    const scope = current.current;
    let response;
    try {
      response = await call.current(`${method}Raw`, {
        ...options,
        params: { pk: projectId, ...options.params },
        headers: { ...options.headers, ...(expected ? { "X-Audit-Viewer": expected } : {}) },
        errorFilter: () => true,
      });
    } catch {
      if (!fresh(generation, scope)) throw { stale: true };
      throw { reason: "transport", generation, scope };
    }
    if (!fresh(generation, scope)) throw { stale: true };
    if (!response?.ok) {
      let body;
      try {
        body = await response?.json();
      } catch {
        /* an untrusted error body conveys no authority */
      }
      if (!fresh(generation, scope)) throw { stale: true };
      if (response?.status === 403 && body?.display_context?.reason === "AUDIT_VIEWER_CHANGED") {
        const capResponse = await call.current("reviewAuditCapabilitiesRaw", {
          params: { pk: projectId },
          errorFilter: () => true,
        });
        const cap = capResponse?.ok ? await capResponse.json() : null;
        if (!fresh(generation, scope)) throw { stale: true };
        if (envelope(cap) && cap.owner_hash === owner.current) {
          viewer.current = cap.viewer_hash;
          throw { reason: "viewer_rotation", generation, scope };
        }
      }
      if ([401, 403].includes(response?.status)) {
        leaveRegion();
        clearRead();
        setEnabled(false);
        setUnknown(false);
        intent.current = null;
        viewer.current = null;
        owner.current = null;
      }
      throw {
        reason: body?.display_context?.reason,
        status: response?.status,
        generation,
        scope,
        rejected: response?.headers?.get("X-Audit-Command-State") === "rejected",
      };
    }
    return { response, generation, scope };
  };
  const json = async (method, options, expected = viewer.current) => {
    const result = await request(method, options, expected);
    let value;
    try {
      value = await result.response.json();
    } catch {
      if (!fresh(result.generation, result.scope)) throw { stale: true };
      throw { reason: "invalid" };
    }
    if (!fresh(result.generation, result.scope)) throw { stale: true };
    if (!envelope(value, expected) || (owner.current && value.owner_hash !== owner.current))
      throw { reason: "invalid" };
    return value;
  };

  useLayoutEffect(() => {
    epoch.current += 1;
    viewer.current = null;
    owner.current = null;
    intent.current = null;
    selected.current = null;
    setEnabled(false);
    setOpened(false);
    setBusy(false);
    setUnknown(false);
    setError("");
    clearRead();
    return () => {
      leaveRegion();
      epoch.current += 1;
    };
  }, [context]);
  useEffect(() => {
    const generation = epoch.current;
    json("reviewAuditCapabilities", {}, null)
      .then((value) => {
        if (fresh(generation, context) && value.collection_supported === true) {
          viewer.current = value.viewer_hash;
          owner.current = value.owner_hash;
          setEnabled(true);
        }
      })
      .catch(() => {});
  }, [context]);
  useLayoutEffect(() => {
    if (opened && focusOpened.current) {
      focusOpened.current = false;
      if (document.activeElement === trigger.current || document.activeElement === document.body)
        title.current?.focus();
    }
  }, [opened]);

  const action = async (work) => {
    title.current?.focus();
    const generation = epoch.current;
    const scope = current.current;
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (failure) {
      if (fresh(generation, scope) && !failure?.stale) {
        clearRead();
        setError(failure?.reason === "unknown" ? "auditUnknown" : "auditFailed");
      }
    } finally {
      if (fresh(generation, scope)) setBusy(false);
    }
  };
  const loadHistory = async (next = null) => {
    const value = await json("reviewAuditList", { params: next ? { cursor: next } : {} });
    if (
      !Array.isArray(value.results) ||
      value.results.length > 50 ||
      !value.results.every(receipt) ||
      (value.next_cursor !== null && typeof value.next_cursor !== "string")
    )
      throw { reason: "invalid" };
    setHistory((old) => (next ? [...old, ...value.results] : value.results));
    setCursor(value.next_cursor);
  };
  const loadDetail = async (packageId) => {
    const value = await json("reviewAuditDetail", { params: { packageId } });
    if (
      !receipt(value) ||
      value.package_id !== packageId ||
      !Array.isArray(value.artifacts) ||
      value.artifacts.length > 1 ||
      value.artifacts.some(
        (a) =>
          !uuid(a.artifact_id) ||
          !hex(a.sha256) ||
          !Number.isSafeInteger(a.size_bytes) ||
          a.size_bytes <= 0 ||
          a.size_bytes > 16 * 1024 ** 2,
      )
    )
      throw { reason: "invalid" };
    if (selected.current === packageId) setDetail(value);
  };
  const sendCommand = async () => {
    const command = intent.current;
    if (!command) return;
    let value;
    try {
      value = await json(command.kind === "retry" ? "reviewAuditRetry" : "reviewAuditCreate", {
        params: command.kind === "retry" ? { packageId: command.packageId } : {},
        body: { ...command.body },
        headers: { "Idempotency-Key": command.key },
      });
      if (
        command.kind === "retry" ? value.package_id !== command.packageId || value.key !== command.key : !receipt(value)
      )
        throw { reason: "invalid" };
    } catch (failure) {
      if (failure?.stale) throw failure;
      if (intent.current !== command) throw failure;
      if (!failure?.rejected && !definitive(failure)) {
        setUnknown(true);
        throw { reason: "unknown" };
      }
      intent.current = null;
      setUnknown(false);
      throw failure;
    }
    intent.current = null;
    setUnknown(false);
    setPreview(null);
    setConfirmed(false);
    selected.current = value.package_id;
    setDetail(command.kind === "retry" ? null : value);
    await loadDetail(value.package_id);
  };
  const capture = async () => {
    if (!intent.current) {
      if (!preview || !confirmed) return;
      intent.current = {
        kind: "capture",
        key: uuidv4(),
        body: Object.freeze({
          review_batch_id: batchId,
          profile: PROFILE,
          expected_batch_binding_hash: preview.batch_binding_hash,
          expected_capture_preview_hash: preview.capture_preview_hash,
        }),
      };
    }
    await sendCommand();
  };
  const download = async (artifact) => {
    const packageId = detail.package_id;
    const result = await request("reviewAuditDownload", { params: { packageId, artifactId: artifact.artifact_id } });
    if (
      result.response.headers?.get("X-Audit-Viewer") !== viewer.current ||
      result.response.headers?.get("Content-Type")?.split(";")[0] !== "application/zip"
    )
      throw { reason: "invalid" };
    const blob = await result.response.blob();
    if (!fresh(result.generation, result.scope) || selected.current !== packageId) return;
    if (blob.size !== artifact.size_bytes || blob.size > 16 * 1024 ** 2) throw { reason: "invalid" };
    const bytes = await blob.arrayBuffer();
    const hash = bytesToHex(sha256(new Uint8Array(bytes)));
    if (!fresh(result.generation, result.scope) || selected.current !== packageId) return;
    if (hash !== artifact.sha256) throw { reason: "invalid" };
    downloadFile(blob, `review-audit-${packageId}.zip`);
  };

  if (!enabled) return null;
  return (
    <section ref={region} data-testid="review-audit">
      <button
        type="button"
        ref={trigger}
        data-testid="review-audit-open"
        aria-expanded={opened}
        disabled={busy}
        onClick={() => {
          if (opened) {
            trigger.current?.focus();
            setOpened(false);
          } else {
            focusOpened.current = true;
            setOpened(true);
            action(() => loadHistory());
          }
        }}
      >
        {t("auditTitle")}
      </button>
      {opened && (
        <div>
          <h4 ref={title} tabIndex={-1}>
            {t("auditTitle")}
          </h4>
          <p>{t("auditBoundary")}</p>
          <p>{t("auditRedaction")}</p>
          <p>{t("auditDeletion")}</p>
          {error && <p role="alert">{t(error)}</p>}
          <button
            type="button"
            data-testid="review-audit-preview"
            disabled={busy || unknown}
            onClick={() =>
              action(async () => {
                const value = await json("reviewAuditPreview", { body: { review_batch_id: batchId } });
                if (
                  !hex(value.batch_binding_hash) ||
                  !hex(value.capture_preview_hash) ||
                  !counts(value.gaps) ||
                  !Number.isSafeInteger(value.candidate_count) ||
                  value.redacted !== true
                )
                  throw { reason: "invalid" };
                setPreview(value);
                setConfirmed(false);
              })
            }
          >
            {t("auditPreview")}
          </button>
          {preview && (
            <div>
              <p>
                {t("auditCounts", {
                  candidates: preview.candidate_count,
                  entities: preview.entity_count,
                  edges: preview.edge_count,
                })}
              </p>
              <p>{t("auditGaps", { count: Object.values(preview.gaps).reduce((a, b) => a + b, 0) })}</p>
              <label>
                <input
                  type="checkbox"
                  data-testid="review-audit-confirm"
                  checked={confirmed}
                  disabled={busy}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                {t("auditConfirm")}
              </label>
            </div>
          )}
          <button
            type="button"
            data-testid="review-audit-create"
            disabled={busy || !confirmed || unknown}
            onClick={() => action(capture)}
          >
            {t("auditCreate")}
          </button>
          {unknown && (
            <button
              type="button"
              data-testid="review-audit-recover"
              disabled={busy}
              onClick={() => action(sendCommand)}
            >
              {t("auditRecover")}
            </button>
          )}
          <h5>{t("auditHistory")}</h5>
          <button
            type="button"
            data-testid="review-audit-refresh"
            disabled={busy}
            onClick={() =>
              action(async () => {
                await loadHistory();
                if (selected.current) await loadDetail(selected.current);
              })
            }
          >
            {t("auditRefresh")}
          </button>
          {history.map((item) => (
            <button
              type="button"
              key={item.package_id}
              data-audit-package={item.package_id}
              disabled={busy}
              onClick={() =>
                action(async () => {
                  selected.current = item.package_id;
                  setDetail(null);
                  await loadDetail(item.package_id);
                })
              }
            >
              {item.captured_at} · {t(`auditState_${item.state}`)}
            </button>
          ))}
          {cursor && (
            <button type="button" disabled={busy} onClick={() => action(() => loadHistory(cursor))}>
              {t("auditMore")}
            </button>
          )}
          {detail && (
            <div data-testid="review-audit-status" role="status">
              <p>{t(`auditState_${detail.state}`)}</p>
              <p>{t("auditCaptured", { time: detail.captured_at })}</p>
              <p>{t("auditEvidence", { count: Object.values(detail.gaps).reduce((a, b) => a + b, 0) })}</p>
              {detail.retryable && (
                <button
                  type="button"
                  data-testid="review-audit-retry"
                  disabled={busy || unknown}
                  onClick={() =>
                    action(async () => {
                      intent.current = {
                        kind: "retry",
                        packageId: detail.package_id,
                        key: uuidv4(),
                        body: Object.freeze({}),
                      };
                      await sendCommand();
                    })
                  }
                >
                  {t("auditRetry")}
                </button>
              )}
              {(detail.artifacts ?? []).map((artifact) => (
                <button
                  type="button"
                  data-testid="review-audit-download"
                  key={artifact.artifact_id}
                  disabled={busy}
                  onClick={() => action(() => download(artifact))}
                >
                  {t("auditDownload")}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
};
