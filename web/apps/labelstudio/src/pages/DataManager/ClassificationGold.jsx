import { useEffect, useRef, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";

const id = z.number().int().positive().safe();
const listSchema = z.object({ items: z.array(z.object({ id, kind: z.enum(["reference", "score"]), created_at: z.string(), manifest_hash: z.string().length(64) })).max(100), has_more: z.boolean() });
const sourcesSchema = z.object({ task_id: id, sources: z.array(z.object({ id, revision: id, submitted_by_id: id.nullable(), result_hash: z.string().length(64) })).max(100) });
const detailSchema = z.object({ id, manifest_hash: z.string().length(64), manifest: z.object({ schema: z.literal("classification-gold-evidence/v1"), project_id: id, kind: z.enum(["reference", "score"]), contents: z.record(z.unknown()) }) });
const failed = (r) => !r || r.error || r.$meta?.ok === false || typeof r.status === "number";

export const ClassificationGoldTitle = () => useLocaleTranslation("reviewBatches").t("goldTitle");

export const ClassificationGoldEntry = ({ projectId, onOpen, disabled }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI();
  const call = useRef(api.callApi); call.current = api.callApi;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let live = true; setEnabled(false);
    Promise.resolve(call.current("classificationGold", { params: { pk: projectId }, errorFilter: () => true }))
      .then((r) => { if (live) setEnabled(!failed(r) && listSchema.safeParse(r).success); }).catch(() => {});
    return () => { live = false; };
  }, [projectId]);
  return enabled ? <Button size="small" look="outlined" disabled={disabled} onClick={onOpen}
    data-testid="open-classification-gold">{t("goldTitle")}</Button> : null;
};

export const ClassificationGold = ({ projectId }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI();
  const call = useRef(api.callApi); call.current = api.callApi;
  const pending = useRef(null), generation = useRef(0);
  const [rows, setRows] = useState([]), [hasMore, setHasMore] = useState(false), [detail, setDetail] = useState(null), [sources, setSources] = useState([]);
  const [task, setTask] = useState(""), [submission, setSubmission] = useState(""), [reference, setReference] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [unknown, setUnknown] = useState(false);
  const read = async (method, params, schema) => {
    const value = await call.current(method, { params: { pk: projectId, ...params }, errorFilter: () => true });
    if (failed(value)) throw new Error("unavailable");
    const parsed = schema.parse(value);
    if (parsed.manifest && parsed.manifest.project_id !== projectId) throw new Error("scope");
    return parsed;
  };
  const refresh = async () => {
    const value = await read("classificationGold", {}, listSchema);
    setRows(value.items); setHasMore(value.has_more);
  };
  useEffect(() => {
    let live = true; generation.current += 1; pending.current = null; setUnknown(false); setDetail(null); setRows([]);
    read("classificationGold", {}, listSchema).then((r) => { if (live) { setRows(r.items); setHasMore(r.has_more); } }).catch(() => { if (live) setError(t("goldError")); });
    return () => { live = false; generation.current += 1; };
  }, [projectId]);
  const loadSources = async () => {
    const taskId = Number(task); if (!Number.isSafeInteger(taskId) || taskId <= 0) { setError(t("goldInvalid")); return; }
    setBusy(true); setError(""); setSources([]); setSubmission(""); setDetail(null);
    try { const value = await read("classificationGoldSources", { task: taskId }, sourcesSchema); if (value.task_id !== taskId) throw new Error("scope"); setSources(value.sources); }
    catch { setRows([]); setError(t("goldError")); }
    finally { setBusy(false); }
  };
  const open = async (evidence) => {
    setBusy(true); setError(""); setDetail(null);
    try { const value = await read("classificationGoldDetail", { evidence }, detailSchema); if (value.id !== evidence) throw new Error("scope"); setDetail(value); }
    catch { setRows([]); setError(t("goldError")); }
    finally { setBusy(false); }
  };
  const send = async (intent) => {
    setBusy(true); setError(""); const epoch = generation.current;
    try {
      const raw = await call.current("createClassificationGold", { params: { pk: projectId }, body: intent.body,
        headers: { "Idempotency-Key": intent.key }, errorFilter: () => true });
      if (failed(raw)) {
        if (!unknown && raw?.$meta?.ok === false && [400, 403, 404, 409, 422].includes(raw.$meta.status)) {
          pending.current = null; setUnknown(false); throw new Error("rejected");
        }
        throw new Error("unknown");
      }
      const value = detailSchema.parse(raw);
      if (value.manifest.project_id !== projectId) throw new Error("unknown");
      if (generation.current !== epoch) return;
      pending.current = null; setUnknown(false); setDetail(value);
      try { await refresh(); } catch { setRows([]); setSources([]); setDetail(null); setError(t("goldError")); }
    } catch (e) {
      if (generation.current !== epoch) return;
      if (e.message === "rejected") { setDetail(null); setSources([]); setRows([]); setError(t("goldRejected")); }
      else { setUnknown(true); setError(t("goldUnknown")); }
    } finally { if (generation.current === epoch) setBusy(false); }
  };
  const create = (kind) => {
    const source = Number(submission), ref = Number(reference);
    if (!Number.isSafeInteger(source) || source <= 0 || (kind === "score" && (!Number.isSafeInteger(ref) || ref <= 0))) { setError(t("goldInvalid")); return; }
    const selected = sources.find((value) => value.id === source);
    const selectedReference = rows.find((value) => value.id === ref && value.kind === "reference");
    if (!selected || (kind === "score" && !selectedReference)) { setError(t("goldInvalid")); return; }
    const body = kind === "reference" ? { kind, submission_id: source, expected_result_hash: selected.result_hash }
      : { kind, submission_id: source, expected_result_hash: selected.result_hash,
        reference_id: ref, expected_reference_hash: selectedReference.manifest_hash };
    pending.current = { body, key: uuidv4() }; send(pending.current);
  };
  return <section data-testid="classification-gold">
    <p>{t("goldBoundary")}</p>
    {error && <p role="alert">{error}</p>}
    {unknown && <Button disabled={busy} onClick={() => send(pending.current)}>{t("goldRetry")}</Button>}
    <fieldset disabled={busy || unknown}>
      <label>{t("goldTask")} <input value={task} inputMode="numeric" onChange={(e) => { setTask(e.target.value); setSources([]); setSubmission(""); }} /></label>
      <Button onClick={loadSources}>{t("goldLoadSources")}</Button>
      <label>{t("goldSubmission")} <select value={submission} onChange={(e) => setSubmission(e.target.value)}>
        <option value="">{t("goldSelect")}</option>{sources.map((s) => <option key={s.id} value={s.id}>#{s.id} · {t("goldRevision", { revision: s.revision })} · {s.submitted_by_id}</option>)}
      </select></label>
      <Button onClick={() => create("reference")}>{t("goldCreateReference")}</Button>
      <label>{t("goldReference")} <select value={reference} onChange={(e) => setReference(e.target.value)}>
        <option value="">{t("goldSelect")}</option>{rows.filter((row) => row.kind === "reference").map((row) => <option key={row.id} value={row.id}>#{row.id}</option>)}
      </select></label>
      <Button onClick={() => create("score")}>{t("goldScore")}</Button>
    </fieldset>
    <h4>{t("goldHistory")}</h4>
    {hasMore && <p>{t("goldHistoryLimit")}</p>}
    <ul>{rows.map((row) => <li key={row.id}><button disabled={busy || unknown} type="button" onClick={() => open(row.id)}>
      {t(row.kind === "reference" ? "goldReference" : "goldScore")} #{row.id}</button> · {row.created_at}</li>)}</ul>
    {detail && <div><h4>{t("goldEvidence", { id: detail.id })}</h4><pre>{JSON.stringify(detail.manifest.contents, null, 2)}</pre></div>}
  </section>;
};
