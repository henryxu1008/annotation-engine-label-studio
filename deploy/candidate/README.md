# Local candidate build storage

Use `python scripts/build_fixed_candidate.py --help` for the existing exact-commit
builder. Each output directory must be new and outside the checkout. Git blobs,
file modes, symlinks, the source-owned recipe, pinned base identities and archive
hashes continue to define the candidate inputs.

The builder now uses Python's `TemporaryDirectory` for its own source checkout,
`source.tar`, and `context.tar.gz`. It releases this workspace after both successful
and failed builds, including ordinary exceptions and keyboard interruption.
The output keeps `Dockerfile.debian`, `build-inputs/`, and `evidence/` (input hashes,
commands, build logs and Docker metadata). `evidence/build-workspace.json` records
whether the workspace was released and whether the build succeeded. This is local
build storage management; it does not activate or remove images or containers.

For deliberate debugging, pass `--retain-build-workspace`. The retained directory
is recorded in the receipt. This opt-in needs manual review and removal when its
owner finishes debugging. A force-killed process or host crash can also leave a
workspace; an `IN_PROGRESS` receipt is not permission to delete it.

There is no age-based or global sweep. Runtime directories, other builds, Docker
BuildKit caches, images, volumes, backups and acceptance evidence remain outside
the cleanup boundary. Existing deployments made by older builders require a
separate reference/ownership audit before removing their intermediate files.

Reuse check: the existing builder and Docker BuildKit already own build execution
and caching. Python's standard-library temporary-directory lifecycle supplies
bounded scratch storage, so no extra cache service, scheduler, package or parallel
build infrastructure is introduced.

Run the offline contract suite with:

```sh
python -m unittest discover -s scripts -p 'test_candidate_*.py' -v
```
