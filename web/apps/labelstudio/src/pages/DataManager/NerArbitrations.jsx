import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { FrozenArbitrationWorkspace } from "./ClassificationArbitrations";
import { NerResult, nerComparisonSchema, validateNerComparison } from "./NerDisagreement";

const SCHEMA = "ner-arbitrations/v1";
const RULE = "adopt-exact-ner-submission/v1";
const id = z.number().int().positive().safe();
const count = z.number().int().nonnegative().safe();
const hash = z.string().regex(/^[0-9a-f]{64}$/);
const state = z.enum(["pending", "resolved", "cancelled"]);
const pin = z.object({
  submission_id: id,
  assignment_id: id,
  assignee_id: id,
  task_id: id,
  revision: id,
  submitter_id: id,
  result_hash: hash,
  config_hash: hash,
  task_data_hash: hash,
});
const caps = z.object({
  enabled: z.literal(true),
  schema: z.literal(SCHEMA),
  decision_rule: z.literal(RULE),
  actor_id: id,
  viewer_hash: hash,
  can_read: z.boolean(),
  can_create: z.boolean(),
  can_decide: z.boolean(),
  can_cancel: z.boolean(),
});
const meta = z.object({
  id,
  schema: z.literal(SCHEMA),
  decision_rule: z.literal(RULE),
  ner_batch_id: id,
  task_id: id,
  batch_name: z.string(),
  assignee_id: id,
  assignee_id_frozen: id,
  input_set_hash: hash,
  task_evidence_hash: hash,
  viewer_hash: hash,
  status: state,
  version: id,
  created_at: z.string(),
  calculated_at: z.string(),
  can_decide: z.boolean(),
  can_cancel: z.boolean(),
  origin_manifest_hash: hash.optional(),
});
const pagination = { count, next: z.string().nullable(), previous: z.string().nullable() };
const roster = z.object({
  ...pagination,
  ner_batch_id: id,
  task_id: id,
  batch_name: z.string(),
  origin_manifest_hash: hash,
  expected_manifest_hash: hash,
  input_set_hash: hash,
  viewer_hash: hash,
  results: z.array(z.object({ id, name: z.string() })).max(100),
});
const receipt = z.object({
  schema: z.literal(SCHEMA),
  operation: z.enum(["create", "decision", "cancel"]),
  project_id: id,
  arbitration_id: id,
  principal: z.string().min(1),
  source: z.string().min(1),
  actor_id: id,
  key: z.string().uuid(),
  created_at: z.string(),
  request_hash: hash,
  payload_hash: hash,
  input_set_hash: hash,
  status_at_commit: state,
  version_at_commit: id,
});
const created = meta.extend({
  action: z.literal("create"),
  replayed: z.boolean(),
  receipt,
  current_status: state,
  current_version: id,
  origin_manifest_hash: hash,
});
const command = z.object({
  id,
  schema: z.literal(SCHEMA),
  action: z.enum(["decision", "cancel"]),
  input_set_hash: hash,
  viewer_hash: hash,
  status: state,
  version: id,
  current_status: state,
  current_version: id,
  replayed: z.boolean(),
  receipt,
  decision_id: id.optional(),
  winner_submission_id: id.optional(),
  winner_revision: id.optional(),
});
const detail = meta.extend({
  observed_at: z.string(),
  source_status: z.enum(["verified", "unavailable"]),
  reason: z.string().nullable(),
  comparison: nerComparisonSchema.nullable(),
  inputs: z.array(pin).max(20),
  frozen_counts: z.object({
    outcome: z.literal("disagreement"),
    reason: z.null(),
    input_count: count,
    total_pairs: count,
    comparable_pairs: count,
    calculation_hash: hash,
  }),
  decision: z
    .object({
      id,
      actor_id: id,
      created_at: z.string(),
      winner_submission_id: id,
      winner_revision: id,
      reason: z.string(),
      rule: z.literal(RULE),
    })
    .nullable(),
  cancellation: z.object({ actor_id: id, created_at: z.string(), reason: z.string() }).nullable(),
});
const methods = {
  classificationArbitrationCapability: "nerArbitrationCapability",
  classificationArbitrationAssignees: "nerArbitrationAssignees",
  classificationArbitrations: "nerArbitrations",
  classificationArbitrationDetail: "nerArbitrationDetail",
  createClassificationArbitration: "createNerArbitration",
  resolveClassificationArbitration: "resolveNerArbitration",
  cancelClassificationArbitration: "cancelNerArbitration",
};
const schemas = {
  classificationArbitrationCapability: caps,
  classificationArbitrationAssignees: roster,
  classificationArbitrations: z.object({ ...pagination, viewer_hash: hash, results: z.array(meta).max(100) }),
  classificationArbitrationDetail: detail,
  createClassificationArbitration: created,
  resolveClassificationArbitration: command,
  cancelClassificationArbitration: command,
};
const valid = (value) => {
  if (!value) throw new Error("Invalid NER arbitration scope");
};
const normalize = (v) => (v.ner_batch_id ? { ...v, batch_id: v.ner_batch_id } : v);
const nerBody = (value) => {
  if (!value || !("batch_id" in value)) return value;
  const { batch_id, ...rest } = value;
  return { ...rest, ner_batch_id: batch_id };
};

const Choices = ({ data, winner, onChoose }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  const p = data.comparison.selected_pair;
  return data.inputs.map((source) => {
    const side = p?.left === source.submission_id ? "left" : p?.right === source.submission_id ? "right" : null;
    return (
      <label key={source.submission_id} style={{ display: "block" }}>
        <input
          type="radio"
          name="arbitration-winner"
          value={source.submission_id}
          checked={winner === String(source.submission_id)}
          onChange={() => onChoose(source.submission_id)}
        />
        {t("classificationSource", { id: source.submission_id, revision: source.revision, actor: source.submitter_id })}
        {side
          ? ` — ${t("nerArbitrationEntities", { count: p[`${side}_entities`].length })}`
          : ` — ${t("nerArbitrationInspect")}`}
      </label>
    );
  });
};

const Evidence = ({ data, busy, onSelectPair }) => {
  const [showDetail, setShowDetail] = useState(true);
  const matrixRefs = useRef({});
  const heading = useRef(null);
  useEffect(() => {
    if (showDetail) heading.current?.focus();
  }, [data.id, data.comparison.selected_pair, showDetail]);
  const report = {
    ...data.comparison,
    rule: "ner-strict-entities/v1",
    source_status: data.source_status,
    manifest_hash: data.task_evidence_hash,
    calculated_at: data.calculated_at,
    observed_at: data.observed_at,
    manifest: { inputs: data.inputs },
  };
  return (
    <NerResult
      report={report}
      busy={busy}
      detail={showDetail}
      onSelectPair={onSelectPair}
      onBack={() => setShowDetail(false)}
      matrixRefs={matrixRefs}
      detailHeading={heading}
    />
  );
};

export const nerArbitrationAdapter = {
  testId: "ner-arbitrations",
  messages: {
    arbitrationTitle: "nerArbitrationTitle",
    arbitrationBoundary: "nerArbitrationBoundary",
    arbitrationDispatch: "nerArbitrationDispatch",
    arbitrationAdopt: "nerArbitrationAdopt",
    arbitrationStartHint: "nerArbitrationStartHint",
  },
  rejected: Object.fromEntries(
    [
      ...["INVALID_INPUT", "INVALID_KEY"].map((k) => [k, 400]),
      ...[
        "ORIGIN_CHANGED",
        "INELIGIBLE",
        "ASSIGNEE_UNAVAILABLE",
        "DUPLICATE_ACTIVE",
        "SOURCE_UNAVAILABLE",
        "SOURCE_CHANGED",
        "CAS_CONFLICT",
        "INPUT_SET_CHANGED",
        "TERMINAL",
        "KEY_CONFLICT",
      ].map((k) => [k, 409]),
      ["BUDGET_EXCEEDED", 422],
    ].map(([k, v]) => [`NER_ARBITRATION_${k}`, v]),
  ),
  request: (method, params, body) => ({ method: methods[method], params: nerBody(params), body: nerBody(body) }),
  parse: (method, raw, expected) => {
    valid(schemas[method]);
    const v = schemas[method].parse(raw);
    const check = (row) =>
      valid(row.assignee_id === row.assignee_id_frozen && row.version === (row.status === "pending" ? 1 : 2));
    if (v.schema === SCHEMA && v.task_id) check(v);
    if (method === "classificationArbitrations") {
      v.results.forEach((row) => {
        check(row);
        valid(row.viewer_hash === v.viewer_hash);
      });
      return { ...v, results: v.results.map(normalize) };
    }
    if (method === "classificationArbitrationDetail") {
      valid(v.id === expected.arbitration);
      valid(
        (v.status === "resolved") === Boolean(v.decision) && (v.status === "cancelled") === Boolean(v.cancellation),
      );
      const ids = v.inputs.map((p) => p.submission_id);
      const n = v.frozen_counts.input_count;
      valid(n >= 2 && n <= 20 && v.frozen_counts.total_pairs === (n * (n - 1)) / 2);
      if (v.source_status === "unavailable")
        valid(!v.comparison && !v.inputs.length && !v.can_decide && v.reason === "source_unavailable");
      else {
        valid(
          v.comparison &&
            ids.length === n &&
            v.reason === null &&
            v.inputs.every((p) => p.task_id === v.task_id && p.assignee_id === p.submitter_id) &&
            ids.every((id, i) => !i || ids[i - 1] < id),
        );
        validateNerComparison(v.comparison, {
          ids,
          pair: expected.left ? [expected.left, expected.right].sort((a, b) => a - b) : ids.slice(0, 2),
        });
        valid(
          v.comparison.comparable_pairs === v.frozen_counts.comparable_pairs &&
            v.comparison.pairs.some((p) => p.f1 && p.f1.numerator !== p.f1.denominator),
        );
      }
      return {
        ...normalize(v),
        available: v.source_status === "verified",
        texts: [],
        report: v.comparison ? { inputs: v.inputs } : null,
      };
    }
    return normalize(v);
  },
  validateAcknowledgement: (result, intent) => {
    const r = result.receipt;
    valid(
      r.schema === SCHEMA &&
        result.schema === SCHEMA &&
        result.action === intent.action &&
        result.viewer_hash === intent.viewerHash &&
        r.arbitration_id === result.id &&
        r.project_id === intent.projectId &&
        r.actor_id === intent.actorId &&
        r.input_set_hash === intent.inputHash &&
        result.input_set_hash === intent.inputHash &&
        result.current_status === result.status &&
        result.current_version === result.version,
    );
    if (intent.action === "decision") valid(result.winner_revision === intent.winnerRevision);
  },
  canAdopt: (data, winner) =>
    data.comparison?.selected_pair &&
    [data.comparison.selected_pair.left, data.comparison.selected_pair.right].includes(winner),
  pairForWinner: (data, winner) =>
    [data.comparison.selected_pair?.left, data.comparison.selected_pair?.right].includes(winner)
      ? null
      : [data.inputs.find((p) => p.submission_id !== winner).submission_id, winner].sort((a, b) => a - b),
  renderChoices: (props) => <Choices {...props} />,
  renderEvidence: (props) => <Evidence {...props} />,
};

export const NerArbitrationTitle = () => {
  const { t } = useLocaleTranslation("reviewBatches");
  return t("nerArbitrationTitle");
};
export const NerArbitrationEntry = ({ projectId, onOpen, disabled, dispatch = false }) => {
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
        const raw = await call.current("nerArbitrationCapability", {
          params: { pk: projectId },
          errorFilter: () => true,
        });
        const result = caps.safeParse(raw);
        if (!disposed && current === generation)
          setEnabled(
            Boolean(
              onOpen &&
                !raw?.error &&
                raw?.$meta?.ok !== false &&
                typeof raw?.status !== "number" &&
                result.success &&
                result.data[dispatch ? "can_create" : "can_read"],
            ),
          );
      } catch {
        /* unavailable */
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
  }, [projectId, dispatch, Boolean(onOpen)]);
  return enabled ? (
    <Button
      disabled={disabled}
      onClick={onOpen}
      data-testid={dispatch ? "dispatch-ner-arbitration" : "open-ner-arbitrations"}
    >
      {t(dispatch ? "nerArbitrationDispatch" : "nerArbitrationTitle")}
    </Button>
  ) : null;
};
export const NerArbitrations = ({ initialOrigin, ...props }) => (
  <FrozenArbitrationWorkspace
    {...props}
    initialOrigin={
      initialOrigin
        ? {
            batch_id: initialOrigin.ner_batch_id,
            task_id: initialOrigin.task_id,
            expected_manifest_hash: initialOrigin.expected_manifest_hash,
          }
        : null
    }
    adapter={nerArbitrationAdapter}
  />
);
