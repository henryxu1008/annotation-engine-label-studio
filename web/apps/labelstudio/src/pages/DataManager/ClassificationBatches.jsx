import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { v4 as uuidv4 } from "uuid";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { ClassificationResult } from "./ClassificationDisagreement";
import { ClassificationArbitrationEntry } from "./ClassificationArbitrations";
import "./ReviewBatches.scss";
import "./ClassificationBatches.scss";

const id = z.number().int().positive().safe();
const count = z.number().int().nonnegative().safe();
const counts = z.object({ agreement: count, disagreement: count, unavailable: count });
const metadata = z.object({ id, name: z.string(), schema: z.literal("classification-batches/v1"),
  rule: z.literal("classification-exact-sets/v1"), selection: z.literal("active-assignment-latest/v1"),
  created_at: z.string(), calculated_at: z.string(), scope_enumerated_at: z.string(), visible_task_count: count,
  visibility: z.enum(["project", "visible_subset"]), manifest_hash: z.string().optional(), projection_hash: z.string().optional(),
});
const capability = z.object({ enabled: z.literal(true), can_create: z.boolean() });
const previewSchema = z.object({ schema: z.literal("classification-batches/v1"), preview_token: z.string(), plan_hash: z.string(),
  task_ids: z.array(id).min(1).max(100), display_counts: counts, scope_enumerated_at: z.string() });
const listSchema = z.object({ count, next: z.string().nullable(), previous: z.string().nullable(), results: z.array(metadata) });
const detailSchema = metadata.extend({ display_counts: counts });
const summary = z.object({ task_id: id, outcome: z.enum(["agreement", "disagreement", "unavailable"]), reason: z.string().nullable(),
  agreement: z.number().min(0).max(1).nullable(), matching_pairs: count.nullable(), total_pairs: count.nullable() });
const queueSchema = z.object({ batch_id: id, count, next: z.string().nullable(), previous: z.string().nullable(), display_counts: counts,
  results: z.array(summary) });
const itemSchema = metadata.extend({ observed_at: z.string(), item: summary.extend({
  status: z.enum(["comparable", "unavailable"]), inputs: z.array(z.object({ submission_id: id, revision: id,
    submitter_id: id.nullable(), result_hash: z.string(), config_hash: z.string() })),
  locations: z.array(z.object({ from_name: z.string(), to_name: z.string(), matching_pairs: count, total_pairs: count,
    agreement: z.number().min(0).max(1), disagrees: z.boolean(),
    values: z.array(z.object({ submission_id: id, choices: z.array(z.string()) })) })),
}).refine((r) => r.status === "unavailable" ? r.agreement === null && r.matching_pairs === null && r.total_pairs === null && r.locations.length === 0
  : r.agreement !== null && r.total_pairs > 0) });
const createdSchema = metadata.extend({ replayed: z.boolean(), idempotency_key: z.string().uuid() });
const failed = (r) => !r || r.error || r.$meta?.ok === false || typeof r.status === "number";
const previewRejections = {
  CLASSIFICATION_BATCH_PREVIEW_INVALID: 400,
  CLASSIFICATION_BATCH_PREVIEW_CHANGED: 409,
  CLASSIFICATION_BATCH_SCOPE_CHANGED: 409,
  CLASSIFICATION_BATCH_SOURCE_INVALID: 409,
  CLASSIFICATION_BATCH_TOO_LARGE: 422,
};
const states = ["disagreement", "agreement", "unavailable", "all"];

export const ClassificationBatchTitle = () => {
  const { t } = useLocaleTranslation("reviewBatches");
  return t("batchTitle");
};

export const ClassificationBatchEntry = ({ projectId, onOpen, disabled }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI();
  const call = useRef(api.callApi); call.current = api.callApi;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let disposed = false; setEnabled(false);
    Promise.resolve(call.current("classificationBatchCapability", { params: { pk: projectId }, errorFilter: () => true }))
      .then((r) => { if (!disposed) setEnabled(!failed(r) && capability.safeParse(r).success); }).catch(() => {});
    return () => { disposed = true; };
  }, [projectId]);
  return enabled ? <Button size="small" look="outlined" disabled={disabled} onClick={onOpen}
    data-testid="open-classification-batches">{t("batchTitle")}</Button> : null;
};

export const ClassificationBatches = ({ projectId, onOpenArbitration }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI();
  const call = useRef(api.callApi); call.current = api.callApi;
  const generation = useRef(0), pending = useRef(null), focusTask = useRef(null), root = useRef(null);
  const [view, setView] = useState("list"), [busy, setBusy] = useState(false), [error, setError] = useState(false);
  const [canCreate, setCanCreate] = useState(false), [unknown, setUnknown] = useState(false);
  const [saved, setSaved] = useState(null);
  const [list, setList] = useState(null), [preview, setPreview] = useState(null), [detail, setDetail] = useState(null);
  const [queue, setQueue] = useState(null), [answer, setAnswer] = useState(null);
  const [batchId, setBatchId] = useState(null), [page, setPage] = useState(1), [state, setState] = useState("disagreement");
  const [form, setForm] = useState({ name: "", scope: "project", tasks: "" });
  const clear = () => { setList(null); setDetail(null); setQueue(null); setAnswer(null); setCanCreate(false); };
  const fetch = async (method, params, schema, body, headers) => {
    const result = await call.current(method, { params: { pk: projectId, ...params }, body, headers, errorFilter: () => true });
    if (failed(result)) {
      const error = new Error("unavailable");
      const reason = result?.response?.display_context?.reason;
      error.previewRejected = method === "createClassificationBatch" && result?.$meta?.ok === false
        && typeof reason === "string" && Object.prototype.hasOwnProperty.call(previewRejections, reason)
        && previewRejections[reason] === result.$meta.status;
      throw error;
    }
    return schema.parse(result);
  };
  const read = async (work) => {
    const current = ++generation.current;
    setBusy(true); setError(false); clear();
    try { await work(() => current === generation.current); }
    catch { if (current === generation.current) { clear(); setError(true); } }
    finally { if (current === generation.current) setBusy(false); }
  };
  const loadList = (targetPage = 1) => {
    setView("list"); setPage(targetPage);
    return read(async (current) => {
      const [caps, rows] = await Promise.all([
        fetch("classificationBatchCapability", {}, capability), fetch("classificationBatches", { page: targetPage }, listSchema),
      ]);
      if (current()) { setCanCreate(caps.can_create); setList(rows); }
    });
  };
  const openBatch = (id, targetPage = 1, filter = "disagreement") => {
    setView("detail"); setBatchId(id); setPage(targetPage); setState(filter);
    return read(async (current) => {
      const [caps, data, items] = await Promise.all([
        fetch("classificationBatchCapability", {}, capability), fetch("classificationBatchDetail", { batch: id }, detailSchema),
        fetch("classificationBatchItems", { batch: id, page: targetPage, state: filter }, queueSchema),
      ]);
      if (data.id !== id || items.batch_id !== id) throw new Error("scope changed");
      if (current()) { setCanCreate(caps.can_create); setDetail(data); setQueue(items); }
    });
  };
  const showItem = (task) => {
    focusTask.current = task;
    return read(async (current) => {
      const result = await fetch("classificationBatchItem", { batch: batchId, task }, itemSchema);
      if (result.id !== batchId || result.item.task_id !== task) throw new Error("scope changed");
      if (current()) setAnswer(result);
    });
  };
  useEffect(() => {
    pending.current = null; setUnknown(false); setSaved(null); setPreview(null); setBatchId(null); loadList();
    const blur = () => { generation.current += 1; clear(); setPreview(null); setBusy(false); setError(true);
      if (pending.current?.attempted) setUnknown(true);
    };
    window.addEventListener("blur", blur);
    return () => { generation.current += 1; window.removeEventListener("blur", blur); };
  }, [projectId]);
  useEffect(() => {
    if (!busy && queue && !answer && focusTask.current !== null) {
      root.current?.querySelector(`[data-task="${focusTask.current}"]`)?.focus();
      focusTask.current = null;
    }
  }, [busy, queue, answer]);
  const change = (key, value) => {
    setForm((old) => ({ ...old, [key]: value })); setPreview(null); pending.current = null; setError(false);
  };
  const plan = async () => {
    const current = ++generation.current;
    setBusy(true); setError(false); setSaved(null); setPreview(null); pending.current = null;
    try {
      const scope = form.scope === "project" ? { kind: "project" }
        : { kind: "tasks", task_ids: form.tasks.trim().split(/[\s,]+/).map(Number) };
      const result = await fetch("classificationBatchPreview", {}, previewSchema, { name: form.name, scope });
      if (current !== generation.current) return;
      pending.current = { body: { preview_token: result.preview_token, expected_plan_hash: result.plan_hash }, key: uuidv4(), attempted: false };
      setPreview(result);
    } catch { if (current === generation.current) setError(true); }
    finally { if (current === generation.current) setBusy(false); }
  };
  const create = async () => {
    const intent = pending.current;
    if (!intent) return;
    const recovering = intent.attempted;
    intent.attempted = true;
    const current = ++generation.current;
    setBusy(true); setError(false); clear();
    try {
      const result = await fetch("createClassificationBatch", {}, createdSchema, intent.body, { "Idempotency-Key": intent.key });
      if (current !== generation.current) return;
      if (result.idempotency_key !== intent.key) throw new Error("acknowledgement changed");
      pending.current = null; setUnknown(false); setPreview(null);
      setSaved({ id: result.id, replayed: result.replayed });
      await openBatch(result.id);
    } catch (error) {
      if (current === generation.current) {
        if (!recovering && error?.previewRejected === true) {
          pending.current = null; setPreview(null); setUnknown(false); setError("batchPreviewRejected");
        } else { setUnknown(true); setError(true); }
      }
    }
    finally { if (current === generation.current) setBusy(false); }
  };
  const refresh = () => view === "detail" && batchId ? openBatch(batchId, page, state) : loadList(page);
  const report = answer ? { ...answer.item, manifest_hash: answer.manifest_hash ?? answer.projection_hash,
    observed_at: answer.observed_at, manifest: { inputs: answer.item.inputs, rule: answer.rule, calculated_at: answer.calculated_at } } : null;
  return <section ref={root} className="review-batches classification-batches" data-testid="classification-batches" aria-busy={busy}>
    <p>{t("batchBoundary")}</p>
    {busy && <p role="status">{t("classificationLoading")}</p>}
    {error && <p role="alert">{t(typeof error === "string" ? error : "batchReadFailed")}</p>}
    {saved && <p role="status">{t(saved.replayed ? "batchRecovered" : "batchSaved", { id: saved.id })}</p>}
    {unknown && <p role="status">{t("batchUnknown")} <Button disabled={busy} onClick={create}>{t("batchRecover")}</Button></p>}
    {!busy && view !== "create" && <Button onClick={refresh}>{t("batchRefresh")}</Button>}
    {view === "list" && <>
      {canCreate && !unknown && <Button disabled={busy} onClick={() => { setView("create"); setList(null); setError(false); }}>{t("batchNew")}</Button>}
      {list && <>
        {!list.results.length && <p>{t("batchEmpty")}</p>}
        <ul>{list.results.map((batch) => <li key={batch.id}><Button onClick={() => openBatch(batch.id)}>{batch.name}</Button></li>)}</ul>
        <Button disabled={busy || !list.previous} onClick={() => loadList(page - 1)}>{t("batchPrevious")}</Button>
        <Button disabled={busy || !list.next} onClick={() => loadList(page + 1)}>{t("batchNext")}</Button>
      </>}
    </>}
    {view === "create" && <>
      <h3>{t("batchNew")}</h3>
      <fieldset disabled={busy || unknown}>
        <label>{t("batchName")}<input value={form.name} maxLength={160} onChange={(e) => change("name", e.target.value)} /></label>
        <label>{t("batchScope")}<select value={form.scope} onChange={(e) => change("scope", e.target.value)}>
          <option value="project">{t("batchProjectScope")}</option><option value="tasks">{t("batchTaskScope")}</option>
        </select></label>
        {form.scope === "tasks" && <label>{t("batchTasks")}<input value={form.tasks} onChange={(e) => change("tasks", e.target.value)} /></label>}
        <p>{t("batchLimits")}</p>
        <Button disabled={!form.name.trim()} onClick={plan}>{t("batchPreview")}</Button>
        {preview && <div data-testid="classification-batch-preview">
          <p>{t("batchScopeFrozen", { count: preview.task_ids.length, time: preview.scope_enumerated_at })}</p>
          <p>{preview.task_ids.join(", ")}</p>
          <p>{t("batchCounts", preview.display_counts)}</p>
          <Button onClick={create}>{t("batchCreate")}</Button>
        </div>}
      </fieldset>
      <Button disabled={busy} onClick={() => { if (!unknown) { pending.current = null; setPreview(null); } loadList(); }}>{t("batchBackList")}</Button>
    </>}
    {view === "detail" && <>
      <Button disabled={busy} onClick={() => loadList()}>{t("batchBackList")}</Button>
      {answer ? <>
        <Button onClick={() => openBatch(batchId, page, state)}>{t("batchBackQueue")}</Button>
        {answer.item.outcome === "disagreement" && answer.manifest_hash && <ClassificationArbitrationEntry projectId={projectId} dispatch
          onOpen={onOpenArbitration ? () => onOpenArbitration({ batch_id: answer.id, task_id: answer.item.task_id, expected_manifest_hash: answer.manifest_hash }) : undefined} />}
        <ClassificationResult report={report} durable />
      </> : detail && queue && <>
        <h3>{detail.name}</h3>
        <p>{t("batchScopeFrozen", { count: detail.visible_task_count, time: detail.scope_enumerated_at })}</p>
        {detail.visibility === "visible_subset" && <p>{t("batchVisibleSubset")}</p>}
        <p>{t("classificationRule", { rule: detail.rule, time: detail.calculated_at })}</p>
        <p>{t("batchCounts", queue.display_counts)}</p>
        <label>{t("batchFilter")}<select value={state} disabled={busy} onChange={(e) => openBatch(batchId, 1, e.target.value)}>
          {states.map((value) => <option key={value} value={value}>{t(`batchState_${value}`)}</option>)}
        </select></label>
        {!queue.results.length && <p>{t("batchNoItems")}</p>}
        <table><thead><tr><th>{t("classificationTask")}</th><th>{t("batchFilter")}</th><th>{t("batchCompare")}</th></tr></thead>
          <tbody>{queue.results.map((item) => <tr key={item.task_id}>
            <td>#{item.task_id}</td><td>{t(`batchState_${item.outcome}`)}</td>
            <td><Button data-task={item.task_id} onClick={() => showItem(item.task_id)}>{t("batchCompare")}</Button></td>
          </tr>)}</tbody></table>
        <Button disabled={busy || !queue.previous} onClick={() => openBatch(batchId, page - 1, state)}>{t("batchPrevious")}</Button>
        <Button disabled={busy || !queue.next} onClick={() => openBatch(batchId, page + 1, state)}>{t("batchNext")}</Button>
      </>}
    </>}
  </section>;
};
