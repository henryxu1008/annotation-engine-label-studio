import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { partialPercent } from "./ClassificationPartial";

const id = z.number().int().positive().safe(), count = z.number().int().nonnegative().safe();
const hash = z.string().regex(/^[a-f0-9]{64}$/);
const ratio = z.object({ numerator: z.string().regex(/^(0|[1-9]\d{0,999})$/),
  denominator: z.string().regex(/^[1-9]\d{0,999}$/) }).refine((v) => BigInt(v.numerator) <= BigInt(v.denominator));
const reasons = z.array(z.object({ reason: z.string(), count }));
const row = z.object({ actor_id: id, tasks: count.positive().max(100), comparable_tasks: count, unavailable_tasks: count,
  disagreement_tasks: count, peer_pairs: count, matching_peer_pairs: count, unavailable_reasons: reasons,
  coverage: ratio, exact_agreement: ratio.nullable(), partial_match: ratio.nullable(), task_ids: z.array(id).max(100) });
const schema = z.object({ schema: z.literal("classification-quality-overview/v1"), rules: z.object({
  aggregation: z.literal("classification-batch-quality-task-macro/v1"), exact: z.literal("classification-exact-sets/v1"),
  partial: z.literal("classification-jaccard-nonempty/v1"), selection: z.literal("active-assignment-latest/v1"),
}), scope: z.object({ kind: z.literal("batch"), project_id: id, batch_id: id, visibility: z.enum(["project", "visible_subset"]),
  reference_kind: z.enum(["manifest_hash", "projection_hash"]), reference_hash: hash }),
  source_set_hash: hash, viewer: hash, scope_counts: z.object({ tasks: count.positive().max(100), comparable_tasks: count,
    unavailable_tasks: count, unavailable_reasons: reasons }), source_status: z.enum(["verified", "unavailable", "integrity_failed"]),
  current_source_issues: z.object({ task_count: count, reasons }), availability: z.enum(["available", "unavailable"]),
  reason: z.string().nullable(), summary: z.object({ coverage: ratio, unanimous_tasks: count, disagreement_tasks: count,
    exact_agreement: ratio.nullable(), partial_match: ratio.nullable() }).nullable(), annotators: z.array(row).max(100).nullable(),
  pagination: z.object({ count: count.max(2000), page: id, page_size: id.max(100), next_page: id.nullable(), previous_page: id.nullable() }).nullable(),
  frozen: z.string().min(1).max(16384).nullable(), result_hash: hash.nullable(), receipt_verified: z.boolean(),
  batch_calculated_at: z.string(), summarized_at: z.string().nullable(), observed_at: z.string(),
});
const fractionIs = (v, n, d) => BigInt(v.numerator) * BigInt(d) === BigInt(n) * BigInt(v.denominator);
const ordered = (ids) => ids.every((id, i) => !i || ids[i - 1] < id);

export const ClassificationOverview = ({ projectId, batch, navigation, onOpenTask }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI();
  const call = useRef(api.callApi); call.current = api.callApi;
  const generation = useRef(0), root = useRef(null), readRef = useRef(null);
  const [data, setData] = useState(null), [phase, setPhase] = useState("idle");
  const reference = batch.manifest_hash ?? batch.projection_hash;
  const context = `${projectId}:${batch.id}:${batch.visibility}:${reference}`;
  const read = async (page = 1, fresh = false) => {
    const current = ++generation.current;
    if (fresh) navigation.current = { context };
    const saved = navigation.current;
    setData(null); setPhase("busy");
    try {
      const raw = await call.current("classificationQualityOverview", { params: { pk: projectId, batch: batch.id,
        page, ...(saved.frozen ? { frozen: saved.frozen } : {}) }, errorFilter: () => true });
      if (current !== generation.current) return;
      if (!raw || raw.error || raw.$meta?.ok === false || typeof raw.status === "number") throw new Error("unavailable");
      const result = schema.parse(raw), s = result.scope, c = result.scope_counts;
      if (s.project_id !== projectId || s.batch_id !== batch.id || s.reference_hash !== reference
        || s.visibility !== batch.visibility || s.reference_kind !== (batch.visibility === "project" ? "manifest_hash" : "projection_hash")
        || result.batch_calculated_at !== batch.calculated_at || c.tasks !== batch.visible_task_count
        || c.comparable_tasks + c.unavailable_tasks !== c.tasks
        || c.unavailable_reasons.reduce((sum, v) => sum + v.count, 0) !== c.unavailable_tasks
        || (saved.frozen && (result.frozen !== saved.frozen || result.summarized_at !== saved.summarized_at || result.viewer !== saved.viewer))) {
        throw new Error("scope changed");
      }
      if (result.source_status !== "verified") {
        if (result.summary !== null || result.annotators !== null || result.pagination !== null
          || result.result_hash !== null || result.receipt_verified || result.availability !== "unavailable") throw new Error("unmasked source");
      } else {
        const summary = result.summary, p = result.pagination;
        if (!result.receipt_verified || !result.frozen || !result.result_hash || !result.summarized_at || !summary || !p || !result.annotators
          || (saved.result_hash && saved.result_hash !== result.result_hash) || p.page !== page
          || !fractionIs(summary.coverage, c.comparable_tasks, c.tasks)
          || summary.unanimous_tasks + summary.disagreement_tasks !== c.comparable_tasks
          || (c.comparable_tasks === 0 ? summary.exact_agreement !== null || summary.partial_match !== null || result.availability !== "unavailable"
            : !summary.exact_agreement || !summary.partial_match || result.availability !== "available")
          || p.previous_page !== (page > 1 ? page - 1 : null)
          || p.next_page !== (page * p.page_size < p.count ? page + 1 : null)
          || result.annotators.length !== Math.min(p.page_size, Math.max(0, p.count - (page - 1) * p.page_size))
          || !ordered(result.annotators.map((v) => v.actor_id))) throw new Error("invalid projection");
        for (const r of result.annotators) {
          if (r.tasks > c.tasks || r.comparable_tasks > c.comparable_tasks || r.comparable_tasks + r.unavailable_tasks !== r.tasks
            || r.task_ids.length !== r.tasks || !ordered(r.task_ids) || r.disagreement_tasks > r.comparable_tasks
            || r.matching_peer_pairs > r.peer_pairs || !fractionIs(r.coverage, r.comparable_tasks, r.tasks)
            || r.unavailable_reasons.reduce((sum, v) => sum + v.count, 0) !== r.unavailable_tasks
            || (r.comparable_tasks === 0 ? r.exact_agreement !== null || r.partial_match !== null : !r.exact_agreement || !r.partial_match)) {
            throw new Error("invalid denominators");
          }
        }
        navigation.current = { ...saved, context, page, frozen: result.frozen, viewer: result.viewer,
          summarized_at: result.summarized_at, result_hash: result.result_hash };
      }
      setData(result); setPhase("ready");
    } catch {
      if (current === generation.current) { setData(null); setPhase("failed"); }
    }
  };
  readRef.current = read;
  useEffect(() => {
    generation.current += 1; setData(null); setPhase("idle");
    if (navigation.current.context !== context) navigation.current = { context };
    if (navigation.current.frozen) readRef.current(navigation.current.page ?? 1);
    const blur = () => { generation.current += 1; setData(null); setPhase("failed"); };
    window.addEventListener("blur", blur);
    return () => { generation.current += 1; window.removeEventListener("blur", blur); };
  }, [context]);
  useEffect(() => {
    if (data && navigation.current.focusTask) {
      root.current?.querySelector(`[data-quality-task="${navigation.current.focusTask}"]`)?.focus();
      delete navigation.current.focusTask;
    }
  }, [data]);
  const shown = data && data.scope.project_id === projectId && data.scope.batch_id === batch.id && data.scope.reference_hash === reference ? data : null;
  const percent = (value) => value ? partialPercent(value) : t("classificationUnavailable");
  return <section ref={root} data-testid="classification-quality-overview" style={{ overflowWrap: "anywhere", overflowX: "auto" }}>
    <Button disabled={phase === "busy"} onClick={() => read(navigation.current.page ?? 1)} data-testid="quality-read">
      {t(navigation.current.frozen ? "qualityRefresh" : "qualityLoad")}</Button>
    {navigation.current.frozen && <Button disabled={phase === "busy"} onClick={() => read(1, true)}>{t("qualityRegenerate")}</Button>}
    {phase === "busy" && <p role="status">{t("classificationLoading")}</p>}
    {phase === "failed" && <p role="alert">{t("qualityFailed")}</p>}
    {shown && <>
      <h4>{t("qualityTitle")}</h4><p>{t("qualityBoundary")}</p>
      {shown.scope.visibility === "visible_subset" && <p>{t("batchVisibleSubset")}</p>}
      <p>{t("qualityCounts", shown.scope_counts)}</p><p>{t("qualityRule", { rule: shown.rules.aggregation, time: shown.summarized_at ?? "—" })}</p>
      <p>{t("qualityTimes", { batch: shown.batch_calculated_at, observed: shown.observed_at })}</p>
      {shown.source_status !== "verified" ? <p role="status">{t("qualitySourceUnavailable")}</p> : <>
        <p data-testid="quality-summary">{t("qualitySummary", { coverage: percent(shown.summary.coverage),
          exact: percent(shown.summary.exact_agreement), partial: percent(shown.summary.partial_match) })}</p>
        {shown.reason === "no_comparable_tasks" && <p role="status">{t("qualityNoComparable")}</p>}
        <table><thead><tr>{["qualityActor", "qualityTaskCounts", "qualityCoverage", "qualityExact", "qualityPartial", "qualityTasks"].map((key) => <th key={key}>{t(key)}</th>)}</tr></thead>
          <tbody>{shown.annotators.map((r) => <tr key={r.actor_id} data-quality-actor={r.actor_id}>
            <td>{t("qualityActorId", { id: r.actor_id })}</td><td>{r.tasks} / {r.comparable_tasks} / {r.unavailable_tasks}</td>
            <td>{percent(r.coverage)}</td><td>{percent(r.exact_agreement)}</td><td>{percent(r.partial_match)}</td>
            <td>{r.task_ids.map((task) => <Button key={task} data-quality-task={task} onClick={() => {
              navigation.current.focusTask = task; onOpenTask(task);
            }}>{t("qualityTaskId", { id: task })}</Button>)}</td>
          </tr>)}</tbody></table>
        <Button disabled={!shown.pagination.previous_page} onClick={() => read(shown.pagination.previous_page)}>{t("batchPrevious")}</Button>
        <Button disabled={!shown.pagination.next_page} onClick={() => read(shown.pagination.next_page)}>{t("batchNext")}</Button>
      </>}
    </>}
  </section>;
};
