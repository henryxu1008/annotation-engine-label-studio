import { useCallback, useEffect, useRef, useState } from "react";
import { nanoid } from "nanoid";
import { useAPI } from "../../providers/ApiProvider";
import { useLocaleTranslation } from "@humansignal/i18n";
import { downloadFile } from "../../utils/download";
import "./ApprovedDatasetExport.scss";

export const ApprovedDatasetExport = ({ projectId }) => {
  const api = useAPI();
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const { t } = useLocaleTranslation("collaboration");
  const generation = useRef(0);
  const key = useRef(null);
  const responses = useRef(new WeakMap());
  const [enabled, setEnabled] = useState(false);
  const [opened, setOpened] = useState(false);
  const [rows, setRows] = useState([]);
  const [selected, setSelected] = useState({});
  const [cursor, setCursor] = useState(null);
  const [formats, setFormats] = useState(["JSON", "JSONL"]);
  const [format, setFormat] = useState("JSONL");
  const [preview, setPreview] = useState(null);
  const [batch, setBatch] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState([]);
  const currentBatch = useRef(null);

  const request = useCallback(async (method, options = {}) => {
    const epoch = generation.current;
    const response = await call.current(`${method}Raw`, {
      ...options, params: { pk: projectId, ...options.params }, errorFilter: () => true,
    }).catch(() => null);
    if (epoch !== generation.current) return null;
    if (!response?.ok) {
      let body = {};
      try { body = await response?.json(); } catch { /* localized generic fallback */ }
      if (epoch !== generation.current) return null;
      const reason = response?.status === 404 ? "NOT_FOUND" : [401, 403].includes(response?.status) ? "RELEASE_FORBIDDEN" : body?.display_context?.reason;
      setError(typeof reason === "string" ? reason : "GENERATION_FAILED");
      if ([401, 403, 404].includes(response?.status)) {
        generation.current += 1;
        currentBatch.current = null;
        setBatch(null);
        setSelected({});
        setPreview(null);
      }
      return null;
    }
    responses.current.set(response, epoch);
    return response;
  }, [projectId]);

  const readJson = async (response) => {
    if (!response) return null;
    const value = await response.json();
    return responses.current.get(response) === generation.current ? value : null;
  };

  useEffect(() => {
    generation.current += 1;
    currentBatch.current = null;
    setEnabled(false); setOpened(false); setRows([]); setSelected({}); setBatch(null); setError(""); setHistory([]);
    const epoch = generation.current;
    Promise.resolve().then(() => call.current("approvedExportCapabilitiesRaw", { params: { pk: projectId }, errorFilter: () => true }))
      .then(async (response) => {
        if (response?.ok && epoch === generation.current) {
          const capabilities = await response.json();
          if (epoch === generation.current) setEnabled(capabilities.mode === "fork_approved");
        }
      }).catch(() => { /* capability remains fail-closed */ });
    return () => { generation.current += 1; };
  }, [projectId]);

  const load = async (next = null) => {
    setBusy(true); setError("");
    try {
      const value = await readJson(await request("approvedExportCandidates", { params: next ? { cursor: next } : {} }));
      if (value) { setRows(value.results); setCursor(value.next_cursor); }
      const receipts = await readJson(await request("approvedExportList"));
      if (receipts) setHistory(receipts.results);
    } finally { setBusy(false); }
  };

  useEffect(() => {
    if (!batch?.release_id || !["queued", "running", "dispatch_pending"].includes(batch.state)) return;
    const epoch = generation.current;
    const timer = setTimeout(async () => {
      const response = await request("approvedExportDetail", { params: { releaseId: batch.release_id } });
      const result = await readJson(response);
      if (result && epoch === generation.current && currentBatch.current === batch.release_id) setBatch(result);
    }, 1500);
    return () => clearTimeout(timer);
  }, [batch, request]);

  const changeSelection = async (row, checked) => {
    const next = { ...selected };
    if (checked) next[row.submission_id] = row; else delete next[row.submission_id];
    setSelected(next); setPreview(null); key.current = null;
    const first = Object.values(next)[0];
    if (first) {
      const response = await request("approvedExportCapabilities", { params: { submission_id: first.submission_id } });
      const capabilities = await readJson(response);
      if (capabilities) {
        setFormats(capabilities.formats);
        if (!capabilities.formats.includes(format)) setFormat("JSONL");
      }
    }
  };

  const body = () => ({
    items: Object.values(selected).map(({ submission_id, revision, result_hash }) => ({ submission_id, revision, result_hash })),
    output: { format, resource_mode: "references_only" }, filters: {},
  });
  const run = async (operation) => {
    setBusy(true); setError("");
    try {
      const options = operation === "retry" ? { params: { releaseId: batch.release_id }, body: {} } : { body: body() };
      if (operation === "create") {
        key.current ??= nanoid();
        options.headers = { "Idempotency-Key": key.current };
      }
      const response = await request({ preview: "approvedExportPreview", create: "approvedExportCreate", retry: "approvedExportRetry" }[operation], options);
      const result = await readJson(response);
      if (result) {
        if (operation === "preview") setPreview(result); else {
          currentBatch.current = result.release_id;
          setBatch(result); setHistory((previous) => [result, ...previous.filter((item) => item.release_id !== result.release_id)]);
        }
      }
    } finally { setBusy(false); }
  };
  const download = async (artifact) => {
    setBusy(true); setError("");
    try {
      const response = await request("approvedExportDownload", { params: { releaseId: batch.release_id, artifactId: artifact.id } });
      if (response) {
        const blob = await response.blob();
        if (responses.current.get(response) === generation.current) downloadFile(blob, artifact.name);
      }
    } finally { setBusy(false); }
  };
  const downloadManifest = async () => {
    setBusy(true); setError("");
    try {
      const response = await request("approvedExportManifest", { params: { releaseId: batch.release_id } });
      if (!response) return;
      const blob = await response.blob();
      if (responses.current.get(response) !== generation.current) return;
      downloadFile(blob, `manifest-${batch.release_id}.json`);
    } finally { setBusy(false); }
  };

  if (!enabled) return null;
  return <section className="approved-dataset-export" data-testid="approved-batch-export">
    <button type="button" data-testid="open-approved-batch" onClick={() => { setOpened(!opened); if (!opened) load(); }}>
      {t("batchTitle")}
    </button>
    {opened && <div>
      <p>{t("batchBoundary")}</p>
      <p>{t("batchMediaNotice")}</p>
      {history.length > 0 && <label>{t("batchHistory")} <select data-testid="batch-history" value={batch?.release_id ?? ""}
        disabled={busy}
        onChange={async (event) => {
          const requested = event.target.value;
          currentBatch.current = requested;
          setBusy(true);
          try {
            const value = await readJson(await request("approvedExportDetail", { params: { releaseId: requested } }));
            if (value && currentBatch.current === requested) setBatch(value);
          } finally { setBusy(false); }
        }}>
        <option value="" disabled>{t("batchChooseHistory")}</option>
        {history.map((receipt) => <option key={receipt.release_id} value={receipt.release_id}>{receipt.created_at} 路 {t(`batchStates_${receipt.state}`)}</option>)}
      </select></label>}
      {error && <div role="alert" data-testid="batch-error">{t(`batchErrors_${error}`, { defaultValue: t("batchFailed") })}</div>}
      <table><thead><tr><th>{t("batchSelect")}</th><th>{t("batchSource")}</th><th>{t("batchReviewer")}</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={row.submission_id}>
          <td><input type="checkbox" aria-label={t("batchSelectSource", { id: row.submission_id })}
            data-testid={`batch-select-${row.submission_id}`} checked={Boolean(selected[row.submission_id])} disabled={busy}
            onChange={(event) => changeSelection(row, event.target.checked)} /></td>
          <td>{t("batchRevision", { id: row.submission_id, revision: row.revision, task: row.task_id })}<br /><code>{row.result_hash}</code></td>
          <td>{row.review.reviewer.local_user_id}</td>
        </tr>)}</tbody>
      </table>
      {!rows.length && <p>{t("batchEmpty")}</p>}
      <div className="approved-dataset-export__actions">
        <button type="button" disabled={busy} onClick={() => load()}>{t("batchRefresh")}</button>
        <button type="button" disabled={busy || !cursor} onClick={() => load(cursor)}>{t("next")}</button>
        <label>{t("batchFormat")} <select data-testid="batch-format" value={format} disabled={busy}
          onChange={(event) => { setFormat(event.target.value); setPreview(null); key.current = null; }}>
          {formats.map((value) => <option key={value}>{value}</option>)}
        </select></label>
        <span>{t("batchCount", { count: Object.keys(selected).length })}</span>
        <button type="button" data-testid="batch-preview" disabled={busy || !Object.keys(selected).length} onClick={() => run("preview")}>{t("batchPreview")}</button>
        <button type="button" data-testid="batch-create" disabled={busy || !preview} onClick={() => run("create")}>{t("batchCreate")}</button>
      </div>
      {preview && <p data-testid="batch-preview-result">{t("batchPreviewCount", { count: preview.item_count, tasks: preview.task_count })}</p>}
      {batch && <div data-testid="batch-status" role="status">
        <p>{t(`batchStates_${batch.state}`)}</p>
        {batch.error_code && <p>{t(`batchErrors_${batch.error_code}`, { defaultValue: t("batchFailed") })}</p>}
        {batch.retryable && <button type="button" data-testid="batch-retry" disabled={busy} onClick={() => run("retry")}>{t("batchRetry")}</button>}
        {(batch.artifacts ?? []).map((artifact) => <button type="button" key={artifact.id} data-testid={`batch-download-${artifact.format}`}
          disabled={busy} onClick={() => download(artifact)}>{t("batchDownload", { format: artifact.format })}</button>)}
        {batch.state === "ready" && <button type="button" data-testid="batch-download-manifest" disabled={busy} onClick={downloadManifest}>{t("batchDownloadManifest")}</button>}
      </div>}
    </div>}
  </section>;
};
