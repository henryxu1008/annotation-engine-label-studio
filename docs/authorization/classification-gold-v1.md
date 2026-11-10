# Classification gold evidence V1 (#82)

This optional Fork-only slice creates a manager-selected immutable reference
from one exact existing classification Submission and evaluates an independent
Submission of the same task against it. It is not blind delivery, an official
review decision, automatic approval, a release, a trained-model evaluation,
an alert or a Host integration. A manager's selection is an asserted reference,
not proof that its labels are objectively correct. #82 remains open for its
remaining scope.

`classification-gold-exact-controls/v1` reuses the existing static Text/Choices
parser and answer validator. Each supported nonempty control contributes one
match when its selected choice sets are exactly equal, independent of ordering.
The result records matched and total controls and per-control expected/actual
values. Unsupported configurations, invalid labels, empty/missing answers,
cancelled assignments, corrupt snapshots or mismatched task/configuration are
rejected rather than turned into zero or perfect scores. The source submission
ID, assignment, actor, revision and result hash, reference hash and rule are
frozen. A new reference produces a new immutable evidence ID; earlier scores
continue to use their original version. Current revisions are never silently
substituted. Reference and candidate must belong to different submitters.

The feature is off unless both `ANNOTATION_CLASSIFICATION_GOLD_ENABLED=true`
and `ANNOTATION_CLASSIFICATION_GOLD_PROJECTS` contains the exact
`"<organization-id>:<project-id>": true` entry. Live project-manager and
`organize_classification_gold` policy checks are mandatory on reads and writes;
UI visibility is not an authorization boundary. Disabling access hides existing
records without changing them. Writes require a UUID Idempotency-Key; reusing
the same key with changed input or principal conflicts. Clients bind the exact
source result hash and reference hash selected before the write; changed input
is rejected. An uncertain write is retried only with the original body and key.

This is not a secret gold-sample delivery system. Existing upstream ground-truth
fields, editor indicators and selection behavior cannot be treated as blind.
Before any future blind mode, separately audit all list, metadata, export,
event, media and timing paths and specify its leakage threat model. Existing
annotation exposure, historical source deletion, configured identity providers,
target deployment and business acceptance require their own validation.
