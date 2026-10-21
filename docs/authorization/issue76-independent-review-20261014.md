# Issue #76 independent review, 2026-10-14

A fresh read-only reviewer examined all 83 changed files in PR #77 at
`663043dd5af61f8931aa4290e3f849a0b0ddd348`, against base
`f3b69385f5d892f89d1853f0cecbd739eed69783`. This is an agent code review;
it does not represent a submitted GitHub approval or authorize a merge.

The reviewer found two P1 candidate-delivery contract defects:

- The build entry point fetched its Dockerfile from the executing checkout,
  even when `--source` named another commit. Dirty/newer instructions could
  therefore receive an older source revision label. It now obtains the recipe
  from that exact Git object and rejects an absent recipe before creating the
  output directory or using the network. Both the context and recorded recipe
  checksum use those captured bytes.
- The offline verifier authenticated the selected OCI graph without checking
  which graph a Docker importer would consume. A self-consistent archive could
  omit OCI layout and point its Docker manifest at a different config while
  still receiving PASS. The admitted Docker-save hybrid profile now requires
  OCI layout 1.0.0, one candidate root/platform, matching Docker config, ordered
  layers and image name, and canonical regular metadata entries. Extra images,
  conflicting aliases and archive links are rejected. BuildKit attestations
  linked to the selected platform remain supported.

Follow-up review confirmed both P1 fixes and found one P2 path-admission gap:
`normpath` leaves leading `../payload` and `..` unchanged. The verifier now
explicitly rejects parent-directory components and root pseudoentries. Three
negative tests cover `../payload`, `..` and `.`. This was a malformed-input
admission defect; no Docker extraction escape or existing artifact pollution
was demonstrated. Linked-attestation positive and missing-layer negative cases
are also retained as contract tests.

The two consumption paths are based on the upstream implementations:
[containerd ImportIndex](https://github.com/containerd/containerd/blob/main/core/images/archive/importer.go#L119-L171)
selects Docker format when OCI layout is absent;
[Moby classic Docker load](https://github.com/moby/moby/blob/v28.5.1/image/tarexport/load.go#L51-L90)
reads `manifest.json`. These checks compare package contents to the admitted
manifest; they are not signatures or independent proof of provenance.

All **23 isolated contract tests pass** after the fixes. On the original review
HEAD the same suite has **20 failures and three passing controls**, with no
fixture errors. The earlier 18-test result remains separately retained. Build tests
use small temporary Git repositories and mock only network/Docker calls; they
do not build or load images. Archive tests recompute the approved package SHA
after each controlled mutation, so the negative cases test internal consumption
consistency rather than merely failing the outer checksum. Fork PR Gate runs
these tests on every PR head.

The existing artifact was not found to be contaminated. Its recipe matches the
`af1cb115` source blob, its OCI/Docker configs agree, and it passes the hardened
verifier including its linked BuildKit attestation. Its archive SHA remains
`adf74a5d31f8961109e3de63eff01ae1e89f3335b5568406edb151dcc4191673`.
The runtime remains `af1cb115c96dcd1b9a5b179938752d8b750fbab9`; no serving image
replacement or new runtime acceptance is claimed for these tooling fixes.
The [48-case native qualification and its limits](issue76-recheck-20261013-latest.md)
remain bound to that artifact.

The raw initial report, before/after logs, current-head follow-up review and CI
readback are retained in
`C:\Users\Henry\.codex\reports\ls-issue76-independent-review-20261014`.
Any newer PR head requires corresponding review and CI evidence; earlier CI
does not qualify it. PR #77 stays draft and Issue #76 stays open. Receiving
environment, trusted-browser HTTPS, the complete second daily cleanup interval
and an authorized old security/schema rollback target remain separate gates.
IAM operations/recovery and Core business integration remain paused. No new
Compose project, certificate-trust change, production deployment or merge is
part of this work.
