import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { cn } from "../../utils/bem";
import { partialPercent } from "./ClassificationPartial";
import "./ClassificationOverview.scss";

const id = z.number().int().positive().safe();
const count = z.number().int().nonnegative().safe();
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const ratio = z
  .object({ numerator: z.string().regex(/^(0|[1-9]\d{0,999})$/), denominator: z.string().regex(/^[1-9]\d{0,999}$/) })
  .refine((v) => BigInt(v.numerator) <= BigInt(v.denominator));
const reasons = z.array(z.object({ reason: z.string(), count: count.positive() }));
const row = z
  .object({
    actor_id: id,
    tasks: count.positive().max(100),
    comparable_tasks: count,
    unavailable_tasks: count,
    peer_pairs: count,
    comparable_peer_pairs: count,
    unavailable_peer_pairs: count,
    unavailable_reasons: reasons,
    unavailable_peer_pair_reasons: reasons,
    task_coverage: ratio,
    pair_coverage: ratio.nullable(),
    raw_agreement: ratio.nullable(),
    normalized_agreement: ratio.nullable(),
    task_ids: z.array(id).max(100),
  })
  .strict();
const schema = z
  .object({
    schema: z.literal("field-quality-overview/v1"),
    rules: z.object({
      aggregation: z.literal("field-batch-quality-task-macro/v1"),
      normalization: z.literal("field-lf-ascii-trim/v1"),
      explanation: z.literal("field-value-differences/v1"),
      engine: z.literal("python-string-equality/v1"),
      selection: z.literal("active-assignment-latest/v1"),
      queue: z.literal("field-raw-difference-queue/v1"),
    }),
    scope: z.object({
      kind: z.literal("batch"),
      organization_id: id,
      project_id: id,
      batch_id: id,
      visibility: z.enum(["project", "visible_subset"]),
      reference_kind: z.enum(["manifest_hash", "projection_hash"]),
      reference_hash: hash,
    }),
    source_set_hash: hash,
    viewer: hash,
    scope_counts: z.object({
      tasks: count.positive().max(100),
      comparable_tasks: count,
      unavailable_tasks: count,
      unavailable_reasons: reasons,
      total_pairs: count.max(19000),
      comparable_pairs: count.max(3800),
      unavailable_pairs: count,
      unavailable_pair_reasons: reasons,
    }),
    source_status: z.enum(["verified", "unavailable"]),
    current_source_issues: z.object({ task_count: count, reasons }),
    availability: z.enum(["available", "unavailable"]),
    reason: z.string().nullable(),
    summary: z
      .object({
        task_coverage: ratio,
        pair_coverage: ratio.nullable(),
        raw_agreement: ratio.nullable(),
        normalized_agreement: ratio.nullable(),
        agreement_tasks: count,
        disagreement_tasks: count,
      })
      .nullable(),
    annotators: z.array(row).max(100).nullable(),
    pagination: z
      .object({
        count: count.max(2000),
        page: id,
        page_size: id.max(100),
        next_page: id.nullable(),
        previous_page: id.nullable(),
      })
      .nullable(),
    frozen: z.string().min(1).max(16384).nullable(),
    result_hash: hash.nullable(),
    receipt_verified: z.boolean(),
    processing_cost: z
      .object({
        validation_passes: z.literal(2),
        scorer_calls: z.literal(0),
        per_pass: z
          .object({
            normalized_values: count.max(400),
            field_equalities: count.max(7600),
            aggregation_pair_visits: count.max(3800),
            annotator_pair_updates: count.max(7600),
          })
          .strict(),
        total: z
          .object({
            normalized_values: count.max(800),
            field_equalities: count.max(15200),
            aggregation_pair_visits: count.max(7600),
            annotator_pair_updates: count.max(15200),
          })
          .strict(),
      })
      .strict(),
    batch_calculated_at: z.string(),
    summarized_at: z.string().nullable(),
    observed_at: z.string(),
  })
  .strict();
const ordered = (ids) => ids.every((value, index) => !index || ids[index - 1] < value);
const fractionIs = (value, n, d) =>
  d === 0
    ? value === null
    : value !== null && BigInt(value.numerator) * BigInt(d) === BigInt(n) * BigInt(value.denominator);
const sum = (values) => values.reduce((n, value) => n + value.count, 0);
const orderedRatios = (raw, normalized) =>
  raw !== null &&
  normalized !== null &&
  BigInt(raw.numerator) * BigInt(normalized.denominator) <= BigInt(normalized.numerator) * BigInt(raw.denominator);
const valid = (condition) => {
  if (!condition) throw new Error("Invalid frozen field overview");
};

export const FieldQualityOverview = ({ projectId, batch, navigation, onOpenTask }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const api = useAPI();
  const call = useRef(api.callApi);
  call.current = api.callApi;
  const generation = useRef(0);
  const root = useRef(null);
  const readRef = useRef(null);
  const [data, setData] = useState(null);
  const [phase, setPhase] = useState("idle");
  const reference = batch.manifest_hash ?? batch.projection_hash;
  const context = `field-quality-overview/v1:${projectId}:${batch.id}:${batch.viewer}:${batch.visibility}:${reference}`;
  const read = async (page = 1, fresh = false) => {
    const current = ++generation.current;
    if (fresh) navigation.current = { context };
    const saved = navigation.current;
    setData(null);
    setPhase("busy");
    try {
      const raw = await call.current("fieldQualityOverview", {
        params: { pk: projectId, batch: batch.id, page, ...(saved.frozen ? { frozen: saved.frozen } : {}) },
        errorFilter: () => true,
      });
      if (current !== generation.current) return;
      if (!raw || raw.error || raw.$meta?.ok === false || typeof raw.status === "number") {
        const status = raw?.$meta?.status ?? raw?.status;
        setPhase(
          { 401: "denied", 403: "denied", 404: "denied", 409: "changed", 422: "budget", 503: "unavailable" }[status] ??
            "failed",
        );
        return;
      }
      const result = schema.parse(raw);
      const s = result.scope;
      const c = result.scope_counts;
      valid(
        s.project_id === projectId &&
          s.batch_id === batch.id &&
          s.reference_hash === reference &&
          s.visibility === batch.visibility &&
          result.viewer === batch.viewer &&
          s.reference_kind === (batch.visibility === "project" ? "manifest_hash" : "projection_hash") &&
          result.batch_calculated_at === batch.calculated_at &&
          c.tasks === batch.visible_task_count &&
          c.comparable_tasks + c.unavailable_tasks === c.tasks &&
          sum(c.unavailable_reasons) === c.unavailable_tasks &&
          c.comparable_pairs + c.unavailable_pairs === c.total_pairs &&
          sum(c.unavailable_pair_reasons) === c.unavailable_pairs &&
          (!saved.frozen ||
            (result.frozen === saved.frozen &&
              result.summarized_at === saved.summarized_at &&
              result.source_set_hash === saved.source_set_hash &&
              result.viewer === saved.viewer)),
      );
      const work = result.processing_cost;
      const plan = batch.plan_cost_per_pass;
      valid(
        plan &&
          Object.keys(work.per_pass).every((k) => work.total[k] === 2 * work.per_pass[k]) &&
          work.per_pass.normalized_values <= plan.normalized_values &&
          work.per_pass.field_equalities <= plan.field_equalities,
      );
      if (result.source_status !== "verified") {
        valid(
          result.summary === null &&
            result.annotators === null &&
            result.pagination === null &&
            result.result_hash === null &&
            !result.receipt_verified &&
            result.availability === "unavailable" &&
            result.reason === "source_unavailable" &&
            result.current_source_issues.task_count > 0 &&
            result.current_source_issues.task_count <= c.tasks &&
            sum(result.current_source_issues.reasons) === result.current_source_issues.task_count &&
            work.per_pass.aggregation_pair_visits === 0 &&
            work.per_pass.annotator_pair_updates === 0 &&
            (saved.frozen ? result.frozen === saved.frozen : result.frozen === null && result.summarized_at === null),
        );
      } else {
        const summary = result.summary;
        const p = result.pagination;
        valid(
          result.receipt_verified &&
            result.frozen &&
            result.result_hash &&
            result.summarized_at &&
            summary &&
            p &&
            result.annotators,
        );
        valid(
          result.current_source_issues.task_count === 0 &&
            !result.current_source_issues.reasons.length &&
            (!saved.result_hash || result.result_hash === saved.result_hash) &&
            p.page === page &&
            fractionIs(summary.task_coverage, c.comparable_tasks, c.tasks) &&
            fractionIs(summary.pair_coverage, c.comparable_pairs, c.total_pairs) &&
            summary.agreement_tasks + summary.disagreement_tasks === c.comparable_tasks &&
            work.per_pass.normalized_values === plan.normalized_values &&
            work.per_pass.field_equalities === plan.field_equalities &&
            work.per_pass.aggregation_pair_visits >= c.comparable_pairs &&
            work.per_pass.aggregation_pair_visits <= c.total_pairs &&
            work.per_pass.field_equalities >= 2 * work.per_pass.aggregation_pair_visits &&
            work.per_pass.annotator_pair_updates === 2 * c.comparable_pairs &&
            (c.comparable_tasks === 0
              ? summary.raw_agreement === null &&
                summary.normalized_agreement === null &&
                result.availability === "unavailable" &&
                result.reason === "no_comparable_tasks"
              : orderedRatios(summary.raw_agreement, summary.normalized_agreement) &&
                result.availability === "available" &&
                result.reason === null) &&
            p.previous_page === (page > 1 ? page - 1 : null) &&
            p.next_page === (page * p.page_size < p.count ? page + 1 : null) &&
            result.annotators.length === Math.min(p.page_size, Math.max(0, p.count - (page - 1) * p.page_size)) &&
            ordered(result.annotators.map((r) => r.actor_id)),
        );
        for (const r of result.annotators) {
          valid(
            r.tasks <= c.tasks &&
              r.comparable_tasks <= c.comparable_tasks &&
              r.comparable_tasks + r.unavailable_tasks === r.tasks &&
              r.peer_pairs <= c.total_pairs &&
              r.comparable_peer_pairs <= c.comparable_pairs &&
              r.comparable_peer_pairs + r.unavailable_peer_pairs === r.peer_pairs &&
              r.task_ids.length === r.tasks &&
              ordered(r.task_ids) &&
              sum(r.unavailable_reasons) === r.unavailable_tasks &&
              sum(r.unavailable_peer_pair_reasons) === r.unavailable_peer_pairs &&
              fractionIs(r.task_coverage, r.comparable_tasks, r.tasks) &&
              fractionIs(r.pair_coverage, r.comparable_peer_pairs, r.peer_pairs) &&
              (r.comparable_tasks === 0
                ? r.raw_agreement === null && r.normalized_agreement === null
                : orderedRatios(r.raw_agreement, r.normalized_agreement)),
          );
        }
        navigation.current = {
          ...saved,
          context,
          page,
          frozen: result.frozen,
          viewer: result.viewer,
          source_set_hash: result.source_set_hash,
          summarized_at: result.summarized_at,
          result_hash: result.result_hash,
        };
      }
      setData(result);
      setPhase("ready");
    } catch {
      if (current === generation.current) {
        setData(null);
        setPhase("failed");
      }
    }
  };
  readRef.current = read;
  useEffect(() => {
    generation.current += 1;
    setData(null);
    setPhase("idle");
    if (navigation.current.context !== context) navigation.current = { context };
    if (navigation.current.frozen) readRef.current(navigation.current.page ?? 1);
    const blur = () => {
      generation.current += 1;
      setData(null);
      setPhase("failed");
    };
    window.addEventListener("blur", blur);
    return () => {
      generation.current += 1;
      window.removeEventListener("blur", blur);
    };
  }, [context]);
  useEffect(() => {
    if (data && navigation.current.focusTask) {
      root.current?.querySelector(`[data-quality-task="${navigation.current.focusTask}"]`)?.focus();
      delete navigation.current.focusTask;
    }
  }, [data]);
  const shown =
    data &&
    data.scope.project_id === projectId &&
    data.scope.batch_id === batch.id &&
    data.scope.reference_hash === reference &&
    data.viewer === batch.viewer
      ? data
      : null;
  const percent = (value) => (value ? partialPercent(value) : t("classificationUnavailable"));
  const explain = (values) =>
    values
      .map(({ reason, count }) =>
        t("fieldQualityReasonCount", {
          reason: t(`fieldReason_${reason}`, { defaultValue: t("fieldReason_unknown") }),
          count,
        }),
      )
      .join("; ");
  const style = cn("classification-quality-overview");
  return (
    <section
      ref={root}
      className={style.toClassName()}
      data-testid="field-quality-overview"
      aria-busy={phase === "busy"}
    >
      <div className={style.elem("actions").toClassName()}>
        <Button
          disabled={phase === "busy"}
          onClick={() => read(navigation.current.page ?? 1)}
          data-testid="field-quality-read"
        >
          {t(navigation.current.frozen ? "qualityRefresh" : "qualityLoad")}
        </Button>
        {navigation.current.frozen && (
          <Button disabled={phase === "busy"} onClick={() => read(1, true)}>
            {t("qualityRegenerate")}
          </Button>
        )}
      </div>
      {phase === "busy" && <p role="status">{t("classificationLoading")}</p>}
      {["failed", "budget", "denied", "changed", "unavailable"].includes(phase) && (
        <p role="alert">
          {t(
            {
              budget: "fieldQualityBudget",
              denied: "fieldQualityDenied",
              changed: "fieldQualityChanged",
              unavailable: "fieldQualityUnavailable",
            }[phase] ?? "qualityFailed",
          )}
        </p>
      )}
      {shown && (
        <>
          <h4>{t("fieldQualityTitle")}</h4>
          <p>{t("fieldQualityBoundary")}</p>
          {shown.scope.visibility === "visible_subset" && <p>{t("batchVisibleSubset")}</p>}
          <p>{t("qualityCounts", shown.scope_counts)}</p>
          <p>{t("fieldQualityPairs", shown.scope_counts)}</p>
          {shown.scope_counts.unavailable_reasons.length > 0 && (
            <p data-testid="field-quality-task-reasons">
              {t("fieldQualityTaskReasons", { reasons: explain(shown.scope_counts.unavailable_reasons) })}
            </p>
          )}
          {shown.scope_counts.unavailable_pair_reasons.length > 0 && (
            <p data-testid="field-quality-pair-reasons">
              {t("fieldQualityPairReasons", { reasons: explain(shown.scope_counts.unavailable_pair_reasons) })}
            </p>
          )}
          <p>{t("qualityRule", { rule: shown.rules.aggregation, time: shown.summarized_at ?? "—" })}</p>
          <p>{t("qualityTimes", { batch: shown.batch_calculated_at, observed: shown.observed_at })}</p>
          {shown.source_status !== "verified" ? (
            <p role="status">{t("qualitySourceUnavailable")}</p>
          ) : (
            <>
              <p data-testid="field-quality-summary">
                {t("fieldQualitySummary", {
                  task: percent(shown.summary.task_coverage),
                  pair: percent(shown.summary.pair_coverage),
                  raw: percent(shown.summary.raw_agreement),
                  normalized: percent(shown.summary.normalized_agreement),
                })}
              </p>
              {shown.reason === "no_comparable_tasks" && <p role="status">{t("qualityNoComparable")}</p>}
              <table>
                <thead>
                  <tr>
                    {[
                      "qualityActor",
                      "qualityTaskCounts",
                      "fieldQualityPeerCounts",
                      "fieldQualityCoverage",
                      "fieldQualityRaw",
                      "fieldQualityNormalized",
                      "qualityTasks",
                    ].map((key) => (
                      <th key={key}>{t(key)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {shown.annotators.map((r) => (
                    <tr key={r.actor_id} data-quality-actor={r.actor_id}>
                      <td>{t("qualityActorId", { id: r.actor_id })}</td>
                      <td>
                        {r.tasks} / {r.comparable_tasks} / {r.unavailable_tasks}
                        <p>{explain(r.unavailable_reasons)}</p>
                      </td>
                      <td>
                        {r.peer_pairs} / {r.comparable_peer_pairs} / {r.unavailable_peer_pairs}
                        <p>{explain(r.unavailable_peer_pair_reasons)}</p>
                      </td>
                      <td>
                        {percent(r.task_coverage)} / {percent(r.pair_coverage)}
                      </td>
                      <td>{percent(r.raw_agreement)}</td>
                      <td>{percent(r.normalized_agreement)}</td>
                      <td>
                        <div className={style.elem("tasks").toClassName()}>
                          {r.task_ids.map((task) => (
                            <Button
                              key={task}
                              data-quality-task={task}
                              onClick={() => {
                                navigation.current.focusTask = task;
                                onOpenTask(task);
                              }}
                            >
                              {t("qualityTaskId", { id: task })}
                            </Button>
                          ))}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className={style.elem("actions").toClassName()}>
                <Button
                  data-testid="field-quality-previous"
                  disabled={!shown.pagination.previous_page}
                  onClick={() => read(shown.pagination.previous_page)}
                >
                  {t("batchPrevious")}
                </Button>
                <Button
                  data-testid="field-quality-next"
                  disabled={!shown.pagination.next_page}
                  onClick={() => read(shown.pagination.next_page)}
                >
                  {t("batchNext")}
                </Button>
              </div>
            </>
          )}
        </>
      )}
    </section>
  );
};
