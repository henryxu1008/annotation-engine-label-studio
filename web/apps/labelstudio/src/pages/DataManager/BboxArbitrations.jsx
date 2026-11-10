import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { Button } from "@humansignal/ui";
import { useLocaleTranslation } from "@humansignal/i18n";
import { useAPI } from "../../providers/ApiProvider";
import { FrozenArbitrationWorkspace } from "./ClassificationArbitrations";
import { BboxComparison, bboxBatchAdapter, bboxComparison, bboxSource } from "./BboxBatches";

const id = z.number().int().positive().safe(), count = z.number().int().nonnegative().safe();
const hash = z.string().regex(/^[a-f0-9]{64}$/), state = z.enum(["pending", "resolved", "cancelled"]);
const caps = z.object({ enabled: z.literal(true), schema: z.literal("bbox-arbitration/v1"), project_id: id,
  actor_id: id, viewer_hash: hash, can_read: z.boolean(), can_create: z.boolean(), can_decide: z.boolean(), can_cancel: z.boolean() });
const meta = z.object({ id, project_id: id, schema: z.literal("bbox-arbitration/v1"), batch_id: id, task_id: id,
  batch_name: z.string(), assignee_id: id, input_set_hash: hash, viewer_hash: hash, status: state, version: id,
  created_at: z.string(), calculated_at: z.string(), can_decide: z.boolean(), can_cancel: z.boolean(),
  origin_manifest_hash: hash.optional() });
const pagination = { count, next: z.string().nullable(), previous: z.string().nullable() };
const receipt = z.object({ operation: z.enum(["create", "decision", "cancel"]), key: z.string().uuid(), actor_id: id,
  input_set_hash: hash, request_hash: hash, status_at_commit: state, version_at_commit: id });
const originItem = z.object({ task_id: id, cohort: z.array(z.object({ assignment_id: id, assignee_id: id,
  source: bboxSource.nullable() })).max(20), result: bboxComparison });
const detail = meta.extend({ observed_at: z.string(), selected_pair: z.tuple([id, id]), origin: z.object({ project_id: id, batch_id: id, item: originItem }),
  decision: z.object({ id, actor_id: id, winner_submission_id: id, winner_revision: id, reason: z.string(), created_at: z.string() }).nullable(),
  cancellation: z.object({ actor_id: id, cancelled_at: z.string(), reason: z.string() }).nullable() });
const roster = z.object({ ...pagination, batch_id: id, task_id: id, batch_name: z.string(), origin_manifest_hash: hash,
  input_set_hash: hash, viewer_hash: hash, results: z.array(z.object({ id, name: z.string() })).max(100) });
const command = meta.extend({ replayed: z.boolean(), receipt, decision_id: id.optional(), winner_submission_id: id.optional(),
  winner_revision: id.optional() });
const schemas = { classificationArbitrationCapability: caps,
  classificationArbitrationAssignees: roster, classificationArbitrations: z.object({ ...pagination, viewer_hash: hash, results: z.array(meta) }),
  classificationArbitrationDetail: detail, createClassificationArbitration: command,
  resolveClassificationArbitration: command, cancelClassificationArbitration: command };
const methods = { classificationArbitrationCapability: "bboxArbitrationCapability", classificationArbitrationAssignees: "bboxArbitrationAssignees",
  classificationArbitrations: "bboxArbitrations", classificationArbitrationDetail: "bboxArbitrationDetail",
  createClassificationArbitration: "createBboxArbitration", resolveClassificationArbitration: "decideBboxArbitration",
  cancelClassificationArbitration: "cancelBboxArbitration" };
const valid = (value) => { if (!value) throw new Error("Invalid bbox arbitration"); };
const mapOrigin = (value) => {
  if (!value || !("expected_manifest_hash" in value)) return value;
  const { expected_manifest_hash, ...rest } = value;
  return { ...rest, expected_hash: expected_manifest_hash };
};
const Choices = ({ data, winner, onChoose }) => {
  const { t } = useLocaleTranslation("reviewBatches");
  return data.inputs.map((source) => <label key={source.submission_id} style={{ display: "block" }}>
    <input type="radio" name="arbitration-winner" checked={winner === String(source.submission_id)}
      onChange={() => onChoose(source.submission_id)} />
    {t("classificationSource", { id: source.submission_id, revision: source.revision, actor: source.submitter_id })}
  </label>);
};

export const bboxArbitrationAdapter = {
  testId: "bbox-arbitrations", requiresEvidenceReady: true,
  messages: { arbitrationTitle: "bboxArbitrationTitle", arbitrationBoundary: "bboxArbitrationBoundary",
    arbitrationDispatch: "bboxArbitrationDispatch", arbitrationStartHint: "bboxArbitrationStartHint" },
  rejected: { BBOX_ARBITRATION_INVALID_INPUT: 400, BBOX_ARBITRATION_INVALID_KEY: 400,
    BBOX_ARBITRATION_ORIGIN_CHANGED: 409, BBOX_ARBITRATION_INELIGIBLE: 409,
    BBOX_ARBITRATION_STATE_CHANGED: 409, BBOX_ARBITRATION_SOURCE_CHANGED: 409,
    BBOX_ARBITRATION_ALREADY_EXISTS: 409, BBOX_ARBITRATION_KEY_CONFLICT: 409 },
  request: (method, params, body) => ({ method: methods[method], params: mapOrigin(params), body: mapOrigin(body) }),
  parse: (method, raw, expected) => {
    const data = schemas[method].parse(raw);
    const check = (row) => valid(row.version === (row.status === "pending" ? 1 : 2));
    if (data.project_id) valid(data.project_id === expected.projectId);
    if (data.status) check(data);
    if (method === "classificationArbitrations") data.results.forEach((row) => {
      check(row); valid(row.project_id === expected.projectId && row.viewer_hash === data.viewer_hash);
    });
    if (method === "classificationArbitrationDetail") {
      valid(data.id === expected.arbitration && data.origin.project_id === data.project_id && data.origin.batch_id === data.batch_id &&
        data.origin.item.task_id === data.task_id && (data.status === "resolved") === Boolean(data.decision) &&
        (data.status === "cancelled") === Boolean(data.cancellation));
      const frozen = data.origin.item;
      const item = { task_id: frozen.task_id, outcome: "disagreement", reason: frozen.result.reason,
        cohort: frozen.cohort, comparison: frozen.result };
      bboxBatchAdapter.validate({ project_id: data.project_id, item }, { projectId: expected.projectId, task: data.task_id });
      const inputs = frozen.cohort.map((entry) => entry.source).filter(Boolean).sort((a, b) => a.submission_id - b.submission_id);
      const pair = expected.left ? [expected.left, expected.right].sort((a, b) => a - b) : inputs.slice(0, 2).map((input) => input.submission_id);
      valid(data.selected_pair[0] === pair[0] && data.selected_pair[1] === pair[1]);
      if (data.decision) valid(inputs.some((source) => source.submission_id === data.decision.winner_submission_id && source.revision === data.decision.winner_revision));
      return { ...data, available: true, report: { inputs }, inputs, item,
        cancellation: data.cancellation ? { ...data.cancellation, created_at: data.cancellation.cancelled_at } : null };
    }
    return data;
  },
  validateAcknowledgement: (result, intent) => {
    valid(result.project_id === intent.projectId && result.viewer_hash === intent.viewerHash &&
      result.receipt.actor_id === intent.actorId && result.receipt.input_set_hash === intent.inputHash &&
      result.input_set_hash === intent.inputHash);
    if (intent.action === "decision") valid(result.winner_revision === intent.winnerRevision);
  },
  canAdopt: (data, winner) => data.selected_pair.includes(winner),
  pairForWinner: (data, winner) => data.selected_pair.includes(winner) ? null
    : [data.inputs.find((source) => source.submission_id !== winner).submission_id, winner].sort((a, b) => a - b),
  renderChoices: (props) => <Choices {...props} />,
  renderEvidence: ({ data, onSelectPair, onEvidenceState }) => <BboxComparison projectId={data.project_id} answer={{ id: data.batch_id, item: data.item }}
    preferredPair={data.selected_pair} onPairSelect={onSelectPair} onEvidenceState={onEvidenceState}
    imageUrl={`${window.APP_SETTINGS.hostname}/api/projects/${data.project_id}/bbox-arbitrations/${data.id}/image/`} />,
};

export const BboxArbitrationTitle = () => { const { t } = useLocaleTranslation("reviewBatches"); return t("bboxArbitrationTitle"); };
export const BboxArbitrationEntry = ({ projectId, onOpen, disabled, dispatch = false }) => {
  const { t } = useLocaleTranslation("reviewBatches"), api = useAPI(), call = useRef(api.callApi);
  call.current = api.callApi;
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    let generation = 0, disposed = false;
    const read = async () => {
      const current = ++generation; setEnabled(false);
      try {
        const raw = await call.current("bboxArbitrationCapability", { params: { pk: projectId }, errorFilter: () => true });
        const parsed = caps.safeParse(raw);
        if (!disposed && current === generation) setEnabled(Boolean(onOpen && !raw?.error && raw?.$meta?.ok !== false &&
          typeof raw?.status !== "number" && parsed.success && parsed.data.project_id === projectId && parsed.data[dispatch ? "can_create" : "can_read"]));
      } catch { /* denied */ }
    };
    const blur = () => { generation++; setEnabled(false); };
    read(); window.addEventListener("focus", read); window.addEventListener("blur", blur);
    return () => { disposed = true; generation++; window.removeEventListener("focus", read); window.removeEventListener("blur", blur); };
  }, [projectId, dispatch, Boolean(onOpen)]);
  return enabled ? <Button disabled={disabled} onClick={onOpen} data-testid={dispatch ? "dispatch-bbox-arbitration" : "open-bbox-arbitrations"}>
    {t(dispatch ? "bboxArbitrationDispatch" : "bboxArbitrationTitle")}</Button> : null;
};
export const BboxArbitrations = (props) => <FrozenArbitrationWorkspace {...props} adapter={bboxArbitrationAdapter} />;
