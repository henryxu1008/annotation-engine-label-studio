import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";

const id = z.number().int().positive().safe();
const pin = z.object({ submission_id: id, revision: id, assignment_id: id, task_id: id,
  submitter_id: id.nullable(), result_hash: z.string(), config_hash: z.string() });
const sourceResponse = z.object({ sources: z.array(pin.extend({ status: z.string() })).max(100), max_inputs: z.literal(20) });
const reportResponse = z.object({
  schema: z.literal("classification-disagreements/v1"), frozen: z.string(), manifest_hash: z.string(),
  manifest: z.object({ project_id: id, task_id: id, rule: z.literal("classification-exact-sets/v1"),
    calculated_at: z.string(), inputs: z.array(pin).max(20) }),
  observed_at: z.string(), status: z.enum(["comparable", "unavailable"]), reason: z.string().nullable(),
  matching_pairs: z.number().int().nullable(), total_pairs: z.number().int().nullable(),
  agreement: z.number().min(0).max(1).nullable(), locations: z.array(z.object({
    from_name: z.string(), to_name: z.string(), matching_pairs: z.number().int(), total_pairs: z.number().int(),
    agreement: z.number().min(0).max(1), disagrees: z.boolean(),
    values: z.array(z.object({ submission_id: id, choices: z.array(z.string()) })),
  })),
}).refine((r) => r.status === "unavailable" ? r.agreement === null && r.locations.length === 0
  : r.agreement !== null && r.total_pairs > 0);
const failed = (r) => !r || r.error || r.$meta?.ok === false || typeof r.status === "number";
const reasons = ["insufficient_inputs", "duplicate_submitter", "missing_submitter", "invalid_source", "unsupported_config",
  "cancelled_or_missing", "missing_or_malformed", "config_mismatch", "task_data_mismatch", "source_unavailable",
  "missing_submission", "latest_revision_unusable", "source_integrity_failed"];

// Shared presentation only: the batch caller supplies its separately authorized,
// exact pinned projection. This component never selects or fetches sources.
export const ClassificationResult = ({ report, durable = false }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const hashStyle = { display: "block", whiteSpace: "normal", overflowWrap: "anywhere" };
  return <section data-testid="classification-report">
    <p>{t("classificationRule", { rule: report.manifest.rule, time: report.manifest.calculated_at })}</p>
    <code style={hashStyle}>{report.manifest_hash}</code>
    <p>{t("classificationObserved", { time: report.observed_at })}</p>
    <p>{t(durable ? "batchFrozenBoundary" : "classificationFrozenBoundary")}</p>
    <ul>{report.manifest.inputs.map((r) => <li key={r.submission_id}>
      {t("classificationSource", { id: r.submission_id, revision: r.revision, actor: r.submitter_id ?? "—" })}
      <code style={hashStyle}>{r.result_hash}</code><code style={hashStyle}>{r.config_hash}</code>
    </li>)}</ul>
    {report.status === "unavailable" ? <p role="status" data-testid="classification-unavailable">
      {t("classificationUnavailable")}: {t(reasons.includes(report.reason) ? `classificationReason_${report.reason}` : "classificationReason_unknown")}
    </p> : <>
      <p data-testid="classification-agreement">{t("classificationAgreement", { matched: report.matching_pairs,
        total: report.total_pairs, rate: (report.agreement * 100).toFixed(2) })}</p>
      <p>{t("classificationFormula")}</p>
      {report.locations.map((location) => <section key={location.from_name} data-testid="classification-location">
        <h4>{location.from_name} → {location.to_name}</h4>
        <p>{t(location.disagrees ? "classificationDisagrees" : "classificationAgrees")}</p>
        <ul>{location.values.map((value) => <li key={value.submission_id}>#{value.submission_id}: {value.choices.join(", ")}</li>)}</ul>
      </section>)}
    </>}
  </section>;
};

export const ClassificationTitle = () => {
  const { t } = useLocaleTranslation("reviewBatches");
  return t("classificationTitle");
};

export const ClassificationEntry = ({ projectId, onOpen, disabled }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const api = useAPI();
  const call = useRef(api.callApi); call.current = api.callApi;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let disposed = false; setEnabled(false);
    Promise.resolve(call.current("classificationCapability", { params: { pk: projectId }, errorFilter: () => true }))
      .then((r) => { if (!disposed) setEnabled(!failed(r) && r.enabled === true); }).catch(() => {});
    return () => { disposed = true; };
  }, [projectId]);
  return enabled ? <Button size="small" look="outlined" disabled={disabled} onClick={onOpen}
    data-testid="open-classification-disagreement">{t("classificationTitle")}</Button> : null;
};

export const ClassificationDisagreement = ({ projectId, initialTaskId = "" }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const api = useAPI();
  const call = useRef(api.callApi); call.current = api.callApi;
  const generation = useRef(0), receipt = useRef(null), heading = useRef(null);
  const [task, setTask] = useState(String(initialTaskId));
  const [rows, setRows] = useState(null), [selected, setSelected] = useState([]);
  const [report, setReport] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState(false);
  const clear = () => { generation.current += 1; setReport(null); setBusy(false); };
  useEffect(() => {
    receipt.current = null; setRows(null); setSelected([]); setError(false); clear(); heading.current?.focus();
    window.addEventListener("blur", clear);
    return () => { generation.current += 1; window.removeEventListener("blur", clear); };
  }, [projectId]);
  const request = async (method, params, schema, accept) => {
    const current = ++generation.current;
    setReport(null); setError(false); setBusy(true);
    try {
      const result = await call.current(method, { params: { pk: projectId, task: Number(task), ...params }, errorFilter: () => true });
      if (current !== generation.current) return;
      if (failed(result) || !schema.safeParse(result).success) throw new Error("unavailable");
      accept(result);
    } catch { if (current === generation.current) { setError(true); setRows(null); setSelected([]); } }
    finally { if (current === generation.current) setBusy(false); }
  };
  const load = () => {
    receipt.current = null; setRows(null); setSelected([]);
    request("classificationSources", {}, sourceResponse, (r) => setRows(r.sources));
  };
  const compare = (frozen = null) => request("classificationReport", frozen ? { frozen } : { submissions: selected.join(",") },
    reportResponse, (r) => {
      if (r.manifest.project_id !== projectId || r.manifest.task_id !== Number(task)) throw new Error("scope changed");
      receipt.current = r.frozen; setReport(r);
    });
  return <section data-testid="classification-workspace" aria-busy={busy}>
    <h3 ref={heading} tabIndex={-1}>{t("classificationTitle")}</h3>
    <p>{t("classificationBoundary")}</p>
    <label>{t("classificationTask")}<input type="number" min="1" value={task} disabled={busy} onChange={(e) => {
      clear(); receipt.current = null; setTask(e.target.value); setRows(null); setSelected([]); setError(false);
    }} /></label>
    <Button disabled={busy || !/^\d+$/.test(task) || Number(task) <= 0} onClick={load}>{t("classificationLoad")}</Button>
    {busy && <p role="status">{t("classificationLoading")}</p>}
    {error && <p role="alert">{t("classificationReadFailed")}</p>}
    {rows && <fieldset disabled={busy}>
      <legend>{t("classificationChoose")}</legend>
      {!rows.length && <p>{t("classificationNoSources")}</p>}
      {rows.map((r) => <label key={r.submission_id} style={{ display: "block" }}>
        <input type="checkbox" checked={selected.includes(r.submission_id)} onChange={(e) => {
          receipt.current = null; setReport(null);
          setSelected((old) => e.target.checked ? [...old, r.submission_id] : old.filter((id) => id !== r.submission_id));
        }} />{t("classificationSource", { id: r.submission_id, revision: r.revision, actor: r.submitter_id ?? "—" })}
      </label>)}
      <Button disabled={!selected.length || selected.length > 20} onClick={() => compare()}>{t("classificationCompare")}</Button>
    </fieldset>}
    {receipt.current && <Button disabled={busy} onClick={() => compare(receipt.current)}>{t("classificationRefreshFrozen")}</Button>}
    {report && <ClassificationResult report={report} />}
  </section>;
};
