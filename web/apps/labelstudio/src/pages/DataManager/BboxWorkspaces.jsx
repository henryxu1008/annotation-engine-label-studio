import { FrozenBatchWorkspace } from "./ClassificationBatches";
import { bboxBatchAdapter, BboxComparison } from "./BboxBatches";
import { BboxArbitrationEntry, BboxArbitrations } from "./BboxArbitrations";

const adapter = { ...bboxBatchAdapter,
  renderItem: (props) => <>
    {props.answer.manifest_hash && props.answer.item.outcome === "disagreement" &&
      <BboxArbitrationEntry projectId={props.projectId} dispatch disabled={props.busy} onOpen={props.onOpenArbitration} />}
    <BboxComparison {...props} />
  </>,
  renderArbitration: ({ projectId, answer, onBack }) => <BboxArbitrations projectId={projectId}
    initialOrigin={{ batch_id: answer.id, task_id: answer.item.task_id, expected_manifest_hash: answer.manifest_hash }}
    onBackToSource={onBack} />,
};
export const BboxBatchWorkspace = (props) => <FrozenBatchWorkspace {...props} adapter={adapter} />;
