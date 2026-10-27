import { useEffect, useMemo, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";

const SCHEMA = "ner-disagreements/v1", RULE = "ner-strict-entities/v1";
const EXPLANATION = "ner-overlap-explanation/v1", ENGINE = "nervaluate/0.2.0/strict-correct-counter";
const id = z.number().int().positive().safe(), count = z.number().int().min(0).max(200);
const hash = z.string().regex(/^[a-f0-9]{64}$/), ratio = z.object({
  numerator: z.string().regex(/^\d{1,4}$/), denominator: z.string().regex(/^[1-9]\d{0,3}$/),
});
const pin = z.object({ submission_id: id, assignment_id: id, assignee_id: id, task_id: id, revision: id,
  submitter_id: id, result_hash: hash, config_hash: hash, task_data_hash: hash });
const entity = z.object({ start: z.number().int().nonnegative(), end: id, label: z.string().min(1) });
const kinds = ["label_difference", "boundary_difference", "boundary_and_label_difference", "left_only", "right_only", "ambiguous_overlap"];
const reasons = ["unsupported_config", "config_mismatch", "task_data_mismatch", "empty_or_missing_text", "cancelled_or_missing",
  "missing_or_malformed", "missing_span_text_or_malformed", "invalid_span", "overlapping_or_duplicate_spans",
  "insufficient_inputs", "no_entities", "source_unavailable"];
const pair = z.object({ left: id, right: id, left_entities: count, right_entities: count, matching_entities: count,
  f1: ratio.nullable(), availability: z.enum(["available", "unavailable"]), reason: z.string().nullable() });
const details = z.object({ left: id, right: id, text: z.string(), control: z.object({ from_name: z.string(),
  to_name: z.string(), field: z.string(), labels: z.array(z.string()).min(1).max(100) }),
  left_entities: z.array(entity).max(200), right_entities: z.array(entity).max(200),
  matches: z.array(z.object({ left: count, right: count })).max(200),
  groups: z.array(z.object({ start: z.number().int().nonnegative(), end: id,
    left: z.array(count), right: z.array(count), kind: z.enum(kinds) })).max(400) });
const reportSchema = z.object({ schema: z.literal(SCHEMA), rule: z.literal(RULE), explanation: z.literal(EXPLANATION),
  engine: z.literal(ENGINE), viewer: hash, scope: z.object({ project_id: id, task_id: id, input_ids: z.array(id).min(1).max(20) }),
  manifest: z.object({ schema: z.literal(SCHEMA), rule: z.literal(RULE), explanation: z.literal(EXPLANATION), engine: z.literal(ENGINE),
    project_id: id, task_id: id, inputs: z.array(pin).min(1).max(20), viewer: hash, calculated_at: z.string(), result_hash: hash }),
  manifest_hash: hash, frozen: z.string().min(1).max(16384), calculated_at: z.string(), observed_at: z.string(),
  source_status: z.enum(["verified", "unavailable"]), receipt_verified: z.boolean(), result_hash: hash.nullable(),
  availability: z.enum(["available", "unavailable"]), reason: z.string().nullable(), input_count: id,
  total_pairs: z.number().int().min(0).max(190), comparable_pairs: z.number().int().nonnegative().optional(),
  unavailable_pairs: z.number().int().nonnegative().optional(), pairs: z.array(pair).max(190).nullable(), selected_pair: details.nullable() });
const sourceSchema = z.object({ schema: z.literal(SCHEMA), viewer: hash, max_inputs: z.literal(20),
  scope: z.object({ project_id: id, task_id: id }), sources: z.array(z.object({ id, assignment_id: id,
    assignment__assignee_id: id, assignment__task_id: id, submitted_by_id: id, revision: id, result_hash: hash,
    status: z.string() })).max(100) });
const failed = (r) => !r || r.error || r.$meta?.ok === false || typeof r.status === "number";
const equalIds = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
const requireValid = (value) => { if (!value) throw new Error("Invalid NER projection"); };
const gcd = (a, b) => b ? gcd(b, a % b) : a;

export const validateNerReport = (data, expected) => {
  const r = reportSchema.parse(data), ids = r.scope.input_ids;
  requireValid(r.scope.project_id === expected.project && r.scope.task_id === expected.task
    && r.manifest.project_id === expected.project && r.manifest.task_id === expected.task
    && equalIds(ids, expected.ids) && equalIds(r.manifest.inputs.map((p) => p.submission_id), ids)
    && ids.every((v, i) => !i || ids[i - 1] < v) && r.input_count === ids.length
    && r.total_pairs === ids.length * (ids.length - 1) / 2 && r.viewer === expected.viewer
    && r.manifest.viewer === r.viewer && r.manifest.calculated_at === r.calculated_at
    && r.manifest.inputs.every((p) => p.task_id === expected.task && p.assignee_id === p.submitter_id)
    && new Set(r.manifest.inputs.map((p) => p.submitter_id)).size === ids.length);
  if (expected.frozen) requireValid(r.frozen === expected.frozen && r.manifest_hash === expected.manifestHash
    && r.calculated_at === expected.time && r.manifest.result_hash === expected.resultHash);
  if (r.source_status === "unavailable") {
    requireValid(!r.receipt_verified && r.result_hash === null && r.availability === "unavailable"
      && r.reason === "source_unavailable" && r.pairs === null && r.selected_pair === null);
    return r;
  }
  requireValid(r.receipt_verified && r.result_hash === r.manifest.result_hash);
  if (r.pairs === null) {
    requireValid(r.availability === "unavailable" && Boolean(r.reason) && r.selected_pair === null);
    return r;
  }
  requireValid(r.pairs.length === r.total_pairs);
  let ordinal = 0;
  for (let a = 0; a < ids.length; a++) for (let b = a + 1; b < ids.length; b++) {
    const p = r.pairs[ordinal++], denominator = p.left_entities + p.right_entities;
    requireValid(p.left === ids[a] && p.right === ids[b] && p.matching_entities <= Math.min(p.left_entities, p.right_entities));
    if (!denominator) requireValid(p.f1 === null && p.availability === "unavailable" && p.reason === "no_entities");
    else {
      requireValid(p.f1 !== null && p.availability === "available" && p.reason === null);
      const n = BigInt(p.f1.numerator), d = BigInt(p.f1.denominator);
      requireValid(n * BigInt(denominator) === d * BigInt(2 * p.matching_entities) && gcd(n, d) === 1n);
    }
  }
  const comparable = r.pairs.filter((p) => p.f1 !== null).length;
  requireValid(r.comparable_pairs === comparable && r.unavailable_pairs === r.total_pairs - comparable
    && r.availability === (comparable ? "available" : "unavailable"));
  if (!r.total_pairs) { requireValid(r.selected_pair === null); return r; }
  const chosen = r.selected_pair, wanted = expected.pair ?? ids.slice(0, 2);
  requireValid(chosen && chosen.left === wanted[0] && chosen.right === wanted[1]);
  const selected = r.pairs.find((p) => p.left === chosen.left && p.right === chosen.right);
  requireValid(selected && chosen.left_entities.length === selected.left_entities
    && chosen.right_entities.length === selected.right_entities && chosen.matches.length === selected.matching_entities);
  const points = Array.from(chosen.text);
  for (const side of [chosen.left_entities, chosen.right_entities]) side.forEach((e, i) => {
    requireValid(e.start < e.end && e.end <= points.length && chosen.control.labels.includes(e.label)
      && (!i || side[i - 1].end <= e.start));
  });
  const seen = { left: new Set(), right: new Set() };
  const accept = (side, index) => {
    requireValid(index < chosen[`${side}_entities`].length && !seen[side].has(index)); seen[side].add(index);
  };
  chosen.matches.forEach((m) => {
    accept("left", m.left); accept("right", m.right);
    requireValid(JSON.stringify(chosen.left_entities[m.left]) === JSON.stringify(chosen.right_entities[m.right]));
  });
  chosen.groups.forEach((g) => {
    requireValid(g.start < g.end && g.end <= points.length && g.left.length + g.right.length > 0);
    g.left.forEach((i) => accept("left", i)); g.right.forEach((i) => accept("right", i));
  });
  requireValid(seen.left.size === chosen.left_entities.length && seen.right.size === chosen.right_entities.length);
  return r;
};

export const NerTitle = () => { const { t } = useLocaleTranslation("reviewBatches"); return t("nerTitle"); };
export const NerEntry = ({ projectId, onOpen, disabled }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI(), call = useRef(api.callApi);
  call.current = api.callApi;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let generation = 0, disposed = false;
    const read = async () => {
      const current = ++generation; setEnabled(false);
      try { const r = await call.current("nerCapability", { params: { pk: projectId }, errorFilter: () => true });
        if (!disposed && current === generation) setEnabled(!failed(r) && r.schema === SCHEMA && r.enabled === true);
      } catch { /* denied/withdrawn capabilities reveal no entry */ }
    };
    const blur = () => { generation++; setEnabled(false); };
    read(); window.addEventListener("focus", read); window.addEventListener("blur", blur);
    return () => { disposed = true; generation++; window.removeEventListener("focus", read); window.removeEventListener("blur", blur); };
  }, [projectId]);
  return enabled ? <Button size="small" look="outlined" disabled={disabled} onClick={onOpen}
    data-testid="open-ner-disagreement">{t("nerTitle")}</Button> : null;
};

const NerText = ({ text, entities, side }) => {
  const points = useMemo(() => Array.from(text), [text]), parts = []; let offset = 0;
  entities.forEach((e, i) => {
    parts.push(points.slice(offset, e.start).join(""));
    parts.push(<mark key={i} title={e.label}>{points.slice(e.start, e.end).join("")}</mark>);
    offset = e.end;
  });
  parts.push(points.slice(offset).join(""));
  return <pre data-testid={`ner-${side}-text`} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{parts}</pre>;
};

export const NerDisagreement = ({ projectId, initialTaskId = "" }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI(), call = useRef(api.callApi);
  call.current = api.callApi;
  const generation = useRef(0), frozen = useRef(null), scope = useRef(null), heading = useRef(null);
  const matrixRefs = useRef({}), detailHeading = useRef(null), returnPair = useRef(null);
  const [task, setTask] = useState(String(initialTaskId)), [rows, setRows] = useState(null);
  const [selected, setSelected] = useState([]), [report, setReport] = useState(null), [detail, setDetail] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(false);
  const clear = () => { generation.current++; setReport(null); setDetail(false); setBusy(false); };
  useEffect(() => {
    clear(); frozen.current = null; scope.current = null; setRows(null); setSelected([]); setError(false);
    setTask(String(initialTaskId)); heading.current?.focus();
    const blur = () => { clear(); setRows(null); };
    window.addEventListener("blur", blur);
    return () => { generation.current++; window.removeEventListener("blur", blur); };
  }, [projectId, initialTaskId]);
  useEffect(() => {
    if (detail) detailHeading.current?.focus();
    else if (report && returnPair.current) matrixRefs.current[returnPair.current]?.focus();
  }, [detail, report]);
  const request = async (method, params, accept) => {
    const current = ++generation.current; setReport(null); setError(false); setBusy(true);
    try {
      const r = await call.current(method, { params: { pk: projectId, task: Number(task), ...params }, errorFilter: () => true });
      if (current !== generation.current) return;
      if (failed(r)) throw new Error("read failed");
      accept(r);
    } catch { if (current === generation.current) { setReport(null); setRows(null); setError(true); } }
    finally { if (current === generation.current) setBusy(false); }
  };
  const load = () => {
    frozen.current = null; scope.current = null; setRows(null); setSelected([]);
    request("nerSources", {}, (raw) => {
      const r = sourceSchema.parse(raw);
      requireValid(r.scope.project_id === projectId && r.scope.task_id === Number(task)
        && r.sources.every((s) => s.assignment__task_id === Number(task) && s.assignment__assignee_id === s.submitted_by_id)
        && new Set(r.sources.map((s) => s.id)).size === r.sources.length);
      scope.current = { viewer: r.viewer }; setRows(r.sources);
    });
  };
  const compare = (pairIds = null, replay = false) => {
    const old = replay || pairIds ? frozen.current : null;
    const ids = old?.ids ?? [...selected].sort((a, b) => a - b), pair = pairIds ?? old?.pair ?? ids.slice(0, 2);
    const expected = { project: projectId, task: Number(task), ids, pair, viewer: old?.viewer ?? scope.current?.viewer,
      ...(old ? { frozen: old.frozen, manifestHash: old.manifestHash, time: old.time, resultHash: old.resultHash } : {}) };
    const params = old ? { frozen: old.frozen, ...(pair.length === 2 ? { left: pair[0], right: pair[1] } : {}) } : { submissions: ids.join(",") };
    request("nerReport", params, (raw) => {
      const r = validateNerReport(raw, expected);
      if (!old) frozen.current = { frozen: r.frozen, manifestHash: r.manifest_hash, resultHash: r.manifest.result_hash,
        time: r.calculated_at, viewer: r.viewer, ids, pair };
      else frozen.current = { ...old, pair };
      setReport(r); setDetail(Boolean(r.selected_pair)); returnPair.current = pair.join("-");
    });
  };
  const p = report?.selected_pair;
  return <section data-testid="ner-workspace" aria-busy={busy}>
    <h3 ref={heading} tabIndex={-1}>{t("nerTitle")}</h3>
    <p>{t("nerBoundary")}</p>
    <label>{t("classificationTask")}<input type="number" min="1" disabled={busy} value={task} onChange={(e) => {
      clear(); frozen.current = null; scope.current = null; setTask(e.target.value); setRows(null); setSelected([]); setError(false);
    }} /></label>
    <Button data-testid="ner-load" look="outlined" disabled={busy || !/^[1-9]\d*$/.test(task) || !Number.isSafeInteger(Number(task))}
      onClick={load}>{t("classificationLoad")}</Button>
    {busy && <p role="status">{t("classificationLoading")}</p>}
    {error && <p role="alert">{t("classificationReadFailed")}</p>}
    {!report && rows && <fieldset disabled={busy}>
      <legend>{t("classificationChoose")}</legend>
      {!rows.length && <p>{t("classificationNoSources")}</p>}
      {rows.map((row) => <label key={row.id} style={{ display: "block" }}><input type="checkbox" checked={selected.includes(row.id)}
        onChange={(e) => {
          frozen.current = null; clear();
          setSelected((old) => e.target.checked ? [...old.filter((id) => rows.find((r) => r.id === id).submitted_by_id !== row.submitted_by_id), row.id]
            : old.filter((id) => id !== row.id));
        }} />{t("classificationSource", { id: row.id, revision: row.revision, actor: row.submitted_by_id })}</label>)}
      <Button data-testid="ner-compare" disabled={selected.length < 2 || selected.length > 20} onClick={() => compare()}>
        {t("classificationCompare")}</Button>
    </fieldset>}
    {frozen.current && <Button data-testid="ner-read" look="outlined" disabled={busy} onClick={() => compare(null, true)}>
      {t("classificationRefreshFrozen")}</Button>}
    {report && <>
      <p>{t("nerFormula")}</p><p>{report.source_status === "verified" && report.pairs
        ? t("nerPairCoverage", { comparable: report.comparable_pairs, total: report.total_pairs })
        : t("nerFrozenScope", { inputs: report.input_count, total: report.total_pairs })}</p>
      <p>{t("classificationRule", { rule: report.rule, time: report.calculated_at })}</p>
      <code style={{ display: "block", overflowWrap: "anywhere" }}>{report.manifest_hash}</code>
      <details><summary>{t("nerFrozenInputs")}</summary><ul>{report.manifest.inputs.map((p) => <li key={p.submission_id}>
        {t("classificationSource", { id: p.submission_id, revision: p.revision, actor: p.submitter_id })}
        <code style={{ display: "block", overflowWrap: "anywhere" }}>{p.result_hash}</code>
      </li>)}</ul></details>
      <p>{t("classificationObserved", { time: report.observed_at })}</p>
      {report.availability === "unavailable" && <p role="status" data-testid="ner-unavailable">{t("nerUnavailable", {
        reason: t(`nerReason_${reasons.includes(report.reason) ? report.reason : "unknown"}`) })}</p>}
      {report.pairs && <div data-testid="ner-matrix" aria-label={t("nerMatrix")}>
        {report.pairs.map((pair) => <Button key={`${pair.left}-${pair.right}`} look="outlined" disabled={busy}
          ref={(node) => { matrixRefs.current[`${pair.left}-${pair.right}`] = node; }}
          data-testid={`ner-pair-${pair.left}-${pair.right}`} onClick={() => compare([pair.left, pair.right])}>
          {t("nerPair", { left: pair.left, right: pair.right, rate: pair.f1
            ? `${(Number(pair.f1.numerator) / Number(pair.f1.denominator) * 100).toFixed(2)}%` : t("nerNoEntities") })}
        </Button>)}
      </div>}
      {detail && p && <section data-testid="ner-detail">
        <h4 ref={detailHeading} tabIndex={-1}>{t("nerDetail", { left: p.left, right: p.right })}</h4>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 16 }}>
          {["left", "right"].map((side) => <section key={side}><h5>{t("nerSide", { id: p[side] })}</h5>
            <NerText text={p.text} entities={p[`${side}_entities`]} side={side} />
            <ul>{p[`${side}_entities`].map((e, i) => <li key={i}>{e.start}–{e.end}: {e.label}</li>)}</ul>
          </section>)}
        </div>
        <p>{t("nerMatched", { count: p.matches.length })}</p>
        <ul>{p.groups.map((g, i) => <li key={i} data-testid="ner-difference">{t(`nerDifference_${g.kind}`)}: {g.start}–{g.end}</li>)}</ul>
        <Button data-testid="ner-back-matrix" look="outlined" onClick={() => setDetail(false)}>{t("nerBackMatrix")}</Button>
      </section>}
      <Button data-testid="ner-change-revisions" look="outlined" onClick={() => { clear(); frozen.current = null; heading.current?.focus(); }}>
        {t("nerChangeRevisions")}</Button>
    </>}
  </section>;
};
