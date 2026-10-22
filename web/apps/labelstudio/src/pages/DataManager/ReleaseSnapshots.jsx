import { useEffect, useRef, useState } from "react";
import { nanoid } from "nanoid";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { downloadFile } from "../../utils/download";

export const ReleaseSnapshots = ({ projectId, releaseId }) => {
  const { t } = useLocaleTranslation("collaboration");
  const api = useAPI();
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const epoch = useRef(0);
  const selected = useRef(null);
  const key = useRef(null);
  const [snapshot, setSnapshot] = useState(null);
  const [history, setHistory] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [against, setAgainst] = useState("");
  const [diff, setDiff] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const request = async (name, options = {}, binary = false) => {
    const start = epoch.current;
    let response;
    try {
      response = await call.current(`${name}Raw`, {
        ...options, params: { pk: projectId, releaseId, ...options.params }, errorFilter: () => true,
      });
    } catch { response = null; }
    if (start !== epoch.current) return null;
    if (!response?.ok) {
      let result;
      try { result = await response?.json(); } catch { /* localized fallback */ }
      if (start !== epoch.current) return null;
      setError([401, 403, 404].includes(response?.status) ? "SNAPSHOT_ACCESS_DENIED" : result?.display_context?.reason ?? "SNAPSHOT_GENERATION_FAILED");
      if ([401, 403, 404].includes(response?.status)) {
        epoch.current += 1; selected.current = null;
        setSnapshot(null); setDiff(null); setHistory([]);
      }
      return null;
    }
    const result = binary ? await response.blob() : await response.json();
    return start === epoch.current ? result : null;
  };
  const operations = useRef({ request });
  operations.current = { request };

  useEffect(() => {
    const start = ++epoch.current;
    selected.current = null; key.current = null;
    setSnapshot(null); setHistory([]); setError(""); setDiff(null);
    operations.current.request("releaseSnapshotList").then((value) => {
      if (value && start === epoch.current) { setHistory(value.results); setCursor(value.next_cursor); }
    });
    return () => { epoch.current += 1; };
  }, [projectId, releaseId]);

  useEffect(() => {
    if (!snapshot || !["queued", "running", "dispatch_pending"].includes(snapshot.state)) return;
    const start = epoch.current;
    const id = snapshot.snapshot_id;
    const timer = setTimeout(async () => {
      const value = await operations.current.request("releaseSnapshotDetail", { params: { snapshotId: id } });
      if (value && start === epoch.current && selected.current === id) setSnapshot(value);
    }, 1500);
    return () => clearTimeout(timer);
  }, [snapshot]);

  const run = async (operation) => {
    setBusy(true); setError("");
    try {
      const start = epoch.current;
      if (operation === "create") key.current ??= nanoid();
      const value = await request(operation === "create" ? "releaseSnapshotCreate" : "releaseSnapshotRetry", {
        body: {}, params: operation === "retry" ? { snapshotId: snapshot.snapshot_id } : {},
        headers: operation === "create" ? { "Idempotency-Key": key.current } : {},
      });
      if (value && start === epoch.current) {
        selected.current = value.snapshot_id; setSnapshot(value); setDiff(null);
        setHistory((items) => [value, ...items.filter((item) => item.snapshot_id !== value.snapshot_id)]);
        if (value.state === "ready") {
          const detail = await request("releaseSnapshotDetail", { params: { snapshotId: value.snapshot_id } });
          if (detail && start === epoch.current && selected.current === value.snapshot_id) setSnapshot(detail);
        }
      }
    } finally { setBusy(false); }
  };
  const download = async (asset) => {
    setBusy(true); setError("");
    try {
      const id = snapshot.snapshot_id;
      const value = await request(asset ? "releaseSnapshotDownload" : "releaseSnapshotManifest", {
        params: { snapshotId: id, ...(asset ? { assetId: asset.id } : {}) },
      }, true);
      if (value && selected.current === id) downloadFile(value, asset?.name ?? `release-manifest-${id}.json`);
    } finally { setBusy(false); }
  };

  return <section data-testid="release-snapshots">
    <h4>{t("snapshotTitle")}</h4>
    <p>{t("snapshotBoundary")}</p>
    <p>{t("snapshotMediaNotice")}</p>
    {error && <p role="alert" data-testid="snapshot-error">{t(`snapshotErrors_${error}`, { defaultValue: t("snapshotFailed") })}</p>}
    <button type="button" data-testid="snapshot-create" disabled={busy} onClick={() => run("create")}>{t("snapshotCreate")}</button>
    <button type="button" disabled={busy} onClick={() => { key.current = null; selected.current = null; setSnapshot(null); setDiff(null); }}>{t("snapshotNewRequest")}</button>
    {history.length > 0 && <label>{t("snapshotHistory")} <select data-testid="snapshot-history" value={snapshot?.snapshot_id ?? ""} disabled={busy}
      onChange={async (event) => {
        const id = event.target.value; const start = epoch.current;
        selected.current = id; setBusy(true); setError(""); setDiff(null);
        try {
          const value = await request("releaseSnapshotDetail", { params: { snapshotId: id } });
          if (value && start === epoch.current && selected.current === id) setSnapshot(value);
        } finally { setBusy(false); }
      }}>
      <option value="" disabled>{t("snapshotChooseHistory")}</option>
      {history.map((item) => <option key={item.snapshot_id} value={item.snapshot_id}>{item.created_at} | {item.snapshot_id}</option>)}
    </select></label>}
    {cursor && <button type="button" disabled={busy} onClick={async () => {
      setBusy(true);
      try {
        const value = await request("releaseSnapshotList", { params: { cursor } });
        if (value) { setHistory((items) => Array.from(new Map([...items, ...value.results].map((item) => [item.snapshot_id, item])).values())); setCursor(value.next_cursor); }
      } finally { setBusy(false); }
    }}>{t("batchMoreHistory")}</button>}
    {snapshot && <div data-testid="snapshot-status" role="status">
      <p>{t(`snapshotStates_${snapshot.state}`, { defaultValue: t("snapshotFailed") })}</p>
      <code data-testid="snapshot-id">{snapshot.snapshot_id}</code>
      {snapshot.error_code && <p>{t(`snapshotErrors_${snapshot.error_code}`, { defaultValue: t("snapshotFailed") })}</p>}
      {snapshot.retryable && <button type="button" disabled={busy} onClick={() => run("retry")}>{t("batchRetry")}</button>}
      {snapshot.state === "ready" && <>
        <p>{t("snapshotDigest")} <code data-testid="snapshot-digest">{snapshot.content_digest}</code></p>
        <button type="button" data-testid="snapshot-manifest" disabled={busy} onClick={() => download()}>{t("snapshotManifest")}</button>
        {(snapshot.assets ?? []).map((asset) => <button type="button" key={asset.id} data-testid={`snapshot-asset-${asset.id}`} disabled={busy} onClick={() => download(asset)}>{t("snapshotAsset", { name: asset.name })}</button>)}
        <label>{t("snapshotCompareWith")} <input data-testid="snapshot-against" value={against} onChange={(event) => setAgainst(event.target.value)} /></label>
        <button type="button" data-testid="snapshot-compare" disabled={busy || !against} onClick={async () => {
          const id = snapshot.snapshot_id; setBusy(true); setError(""); setDiff(null);
          try {
            const value = await request("releaseSnapshotDiff", { params: { snapshotId: id, against } });
            if (value && selected.current === id) setDiff(value);
          } finally { setBusy(false); }
        }}>{t("snapshotCompare")}</button>
      </>}
    </div>}
    {diff && <div data-testid="snapshot-diff"><p>{t("snapshotDiffCounts", diff.counts)}</p><p>{t(diff.schema_changed ? "snapshotSchemaChanged" : "snapshotSchemaUnchanged")}</p></div>}
  </section>;
};
