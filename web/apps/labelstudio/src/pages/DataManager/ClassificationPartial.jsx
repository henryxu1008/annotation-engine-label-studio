import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";

const id = z.number().int().positive().safe();
const count = z.number().int().nonnegative().safe();
const ratio = z.object({ numerator: z.string().regex(/^(0|[1-9]\d{0,99})$/),
  denominator: z.string().regex(/^[1-9]\d{0,99}$/) }).refine((v) => BigInt(v.numerator) <= BigInt(v.denominator));
const pin = z.object({ submission_id: id, revision: id, submitter_id: id.nullable(), result_hash: z.string(), config_hash: z.string() });
const base = z.object({ manifest_hash: z.string(), manifest: z.object({ rule: z.literal("classification-exact-sets/v1"),
  calculated_at: z.string(), inputs: z.array(pin).max(20) }),
  status: z.enum(["comparable", "unavailable"]), reason: z.string().nullable(),
  matching_pairs: count.nullable(), total_pairs: count.nullable(), agreement: z.number().min(0).max(1).nullable(),
  locations: z.array(z.object({ from_name: z.string(), to_name: z.string(), matching_pairs: count, total_pairs: count,
    agreement: z.number().min(0).max(1), disagrees: z.boolean(),
    values: z.array(z.object({ submission_id: id, choices: z.array(z.string()).min(1).max(100) })).max(20) })).max(20),
});
const response = z.object({ schema: z.literal("classification-partial-match/v1"),
  rule: z.literal("classification-jaccard-nonempty/v1"), viewer: z.string().regex(/^[a-f0-9]{64}$/),
  origin: z.object({ kind: z.enum(["manual", "batch"]), project_id: id, task_id: id, batch_id: id.optional(),
    origin_hash: z.string(), rule: z.literal("classification-exact-sets/v1"), calculated_at: z.string() }),
  frozen: z.string().min(1).max(16384), receipt: z.object({ schema: z.literal("classification-partial-match/v1"),
    rule: z.literal("classification-jaccard-nonempty/v1"), explained_at: z.string(), result_hash: z.string(),
    origin: z.object({ project_id: id, task_id: id, kind: z.enum(["manual", "batch"]), batch_id: id.optional(), origin_hash: z.string() }) }),
  observed_at: z.string(), base, partial: z.object({ status: z.enum(["comparable", "unavailable"]), reason: z.string().nullable(),
    score: ratio.nullable(), controls: z.array(z.object({ from_name: z.string(), to_name: z.string(), score: ratio,
      pairs: z.array(z.object({ left_submission_id: id, right_submission_id: id, intersection_count: count,
        union_count: count.positive(), shared: z.array(z.string()), only_left: z.array(z.string()), only_right: z.array(z.string()) })).max(190),
    })).max(20) }),
});
// Round only the displayed percentage, using exact integer arithmetic.
export const partialPercent = (v) => {
  const n = BigInt(v.numerator), d = BigInt(v.denominator), rounded = (n * 20000n + d) / (2n * d);
  return `${rounded / 100n}.${String(rounded % 100n).padStart(2, "0")}%`;
};
const add = (a, b) => [a[0] * b[1] + b[0] * a[1], a[1] * b[1]];
const equalsRatio = (a, b) => a[0] * BigInt(b.denominator) === BigInt(b.numerator) * a[1];

export const ClassificationPartial = ({ projectId, taskId, batchId, sourceFrozen, enabled, report, onFailure, children }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI();
  const call = useRef(api.callApi), failure = useRef(onFailure); call.current = api.callApi; failure.current = onFailure;
  const generation = useRef(0), receipt = useRef(null);
  const [phase, setPhase] = useState("idle"), [joint, setJoint] = useState(null);
  useEffect(() => {
    generation.current += 1; receipt.current = null; setJoint(null); setPhase("idle");
    const blur = () => { generation.current += 1; setJoint(null); setPhase("failed"); };
    window.addEventListener("blur", blur);
    return () => { generation.current += 1; window.removeEventListener("blur", blur); };
  }, [projectId, taskId, batchId, sourceFrozen, report.manifest_hash, enabled]);

  const read = async () => {
    const current = ++generation.current;
    setPhase("busy"); setJoint(null);
    try {
      const params = { pk: projectId, task: taskId, ...(batchId ? { batch: batchId } : {}),
        ...(receipt.current ? { frozen: receipt.current } : batchId ? {} : { source_frozen: sourceFrozen }) };
      const raw = await call.current(batchId ? "classificationBatchPartial" : "classificationPartial", { params, errorFilter: () => true });
      if (current !== generation.current) return;
      if (!raw || raw.error || raw.$meta?.ok === false || typeof raw.status === "number") throw new Error("unavailable");
      const data = response.parse(raw), origin = data.origin, frozenOrigin = data.receipt.origin;
      if (origin.project_id !== projectId || origin.task_id !== taskId || origin.kind !== (batchId ? "batch" : "manual")
        || origin.batch_id !== batchId || frozenOrigin.project_id !== projectId || frozenOrigin.task_id !== taskId
        || frozenOrigin.kind !== origin.kind || frozenOrigin.batch_id !== batchId || frozenOrigin.origin_hash !== origin.origin_hash
        || origin.calculated_at !== report.manifest.calculated_at || data.base.manifest.rule !== origin.rule
        || data.base.manifest.calculated_at !== origin.calculated_at || data.base.manifest_hash !== origin.origin_hash
        || (!batchId && origin.origin_hash !== report.manifest_hash)
        || (receipt.current && data.frozen !== receipt.current)) throw new Error("scope changed");
      const result = data.partial;
      if (result.status !== data.base.status || (result.status === "unavailable"
        ? result.score !== null || result.controls.length !== 0 || data.base.agreement !== null || data.base.locations.length !== 0
          || data.base.matching_pairs !== null || data.base.total_pairs !== null || result.reason !== data.base.reason
        : result.score === null || !result.controls.length || data.base.agreement === null || data.base.total_pairs < 1)) throw new Error("invalid result");
      if (result.status === "comparable") {
        const pins = data.base.manifest.inputs.map((v) => v.submission_id).sort((a, b) => a - b);
        if (JSON.stringify(data.base.manifest.inputs) !== JSON.stringify(report.manifest.inputs.map((v) => pin.parse(v)))
          || result.controls.length !== data.base.locations.length) throw new Error("inputs changed");
        let mean = [0n, 1n];
        const names = new Set();
        for (const control of result.controls) {
          const location = data.base.locations.find((v) => v.from_name === control.from_name && v.to_name === control.to_name);
          if (!location || names.has(control.from_name) || control.pairs.length !== pins.length * (pins.length - 1) / 2) throw new Error("invalid pairs");
          names.add(control.from_name);
          const seen = new Set();
          let sum = [0n, 1n];
          for (const pair of control.pairs) {
            const key = `${pair.left_submission_id}:${pair.right_submission_id}`;
            if (pair.left_submission_id >= pair.right_submission_id || !pins.includes(pair.left_submission_id)
              || !pins.includes(pair.right_submission_id) || seen.has(key)
              || pair.intersection_count !== pair.shared.length
              || pair.union_count !== pair.shared.length + pair.only_left.length + pair.only_right.length) throw new Error("invalid pair");
            seen.add(key);
            const left = location.values.find((v) => v.submission_id === pair.left_submission_id)?.choices;
            const right = location.values.find((v) => v.submission_id === pair.right_submission_id)?.choices;
            if (!left || !right || JSON.stringify([...pair.shared, ...pair.only_left].sort()) !== JSON.stringify([...left].sort())
              || JSON.stringify([...pair.shared, ...pair.only_right].sort()) !== JSON.stringify([...right].sort())
              || pair.shared.some((v) => pair.only_left.includes(v) || pair.only_right.includes(v))
              || pair.only_left.some((v) => pair.only_right.includes(v))) throw new Error("invalid differences");
            sum = add(sum, [BigInt(pair.intersection_count), BigInt(pair.union_count)]);
          }
          sum[1] *= BigInt(control.pairs.length);
          if (!equalsRatio(sum, control.score)) throw new Error("invalid mean");
          mean = add(mean, [BigInt(control.score.numerator), BigInt(control.score.denominator)]);
        }
        mean[1] *= BigInt(result.controls.length);
        if (!equalsRatio(mean, result.score)) throw new Error("invalid mean");
      }
      receipt.current = data.frozen; setJoint(data); setPhase("ready");
    } catch {
      if (current === generation.current) { setJoint(null); setPhase("failed"); failure.current?.(); }
    }
  };
  const jointCurrent = enabled && joint && joint.origin.project_id === projectId && joint.origin.task_id === taskId
    && joint.origin.batch_id === batchId && joint.origin.calculated_at === report.manifest.calculated_at
    && (batchId || joint.origin.origin_hash === report.manifest_hash);
  const shown = phase === "idle" ? report : phase === "ready" && jointCurrent ? { ...joint.base, observed_at: joint.observed_at } : null;
  return <>
    {shown && children(shown)}
    {enabled && <Button disabled={phase === "busy"} onClick={read} data-testid="classification-partial-load">
      {t(receipt.current ? "partialRecheck" : "partialLoad")}</Button>}
    {phase === "busy" && <p role="status">{t("classificationLoading")}</p>}
    {phase === "failed" && <p role="alert">{t("partialReadFailed")}</p>}
    {jointCurrent && <section data-testid="classification-partial-result" style={{ overflowWrap: "anywhere" }}>
      <h4>{t("partialTitle")}</h4><p>{t("partialBoundary")}</p><p>{t("partialFormula")}</p>
      <p>{t("partialRule", { rule: joint.rule, time: joint.receipt.explained_at })}</p>
      {joint.partial.status === "unavailable" ? <p role="status">{t("classificationUnavailable")}</p> : <>
        <p data-testid="classification-partial-score">{t("partialScore", { score: partialPercent(joint.partial.score) })}</p>
        {joint.partial.controls.map((control) => <section key={control.from_name}>
          <h5>{control.from_name} → {control.to_name}: {partialPercent(control.score)}</h5>
          {control.pairs.map((pair) => <div key={`${pair.left_submission_id}:${pair.right_submission_id}`} data-testid="classification-partial-pair">
            <p>{t("partialPair", { left: pair.left_submission_id, right: pair.right_submission_id,
              intersection: pair.intersection_count, union: pair.union_count })}</p>
            <p>{t("partialShared")}: {pair.shared.join(", ") || "—"}</p>
            <p>{t("partialOnlyLeft")}: {pair.only_left.join(", ") || "—"}</p>
            <p>{t("partialOnlyRight")}: {pair.only_right.join(", ") || "—"}</p>
          </div>)}
        </section>)}
      </>}
    </section>}
  </>;
};
