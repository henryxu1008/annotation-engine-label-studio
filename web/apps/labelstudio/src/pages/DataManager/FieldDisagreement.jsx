import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { fieldDigest } from "./fieldEvidence";
import { trim } from "es-toolkit/string";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";

const SCHEMA = "field-disagreements/v1";
const RULE = "field-lf-ascii-trim/v1";
const EXPLANATION = "field-value-differences/v1";
const ENGINE = "python-string-equality/v1";
const id = z.number().int().positive().safe();
const count = z.number().int().min(0).max(20);
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const ratio = z.object({ numerator: z.string().regex(/^\d{1,2}$/), denominator: z.string().regex(/^[1-9]\d?$/) });
const pin = z
  .object({
    submission_id: id,
    assignment_id: id,
    assignee_id: id,
    task_id: id,
    revision: id,
    submitter_id: id,
    result_hash: hash,
    config_hash: hash,
    task_data_hash: hash,
  })
  .strict();
const value = z.object({ raw: z.string().max(4096), normalized: z.string().max(4096) });
const entry = z.object({
  name: z.string().min(1),
  left: value,
  right: value,
  informative: z.boolean(),
  raw_equal: z.boolean(),
  normalized_equal: z.boolean(),
  kind: z.enum(["equal", "format_only", "value_difference", "both_empty"]),
});
const pair = z.object({
  left: id,
  right: id,
  denominator: count,
  excluded_empty_fields: count,
  raw_matches: count,
  normalized_matches: count,
  raw_rate: ratio.nullable(),
  normalized_rate: ratio.nullable(),
  availability: z.enum(["available", "unavailable"]),
  reason: z.string().nullable(),
});
const reportSchema = z.object({
  schema: z.literal(SCHEMA),
  rule: z.literal(RULE),
  explanation: z.literal(EXPLANATION),
  engine: z.literal(ENGINE),
  viewer: hash,
  scope: z.object({ project_id: id, task_id: id, input_ids: z.array(id).min(2).max(20) }),
  manifest: z
    .object({
      schema: z.literal(SCHEMA),
      rule: z.literal(RULE),
      explanation: z.literal(EXPLANATION),
      engine: z.literal(ENGINE),
      project_id: id,
      task_id: id,
      inputs: z.array(pin).min(2).max(20),
      viewer: hash,
      calculated_at: z.string(),
      result_hash: hash,
      configured_fields: count.nullable(),
    })
    .strict(),
  manifest_hash: hash,
  frozen: z.string().min(1).max(16384),
  calculated_at: z.string(),
  observed_at: z.string(),
  source_status: z.enum(["verified", "unavailable"]),
  receipt_verified: z.boolean(),
  result_hash: hash.nullable(),
  availability: z.enum(["available", "unavailable"]),
  reason: z.string().nullable(),
  input_count: count,
  total_pairs: z.number().int().min(1).max(190),
  configured_fields: count.nullable(),
  comparable_pairs: z.number().int().nonnegative().optional(),
  unavailable_pairs: z.number().int().nonnegative().optional(),
  cost_per_pass: z
    .object({ normalized_values: z.number().int().nonnegative(), field_equalities: z.number().int().nonnegative() })
    .optional(),
  pairs: z.array(pair).max(190).nullable(),
  selected_pair: z.object({ left: id, right: id, fields: z.array(entry).min(1).max(20) }).nullable(),
});
const sourceSchema = z.object({
  schema: z.literal(SCHEMA),
  viewer: hash,
  max_inputs: z.literal(20),
  scope: z.object({ project_id: id, task_id: id }),
  sources: z
    .array(
      z.object({
        id,
        assignment_id: id,
        assignment__assignee_id: id,
        assignment__task_id: id,
        submitted_by_id: id,
        revision: id,
        result_hash: hash,
        status: z.string(),
      }),
    )
    .max(100),
});
const failed = (r) => !r || r.error || r.$meta?.ok === false || typeof r.status === "number";
const equalIds = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);
const requireValid = (v) => {
  if (!v) throw new Error("Invalid field projection");
};
const gcd = (a, b) => (b ? gcd(b, a % b) : a);
const normalize = (s) => trim(s.replace(/\r\n?/g, "\n"), [" ", "\t", "\n", "\v", "\f"]);
const reasons = [
  "unsupported_config",
  "config_mismatch",
  "task_data_mismatch",
  "empty_or_missing_text",
  "cancelled_or_missing",
  "missing_or_malformed",
  "missing_field",
  "no_values",
  "source_unavailable",
];

export const validateFieldReport = (raw, expected) => {
  const r = reportSchema.parse(raw);
  const ids = r.scope.input_ids;
  requireValid(fieldDigest(r.manifest) === r.manifest_hash);
  requireValid(
    r.scope.project_id === expected.project &&
      r.scope.task_id === expected.task &&
      r.manifest.project_id === expected.project &&
      r.manifest.task_id === expected.task &&
      equalIds(ids, expected.ids) &&
      equalIds(
        r.manifest.inputs.map((p) => p.submission_id),
        ids,
      ) &&
      ids.every((v, i) => !i || ids[i - 1] < v) &&
      r.input_count === ids.length &&
      r.total_pairs === (ids.length * (ids.length - 1)) / 2 &&
      r.viewer === expected.viewer &&
      r.manifest.viewer === r.viewer &&
      r.manifest.calculated_at === r.calculated_at &&
      r.manifest.configured_fields === r.configured_fields &&
      r.manifest.inputs.every((p) => p.task_id === expected.task && p.assignee_id === p.submitter_id) &&
      new Set(r.manifest.inputs.map((p) => p.submitter_id)).size === ids.length,
  );
  if (!expected.frozen)
    requireValid(
      expected.sources?.length === ids.length &&
        r.manifest.inputs.every((p) => {
          const s = expected.sources.find((s) => s?.id === p.submission_id);
          return (
            s &&
            s.assignment_id === p.assignment_id &&
            s.assignment__assignee_id === p.assignee_id &&
            s.assignment__task_id === p.task_id &&
            s.submitted_by_id === p.submitter_id &&
            s.revision === p.revision &&
            s.result_hash === p.result_hash
          );
        }),
    );
  if (expected.frozen)
    requireValid(
      r.frozen === expected.frozen &&
        r.manifest_hash === expected.manifestHash &&
        r.calculated_at === expected.time &&
        r.manifest.result_hash === expected.resultHash,
    );
  if (r.source_status === "unavailable") {
    requireValid(
      !r.receipt_verified &&
        r.result_hash === null &&
        r.availability === "unavailable" &&
        r.reason === "source_unavailable" &&
        r.pairs === null &&
        r.selected_pair === null,
    );
    return r;
  }
  requireValid(r.receipt_verified && r.result_hash === r.manifest.result_hash);
  return validateFieldComparison(r, expected);
};

export const fieldComparisonSchema = reportSchema
  .pick({
    availability: true,
    reason: true,
    input_count: true,
    total_pairs: true,
    configured_fields: true,
    comparable_pairs: true,
    unavailable_pairs: true,
    cost_per_pass: true,
    pairs: true,
    selected_pair: true,
  })
  .extend({ total_pairs: z.number().int().min(0).max(190) });

export const validateFieldComparison = (r, expected) => {
  fieldComparisonSchema.parse(r);
  const ids = expected.ids;
  if (r.pairs === null) {
    requireValid(
      r.availability === "unavailable" && (expected.reasons ?? reasons).includes(r.reason) && r.selected_pair === null,
    );
    return r;
  }
  requireValid(
    r.input_count === ids.length &&
      r.configured_fields > 0 &&
      r.pairs.length === r.total_pairs &&
      r.cost_per_pass?.normalized_values === ids.length * r.configured_fields &&
      r.cost_per_pass?.field_equalities === 2 * r.total_pairs * r.configured_fields,
  );
  let ordinal = 0;
  for (let a = 0; a < ids.length; a++)
    for (let b = a + 1; b < ids.length; b++) {
      const p = r.pairs[ordinal++];
      requireValid(
        p.left === ids[a] &&
          p.right === ids[b] &&
          p.denominator + p.excluded_empty_fields === r.configured_fields &&
          p.raw_matches <= p.normalized_matches &&
          p.normalized_matches <= p.denominator,
      );
      if (!p.denominator)
        requireValid(
          p.raw_rate === null &&
            p.normalized_rate === null &&
            p.availability === "unavailable" &&
            p.reason === "no_values",
        );
      else {
        requireValid(p.availability === "available" && p.reason === null);
        for (const name of ["raw", "normalized"]) {
          const rate = p[`${name}_rate`];
          requireValid(rate !== null);
          const n = BigInt(rate.numerator);
          const d = BigInt(rate.denominator);
          requireValid(n * BigInt(p.denominator) === d * BigInt(p[`${name}_matches`]) && gcd(n, d) === 1n);
        }
      }
    }
  const comparable = r.pairs.filter((p) => p.denominator > 0).length;
  requireValid(
    r.comparable_pairs === comparable &&
      r.unavailable_pairs === r.total_pairs - comparable &&
      r.availability === (comparable ? "available" : "unavailable") &&
      r.reason === (comparable ? null : "no_values"),
  );
  const chosen = r.selected_pair;
  const wanted = expected.pair ?? ids.slice(0, 2);
  requireValid(
    chosen &&
      chosen.left === wanted[0] &&
      chosen.right === wanted[1] &&
      chosen.fields.length === r.configured_fields &&
      new Set(chosen.fields.map((e) => e.name)).size === r.configured_fields,
  );
  const p = r.pairs.find((p) => p.left === chosen.left && p.right === chosen.right);
  requireValid(p);
  for (const e of chosen.fields) {
    requireValid(
      e.left.normalized === normalize(e.left.raw) &&
        e.right.normalized === normalize(e.right.raw) &&
        e.raw_equal === (e.left.raw === e.right.raw) &&
        e.normalized_equal === (e.left.normalized === e.right.normalized) &&
        e.informative === Boolean(e.left.normalized || e.right.normalized) &&
        e.kind ===
          (!e.informative
            ? "both_empty"
            : e.raw_equal
              ? "equal"
              : e.normalized_equal
                ? "format_only"
                : "value_difference"),
    );
  }
  requireValid(
    chosen.fields.filter((e) => e.informative).length === p.denominator &&
      chosen.fields.filter((e) => e.informative && e.raw_equal).length === p.raw_matches &&
      chosen.fields.filter((e) => e.informative && e.normalized_equal).length === p.normalized_matches,
  );
  return r;
};

export const FieldTitle = () => {
  const { t } = useLocaleTranslation("reviewBatches");
  return t("fieldTitle");
};

const rate = (r) => (r ? `${((Number(r.numerator) / Number(r.denominator)) * 100).toFixed(2)}%` : "—");
export const FieldResult = ({
  report,
  busy,
  detail,
  onSelectPair,
  onBack,
  matrixRefs,
  detailHeading,
  evidence,
  extraReasons = [],
}) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const p = report.selected_pair;
  return (
    <>
      <p>{t("fieldFormula")}</p>
      <p>{t("nerFrozenScope", { inputs: report.input_count, total: report.total_pairs })}</p>
      <p>{t("classificationRule", { rule: report.rule, time: report.calculated_at })}</p>
      <code style={{ display: "block", overflowWrap: "anywhere" }}>{evidence?.reference ?? report.manifest_hash}</code>
      <details>
        <summary>{t("nerFrozenInputs")}</summary>
        <ul>
          {(evidence?.inputs ?? report.manifest.inputs).map((p) => (
            <li key={p.submission_id}>
              {t("classificationSource", { id: p.submission_id, revision: p.revision, actor: p.submitter_id })}
              <code style={{ display: "block", overflowWrap: "anywhere" }}>{p.result_hash}</code>
            </li>
          ))}
        </ul>
      </details>
      <p>{t("classificationObserved", { time: report.observed_at })}</p>
      {report.availability === "unavailable" && (
        <p role="status" data-testid="field-unavailable">
          {t("fieldUnavailable", {
            reason: t(
              `fieldReason_${reasons.includes(report.reason) || extraReasons.includes(report.reason) ? report.reason : "unknown"}`,
            ),
          })}
        </p>
      )}
      {report.pairs && (
        <div data-testid="field-matrix" aria-label={t("nerMatrix")}>
          {report.pairs.map((pair) => (
            <Button
              key={`${pair.left}-${pair.right}`}
              look="outlined"
              disabled={busy}
              ref={(node) => {
                matrixRefs.current[`${pair.left}-${pair.right}`] = node;
              }}
              data-testid={`field-pair-${pair.left}-${pair.right}`}
              onClick={() => onSelectPair([pair.left, pair.right])}
            >
              {t("fieldPair", {
                left: pair.left,
                right: pair.right,
                raw: rate(pair.raw_rate),
                normalized: rate(pair.normalized_rate),
                denominator: pair.denominator,
              })}
            </Button>
          ))}
        </div>
      )}
      {detail && p && (
        <section data-testid="field-detail">
          <h4 ref={detailHeading} tabIndex={-1}>
            {t("nerDetail", { left: p.left, right: p.right })}
          </h4>
          {p.fields.map((e) => (
            <section key={e.name} data-testid="field-difference">
              <h5>
                {e.name}: {t(`fieldKind_${e.kind}`)}
              </h5>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 16 }}>
                {["left", "right"].map((side) => (
                  <section key={side}>
                    <p>{t("nerSide", { id: p[side] })}</p>
                    <p>{t("fieldRaw")}</p>
                    <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{e[side].raw}</pre>
                    <p>{t("fieldNormalized")}</p>
                    <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{e[side].normalized}</pre>
                  </section>
                ))}
              </div>
            </section>
          ))}
          <Button data-testid="field-back-matrix" look="outlined" onClick={onBack}>
            {t("nerBackMatrix")}
          </Button>
        </section>
      )}
    </>
  );
};

export const FieldEntry = ({ projectId, onOpen, disabled }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const api = useAPI();
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let generation = 0;
    let disposed = false;
    const read = async () => {
      const current = ++generation;
      setEnabled(false);
      try {
        const r = await call.current("fieldCapability", { params: { pk: projectId }, errorFilter: () => true });
        if (!disposed && current === generation)
          setEnabled(!failed(r) && r.schema === SCHEMA && r.enabled === true && hash.safeParse(r.viewer).success);
      } catch {
        /* denied/withdrawn capabilities reveal no entry */
      }
    };
    const blur = () => {
      generation++;
      setEnabled(false);
    };
    read();
    window.addEventListener("focus", read);
    window.addEventListener("blur", blur);
    return () => {
      disposed = true;
      generation++;
      window.removeEventListener("focus", read);
      window.removeEventListener("blur", blur);
    };
  }, [projectId]);
  return enabled ? (
    <Button size="small" look="outlined" disabled={disabled} onClick={onOpen} data-testid="open-field-disagreement">
      {t("fieldTitle")}
    </Button>
  ) : null;
};

export const FieldDisagreement = ({ projectId, initialTaskId = "" }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const api = useAPI();
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const generation = useRef(0);
  const frozen = useRef(null);
  const scope = useRef(null);
  const heading = useRef(null);
  const matrixRefs = useRef({});
  const detailHeading = useRef(null);
  const returnPair = useRef(null);
  const [task, setTask] = useState(String(initialTaskId));
  const [rows, setRows] = useState(null);
  const [selected, setSelected] = useState([]);
  const [report, setReport] = useState(null);
  const [detail, setDetail] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const clear = () => {
    generation.current++;
    setReport(null);
    setDetail(false);
    setBusy(false);
  };
  useEffect(() => {
    clear();
    frozen.current = null;
    scope.current = null;
    setRows(null);
    setSelected([]);
    setError(false);
    setTask(String(initialTaskId));
    heading.current?.focus();
    const blur = () => {
      clear();
      setRows(null);
    };
    window.addEventListener("blur", blur);
    return () => {
      generation.current++;
      window.removeEventListener("blur", blur);
    };
  }, [projectId, initialTaskId]);
  useEffect(() => {
    if (detail) detailHeading.current?.focus();
    else if (report && returnPair.current) matrixRefs.current[returnPair.current]?.focus();
  }, [detail, report]);
  const request = async (method, params, accept) => {
    const current = ++generation.current;
    setReport(null);
    setError(false);
    setBusy(true);
    try {
      const r = await call.current(method, {
        params: { pk: projectId, task: Number(task), ...params },
        errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (failed(r)) throw new Error("read failed");
      accept(r);
    } catch {
      if (current === generation.current) {
        setReport(null);
        setRows(null);
        setError(true);
      }
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };
  const load = () => {
    frozen.current = null;
    scope.current = null;
    setRows(null);
    setSelected([]);
    request("fieldSources", {}, (raw) => {
      const r = sourceSchema.parse(raw);
      requireValid(
        r.scope.project_id === projectId &&
          r.scope.task_id === Number(task) &&
          r.sources.every(
            (s) => s.assignment__task_id === Number(task) && s.assignment__assignee_id === s.submitted_by_id,
          ) &&
          new Set(r.sources.map((s) => s.id)).size === r.sources.length,
      );
      scope.current = { viewer: r.viewer };
      setRows(r.sources);
    });
  };
  const compare = (pairIds = null, replay = false) => {
    const old = replay || pairIds ? frozen.current : null;
    const ids = old?.ids ?? [...selected].sort((a, b) => a - b);
    const pair = pairIds ?? old?.pair ?? ids.slice(0, 2);
    const expected = {
      project: projectId,
      task: Number(task),
      ids,
      pair,
      viewer: old?.viewer ?? scope.current?.viewer,
      sources: old ? undefined : ids.map((id) => rows?.find((row) => row.id === id)),
      ...(old
        ? { frozen: old.frozen, manifestHash: old.manifestHash, time: old.time, resultHash: old.resultHash }
        : {}),
    };
    const params = old
      ? { frozen: old.frozen, ...(pair.length === 2 ? { left: pair[0], right: pair[1] } : {}) }
      : { submissions: ids.join(",") };
    request("fieldReport", params, (raw) => {
      const r = validateFieldReport(raw, expected);
      if (!old)
        frozen.current = {
          frozen: r.frozen,
          manifestHash: r.manifest_hash,
          resultHash: r.manifest.result_hash,
          time: r.calculated_at,
          viewer: r.viewer,
          ids,
          pair,
        };
      else frozen.current = { ...old, pair };
      setReport(r);
      setDetail(Boolean(r.selected_pair));
      returnPair.current = pair.join("-");
    });
  };
  return (
    <section data-testid="field-workspace" aria-busy={busy}>
      <h3 ref={heading} tabIndex={-1}>
        {t("fieldTitle")}
      </h3>
      <p>{t("fieldBoundary")}</p>
      <label>
        {t("classificationTask")}
        <input
          type="number"
          min="1"
          disabled={busy}
          value={task}
          onChange={(e) => {
            clear();
            frozen.current = null;
            scope.current = null;
            setTask(e.target.value);
            setRows(null);
            setSelected([]);
            setError(false);
          }}
        />
      </label>
      <Button
        data-testid="field-load"
        look="outlined"
        disabled={busy || !/^[1-9]\d*$/.test(task) || !Number.isSafeInteger(Number(task))}
        onClick={load}
      >
        {t("classificationLoad")}
      </Button>
      {busy && <p role="status">{t("classificationLoading")}</p>}
      {error && <p role="alert">{t("classificationReadFailed")}</p>}
      {!report && rows && (
        <fieldset disabled={busy}>
          <legend>{t("classificationChoose")}</legend>
          {!rows.length && <p>{t("classificationNoSources")}</p>}
          {rows.map((row) => (
            <label key={row.id} style={{ display: "block" }}>
              <input
                type="checkbox"
                checked={selected.includes(row.id)}
                onChange={(e) => {
                  frozen.current = null;
                  clear();
                  setSelected((old) =>
                    e.target.checked
                      ? [
                          ...old.filter((id) => rows.find((r) => r.id === id).submitted_by_id !== row.submitted_by_id),
                          row.id,
                        ]
                      : old.filter((id) => id !== row.id),
                  );
                }}
              />
              {t("classificationSource", { id: row.id, revision: row.revision, actor: row.submitted_by_id })}
            </label>
          ))}
          <Button
            data-testid="field-compare"
            disabled={selected.length < 2 || selected.length > 20}
            onClick={() => compare()}
          >
            {t("classificationCompare")}
          </Button>
        </fieldset>
      )}
      {frozen.current && (
        <Button data-testid="field-read" look="outlined" disabled={busy} onClick={() => compare(null, true)}>
          {t("classificationRefreshFrozen")}
        </Button>
      )}
      {report && (
        <>
          <FieldResult
            report={report}
            busy={busy}
            detail={detail}
            onSelectPair={(pair) => compare(pair)}
            onBack={() => setDetail(false)}
            matrixRefs={matrixRefs}
            detailHeading={detailHeading}
          />
          <Button
            data-testid="field-change-revisions"
            look="outlined"
            onClick={() => {
              clear();
              frozen.current = null;
              heading.current?.focus();
            }}
          >
            {t("nerChangeRevisions")}
          </Button>
        </>
      )}
    </section>
  );
};
