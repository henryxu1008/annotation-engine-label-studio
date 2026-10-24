import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { v4 as uuidv4 } from "uuid";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { ClassificationResult } from "./ClassificationDisagreement";
import "./ReviewBatches.scss";
import "./ClassificationBatches.scss";

const id = z.number().int().positive().safe(), count = z.number().int().nonnegative().safe();
const hash = z.string().length(64), state = z.enum(["pending", "resolved", "cancelled"]);
const capsSchema = z.object({ enabled: z.literal(true), viewer_hash: hash, can_read: z.boolean(), can_create: z.boolean(), can_decide: z.boolean(), can_cancel: z.boolean() });
const meta = z.object({ id, schema: z.literal("classification-arbitrations/v1"), decision_rule: z.literal("adopt-exact-submission/v1"),
  batch_id: id, task_id: id, batch_name: z.string(), assignee_id: id, input_set_hash: hash, viewer_hash: hash, status: state, version: id,
  created_at: z.string(), calculated_at: z.string(), can_decide: z.boolean(), can_cancel: z.boolean() });
const pagination = { count, next: z.string().nullable(), previous: z.string().nullable() };
const listSchema = z.object({ ...pagination, viewer_hash: hash, results: z.array(meta) });
const rosterSchema = z.object({ ...pagination, results: z.array(z.object({ id, name: z.string() })), batch_id: id, task_id: id,
  batch_name: z.string(), origin_manifest_hash: hash, input_set_hash: hash, viewer_hash: hash });
const pin = z.object({ submission_id: id, revision: id, submitter_id: id.nullable(), result_hash: z.string(), config_hash: z.string() });
const reportSchema = z.object({ status: z.literal("comparable"), outcome: z.literal("disagreement"), task_id: id,
  reason: z.null(), agreement: z.number().min(0).max(1), matching_pairs: count, total_pairs: id, inputs: z.array(pin).min(2).max(20),
  locations: z.array(z.object({ from_name: z.string(), to_name: z.string(), matching_pairs: count, total_pairs: id,
    agreement: z.number().min(0).max(1), disagrees: z.boolean(), values: z.array(z.object({ submission_id: id, choices: z.array(z.string()) })) })) });
const detailSchema = meta.extend({ observed_at: z.string(), available: z.boolean(), reason: z.string().nullable(),
  texts: z.array(z.object({ name: z.string(), text: z.string().max(65536) })).max(20), report: reportSchema.nullable(),
  decision: z.object({ id, actor_id: id, created_at: z.string(), winner_submission_id: id, winner_revision: id, reason: z.string(), rule: z.literal("adopt-exact-submission/v1") }).nullable(),
  cancellation: z.object({ actor_id: id, created_at: z.string(), reason: z.string() }).nullable(),
}).refine((r) => r.available ? r.report !== null && r.report.task_id === r.task_id && r.texts.length > 0 : r.report === null && r.texts.length === 0);
const receipt = z.object({ operation: z.enum(["create", "decision", "cancel"]), key: z.string().uuid(), status_at_commit: state, version_at_commit: id });
const createdSchema = meta.extend({ replayed: z.boolean(), receipt, origin_manifest_hash: hash });
const commandSchema = z.object({ id, status: z.enum(["resolved", "cancelled"]), version: id, replayed: z.boolean(), receipt,
  decision_id: id.optional(), winner_submission_id: id.optional() });
const failed = (r) => !r || r.error || r.$meta?.ok === false || typeof r.status === "number";
const rejected = { ARBITRATION_INVALID_INPUT: 400, ARBITRATION_INVALID_KEY: 400, ARBITRATION_ORIGIN_CHANGED: 409,
  ARBITRATION_INELIGIBLE: 409, ARBITRATION_ASSIGNEE_UNAVAILABLE: 409, ARBITRATION_DUPLICATE_ACTIVE: 409,
  ARBITRATION_SOURCE_UNAVAILABLE: 409, ARBITRATION_SOURCE_CHANGED: 409, ARBITRATION_BUDGET_EXCEEDED: 422,
  ARBITRATION_CAS_CONFLICT: 409, ARBITRATION_INPUT_SET_CHANGED: 409, ARBITRATION_TERMINAL: 409 };

export const ClassificationArbitrationTitle = () => {
  const { t } = useLocaleTranslation("reviewBatches");
  return t("arbitrationTitle");
};

export const ClassificationArbitrationEntry = ({ projectId, onOpen, disabled, dispatch = false }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI();
  const call = useRef(api.callApi); call.current = api.callApi;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let disposed = false; setEnabled(false);
    if (!onOpen) return;
    Promise.resolve(call.current("classificationArbitrationCapability", { params: { pk: projectId }, errorFilter: () => true })).then((r) => {
      const parsed = !failed(r) && capsSchema.safeParse(r);
      if (!disposed) setEnabled(Boolean(parsed?.success && (dispatch ? parsed.data.can_create : parsed.data.can_read)));
    }).catch(() => {});
    return () => { disposed = true; };
  }, [projectId, dispatch, Boolean(onOpen)]);
  return enabled ? <Button disabled={disabled} onClick={onOpen} data-testid={dispatch ? "dispatch-classification-arbitration" : "open-classification-arbitrations"}>
    {t(dispatch ? "arbitrationDispatch" : "arbitrationTitle")}</Button> : null;
};

export const ClassificationArbitrations = ({ projectId, initialOrigin = null }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI();
  const call = useRef(api.callApi); call.current = api.callApi;
  const generation = useRef(0), pending = useRef(null), selected = useRef(null), heading = useRef(null);
  const [view, setView] = useState(initialOrigin ? "create" : "list"), [busy, setBusy] = useState(false);
  const [unknown, setUnknown] = useState(false), [error, setError] = useState(""), [saved, setSaved] = useState(null);
  const [caps, setCaps] = useState(null), [list, setList] = useState(null), [data, setData] = useState(null), [roster, setRoster] = useState(null);
  const [page, setPage] = useState(1), [filter, setFilter] = useState("pending"), [rosterPage, setRosterPage] = useState(1);
  const [assignee, setAssignee] = useState(""), [winner, setWinner] = useState(""), [reason, setReason] = useState("");
  const clear = () => { setCaps(null); setList(null); setData(null); setRoster(null); };
  const fetch = async (method, params, schema, body, headers) => {
    const r = await call.current(method, { params: { pk: projectId, ...params }, body, headers, errorFilter: () => true });
    if (failed(r)) {
      const e = new Error("unavailable"), code = r?.response?.display_context?.reason;
      e.rejected = r?.$meta?.ok === false && typeof code === "string" && Object.prototype.hasOwnProperty.call(rejected, code) && rejected[code] === r.$meta.status;
      throw e;
    }
    return schema.parse(r);
  };
  const read = async (work) => {
    const current = ++generation.current;
    clear(); setBusy(true); setError("");
    try { await work(() => current === generation.current); }
    catch { if (current === generation.current) { clear(); setError("arbitrationReadFailed"); } }
    finally { if (current === generation.current) setBusy(false); }
  };
  const loadList = (targetPage = 1, targetFilter = filter) => {
    setView("list"); setPage(targetPage); setFilter(targetFilter);
    return read(async (current) => {
      const [capability, rows] = await Promise.all([fetch("classificationArbitrationCapability", {}, capsSchema),
        fetch("classificationArbitrations", { page: targetPage, state: targetFilter }, listSchema)]);
      if (!capability.can_read || capability.viewer_hash !== rows.viewer_hash
        || rows.results.some((row) => row.viewer_hash !== rows.viewer_hash)) throw new Error("read permission changed");
      if (current()) { setCaps(capability); setList(rows); }
    });
  };
  const openTask = (identifier) => {
    selected.current = identifier; setView("detail"); setWinner(""); setReason("");
    return read(async (current) => {
      const [capability, detail] = await Promise.all([fetch("classificationArbitrationCapability", {}, capsSchema),
        fetch("classificationArbitrationDetail", { arbitration: identifier }, detailSchema)]);
      if (!capability.can_read || capability.viewer_hash !== detail.viewer_hash || detail.id !== identifier) throw new Error("scope changed");
      if (current()) { setCaps(capability); setData(detail); }
    });
  };
  const loadOrigin = (targetPage = 1) => {
    setView("create"); setRosterPage(targetPage); setAssignee("");
    return read(async (current) => {
      const [capability, candidates] = await Promise.all([fetch("classificationArbitrationCapability", {}, capsSchema),
        fetch("classificationArbitrationAssignees", { ...initialOrigin, page: targetPage, page_size: 100 }, rosterSchema)]);
      if (candidates.batch_id !== initialOrigin.batch_id || candidates.task_id !== initialOrigin.task_id
        || candidates.origin_manifest_hash !== initialOrigin.expected_manifest_hash || !capability.can_read || !capability.can_create
        || capability.viewer_hash !== candidates.viewer_hash) throw new Error("scope changed");
      if (current()) { setCaps(capability); setRoster(candidates); }
    });
  };
  useEffect(() => {
    pending.current = null; selected.current = null; setUnknown(false); setSaved(null); setWinner(""); setReason("");
    initialOrigin ? loadOrigin() : loadList(1, "pending");
    const blur = () => { generation.current += 1; clear(); setBusy(false); setError("arbitrationReadFailed");
      if (pending.current?.attempted) setUnknown(true);
    };
    window.addEventListener("blur", blur);
    heading.current?.focus();
    return () => { generation.current += 1; window.removeEventListener("blur", blur); };
  }, [projectId]);
  const send = async (intent = pending.current) => {
    if (!intent) return;
    const recovering = intent.attempted; intent.attempted = true; pending.current = intent;
    const current = ++generation.current;
    clear(); setBusy(true); setError("");
    try {
      const methods = { create: "createClassificationArbitration", decision: "resolveClassificationArbitration", cancel: "cancelClassificationArbitration" };
      const result = await fetch(methods[intent.action], intent.identifier ? { arbitration: intent.identifier } : {},
        intent.action === "create" ? createdSchema : commandSchema, intent.body, { "Idempotency-Key": intent.key });
      if (current !== generation.current) return;
      const expectedState = { create: "pending", decision: "resolved", cancel: "cancelled" }[intent.action];
      if (result.receipt.key !== intent.key || result.receipt.operation !== intent.action
        || result.receipt.status_at_commit !== expectedState || result.receipt.version_at_commit !== (intent.action === "create" ? 1 : 2)
        || (intent.action !== "create" && (result.status !== expectedState || result.version !== 2))
        || (intent.identifier && result.id !== intent.identifier)
        || (intent.action === "create" && (result.batch_id !== intent.body.batch_id || result.task_id !== intent.body.task_id
          || result.assignee_id !== intent.body.assignee_id || result.origin_manifest_hash !== intent.body.expected_manifest_hash
          || result.input_set_hash !== intent.inputHash))
        || (intent.action === "decision" && result.winner_submission_id !== intent.body.winner_submission_id)) throw new Error("acknowledgement changed");
      pending.current = null; setUnknown(false); setSaved({ action: intent.action, id: result.id });
      await openTask(result.id);
    } catch (e) {
      if (current === generation.current) {
        if (!recovering && e?.rejected === true) { pending.current = null; setUnknown(false); setError("arbitrationRejected"); }
        else { setUnknown(true); setError("arbitrationReadFailed"); }
      }
    } finally { if (current === generation.current) setBusy(false); }
  };
  const begin = (action, body, identifier = null) => {
    if (busy || unknown || pending.current) return;
    send({ action, body, identifier, key: uuidv4(), attempted: false, inputHash: roster?.input_set_hash });
  };
  const refresh = () => view === "create" ? loadOrigin(rosterPage) : view === "detail" && selected.current ? openTask(selected.current) : loadList(page);
  const report = data?.available ? { ...data.report, manifest_hash: data.input_set_hash, observed_at: data.observed_at,
    manifest: { inputs: data.report.inputs, rule: "classification-exact-sets/v1", calculated_at: data.calculated_at } } : null;
  return <section className="review-batches classification-batches" data-testid="classification-arbitrations" aria-busy={busy}>
    <h3 ref={heading} tabIndex={-1}>{t("arbitrationTitle")}</h3>
    <p>{t("arbitrationBoundary")}</p>
    {busy && <p role="status">{t("classificationLoading")}</p>}
    {error && <p role="alert">{t(error)}</p>}
    {saved && <p role="status" data-testid="arbitration-saved">{t(`arbitrationSaved_${saved.action}`, { id: saved.id })}</p>}
    {unknown && <p role="status">{t("arbitrationUnknown")} <Button disabled={busy} onClick={() => send()}>{t("arbitrationRecover")}</Button></p>}
    {!busy && <Button onClick={refresh}>{t("batchRefresh")}</Button>}
    {view !== "list" && <Button disabled={busy} onClick={() => loadList(page)}>{t("arbitrationBack")}</Button>}
    {view === "list" && list && <>
      {caps?.can_create && <p>{t("arbitrationStartHint")}</p>}
      <label>{t("arbitrationState")}<select value={filter} disabled={busy} onChange={(e) => loadList(1, e.target.value)}>
        {["pending", "resolved", "cancelled", "all"].map((s) => <option key={s} value={s}>{t(`arbitrationState_${s}`)}</option>)}</select></label>
      {!list.results.length && <p>{t("arbitrationEmpty")}</p>}
      <ul>{list.results.map((row) => <li key={row.id}><Button data-arbitration={row.id} onClick={() => openTask(row.id)}>
        {t("arbitrationTask", { task: row.task_id, batch: row.batch_name })} — {t(`arbitrationState_${row.status}`)}</Button></li>)}</ul>
      <Button disabled={busy || !list.previous} onClick={() => loadList(page - 1)}>{t("batchPrevious")}</Button>
      <Button disabled={busy || !list.next} onClick={() => loadList(page + 1)}>{t("batchNext")}</Button>
    </>}
    {view === "create" && roster && caps?.can_create && <fieldset disabled={busy || unknown}>
      <legend>{t("arbitrationDispatch")}</legend><p>{t("arbitrationTask", { task: roster.task_id, batch: roster.batch_name })}</p>
      <label>{t("arbitrationAssignee")}<select value={assignee} onChange={(e) => setAssignee(e.target.value)}>
        <option value="">{t("arbitrationChooseAssignee")}</option>{roster.results.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}</select></label>
      {!roster.results.length && <p>{t("arbitrationNoAssignees")}</p>}
      <Button disabled={!roster.previous} onClick={() => loadOrigin(rosterPage - 1)}>{t("batchPrevious")}</Button>
      <Button disabled={!roster.next} onClick={() => loadOrigin(rosterPage + 1)}>{t("batchNext")}</Button>
      <Button disabled={!assignee} onClick={() => begin("create", { ...initialOrigin, assignee_id: Number(assignee) })}>{t("arbitrationConfirmDispatch")}</Button>
    </fieldset>}
    {view === "detail" && data && <>
      <h4>{t("arbitrationTask", { task: data.task_id, batch: data.batch_name })}</h4><p>{t(`arbitrationState_${data.status}`)}</p>
      {data.decision && <p data-testid="arbitration-decision">{t("arbitrationDecision", { source: data.decision.winner_submission_id,
        revision: data.decision.winner_revision, actor: data.decision.actor_id, time: data.decision.created_at })}: {data.decision.reason}</p>}
      {data.cancellation && <p>{t("arbitrationCancelled", { actor: data.cancellation.actor_id, time: data.cancellation.created_at })}: {data.cancellation.reason}</p>}
      {!data.available && <p role="status" data-testid="arbitration-unavailable">{t("arbitrationUnavailable")}</p>}
      {data.available && <div data-testid="arbitration-text">{data.texts.map((text) => <section key={text.name}><h4>{text.name}</h4>
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{text.text}</pre></section>)}</div>}
      {caps?.can_decide && data.can_decide && data.available && <fieldset disabled={busy || unknown}>
        <legend>{t("arbitrationChooseWinner")}</legend>
        {data.report.inputs.map((source) => <label key={source.submission_id} style={{ display: "block" }}>
          <input type="radio" name="arbitration-winner" value={source.submission_id} checked={winner === String(source.submission_id)} onChange={(e) => setWinner(e.target.value)} />
          {t("classificationSource", { id: source.submission_id, revision: source.revision, actor: source.submitter_id ?? "—" })}: {data.report.locations.map((location) => `${location.from_name}: ${location.values.find((v) => v.submission_id === source.submission_id)?.choices.join(", ") ?? ""}`).join("; ")}
        </label>)}
        <label>{t("arbitrationReason")}<textarea maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
        <Button disabled={!winner || !reason.trim()} onClick={() => begin("decision", { expected_version: data.version,
          expected_input_set_hash: data.input_set_hash, winner_submission_id: Number(winner), reason }, data.id)}>{t("arbitrationAdopt")}</Button>
      </fieldset>}
      {caps?.can_cancel && data.can_cancel && <fieldset disabled={busy || unknown}><legend>{t("arbitrationCancel")}</legend>
        <label>{t("arbitrationReason")}<textarea maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
        <Button disabled={!reason.trim()} onClick={() => begin("cancel", { expected_version: data.version,
          expected_input_set_hash: data.input_set_hash, reason }, data.id)}>{t("arbitrationConfirmCancel")}</Button></fieldset>}
      {report && <details><summary>{t("arbitrationEvidence")}</summary><ClassificationResult report={report} durable /></details>}
    </>}
  </section>;
};
