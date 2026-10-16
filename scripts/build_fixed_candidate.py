"""Build the Debian candidate from exact Git objects, including on Windows.

Output must be a new, exclusive directory outside the checkout. No runtime secrets
are copied into the build context. This entry point only builds a local image.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def run(args, cwd=None):
    return subprocess.check_output(args, cwd=cwd).decode().strip()


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


RECIPE_PATH = "deploy/candidate/Dockerfile.debian"


@contextlib.contextmanager
def build_workspace(root, retain=False):
    """Own one scratch directory; never sweep other builds or runtime files."""
    options = dict(prefix=".build-work-", dir=root)
    manager = (contextlib.nullcontext(tempfile.mkdtemp(**options)) if retain
               else tempfile.TemporaryDirectory(**options))
    receipt = {"policy": "RETAIN_EXPLICITLY" if retain else "RELEASE_ON_EXIT",
               "status": "IN_PROGRESS", "started_at": datetime.now(timezone.utc).isoformat()}
    path = root / "evidence/build-workspace.json"
    workspace = None
    try:
        with manager as directory:
            workspace = Path(directory).resolve()
            if workspace.parent != root.resolve():
                raise RuntimeError("Build workspace escaped its exclusive output directory")
            receipt["workspace"] = str(workspace)
            path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            yield workspace
            receipt["status"] = "SUCCEEDED"
    except BaseException as error:
        receipt["status"] = "FAILED"
        receipt["error_type"] = type(error).__name__
        raise
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        receipt["released"] = workspace is not None and not workspace.exists()
        path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def main():
    if not __debug__:
        raise SystemExit("Git object assertions require Python without optimization")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--source", required=True, help="Full 40-character commit SHA")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--branch", default="main", help="Recorded snapshot branch, including unmerged candidates")
    parser.add_argument("--node-image", default="mirror.gcr.io/library/node:20-alpine")
    parser.add_argument("--python-image", default="mirror.gcr.io/library/python:3.11-slim-bookworm")
    parser.add_argument("--retain-build-workspace", action="store_true",
                        help="Explicitly keep this build's source and tar files for debugging; "
                             "otherwise release them on success and failure, preserving evidence")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source):
        parser.error("--source must be a full commit SHA")
    repo, root = args.repository.resolve(), args.output.resolve()
    if root == repo or repo in root.parents:
        parser.error("Output must be outside the repository")
    if run(["git", "rev-parse", args.source + "^{commit}"], repo) != args.source:
        parser.error("Source object does not resolve to the supplied SHA")
    try:
        recipe_bytes = subprocess.check_output(["git", "show", args.source + ":" + RECIPE_PATH],
                                               cwd=repo, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError:
        parser.error("Source commit does not contain the admitted candidate recipe: " + RECIPE_PATH)
    root.mkdir(parents=True, exist_ok=False)
    evidence = root / "evidence"
    evidence.mkdir()
    (root / "build-inputs").mkdir()
    with build_workspace(root, args.retain_build_workspace) as workspace:
        build(args, repo, root, evidence, workspace, recipe_bytes)


def build(args, repo, root, evidence, workspace, recipe_bytes):
    source = workspace / "source"
    subprocess.run(["git", "init", "-b", args.branch, str(source)], check=True, capture_output=True)
    for key, value in (("core.autocrlf", "false"), ("core.symlinks", "false")):
        subprocess.run(["git", "config", key, value], cwd=source, check=True)
    subprocess.run(["git", "fetch", "--depth=1", repo.as_uri(), args.source], cwd=source, check=True)
    subprocess.run(["git", "checkout", "-B", args.branch, "FETCH_HEAD"], cwd=source, check=True, capture_output=True)
    assert run(["git", "rev-parse", "HEAD"], source) == args.source
    assert not run(["git", "status", "--porcelain"], source)
    installer = root / "build-inputs/install-poetry.py"
    installer_url = "https://install.python-poetry.org"
    with urllib.request.urlopen(installer_url, timeout=60) as response:
        installer.write_bytes(response.read())
    recipe = root / "Dockerfile.debian"
    recipe.write_bytes(recipe_bytes)
    bases = {}
    for name, ref in (("node", args.node_image), ("python", args.python_image)):
        manifest = json.loads(run(["docker", "buildx", "imagetools", "inspect", ref,
                                   "--format", "{{json .Manifest}} "]))
        selected = next(m for m in manifest["manifests"] if m.get("platform") == {"architecture": "amd64", "os": "linux"})
        bases[name] = {"requested": ref, "index_digest": manifest["digest"],
                       "platform_manifest_digest": selected["digest"],
                       "pinned": ref.split("@")[0].rsplit(":", 1)[0] + "@" + manifest["digest"]}
    archive, context = workspace / "source.tar", workspace / "context.tar.gz"
    subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "tar.umask=0022",
                    "archive", "--format=tar", "-o", str(archive), args.source], cwd=source, check=True)
    tracked = {}
    for record in subprocess.check_output(["git", "ls-tree", "-rz", args.source], cwd=source).split(b"\0"):
        if record:
            metadata, name = record.split(b"\t", 1)
            mode, kind, object_id = metadata.decode().split()
            assert kind == "blob", "Submodules need a separately admitted build input"
            tracked[name.decode()] = (mode, object_id)
    links, verified, executables = {}, 0, 0
    with tarfile.open(archive) as src, tarfile.open(context, "w:gz", compresslevel=1, format=tarfile.PAX_FORMAT) as dst:
        for member in src:
            assert not member.name.startswith("/") and ".." not in Path(member.name).parts
            if member.isfile() or member.issym():
                mode, object_id = tracked[member.name]
                payload = member.linkname.encode() if member.issym() else src.extractfile(member).read()
                assert hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest() == object_id, member.name
                assert (mode == "120000" and member.issym()) or member.mode == (int(mode, 8) & 0o777), member.name
                verified += 1
            if member.issym():
                links[member.name] = member.linkname
            if member.isfile() and member.mode & 0o111:
                executables += 1
            # Replace rather than duplicate the ignore entry in the archive.
            if member.name != ".dockerignore":
                dst.addfile(member, src.extractfile(member) if member.isfile() else None)
        dst.add(source / ".git", arcname=".git")
        dst.add(root / "Dockerfile.debian", arcname="Dockerfile.debian")
        dst.add(installer, arcname="build-inputs/install-poetry.py")
        ignore = subprocess.check_output(["git", "show", args.source + ":.dockerignore"], cwd=source)
        ignore += b"\n!build-inputs/install-poetry.py\n"
        member = tarfile.TarInfo(".dockerignore")
        member.mode, member.size = 0o644, len(ignore)
        dst.addfile(member, io.BytesIO(ignore))
    assert verified == len(tracked)
    inputs = {"source_commit": args.source, "source_tree": run(["git", "rev-parse", "HEAD^{tree}"], source),
              "version": args.version, "platform": "linux/amd64", "tag": args.tag, "branch": args.branch,
              "source_archive_sha256": sha(archive), "context_sha256": sha(context),
              "dockerfile_git_path": RECIPE_PATH, "dockerfile_sha256": sha(recipe),
              "installer": {"url": installer_url, "sha256": sha(installer)},
              "bases": bases, "verified_git_blob_count": verified, "executable_file_count": executables,
              "git_tracked_links": links, "frontend_history": "depth-1 source snapshot; candidate identity",
              "lockfiles": {name: hashlib.sha256(subprocess.check_output(["git", "show", args.source + ":" + name], cwd=source)).hexdigest()
                            for name in ("poetry.lock", "pyproject.toml", "web/yarn.lock", "web/package.json")},
              "bit_for_bit_rebuild_promised": False}
    (evidence / "build-inputs.json").write_text(json.dumps(inputs, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"canonical_context": "PASS", "verified_blobs": verified, "context_sha256": inputs["context_sha256"]}), flush=True)
    command = ["docker", "buildx", "build", "--platform", "linux/amd64", "--load", "--progress", "plain",
               "--metadata-file", str(evidence / "build-metadata.json"),
               "--build-arg", "NODE_IMAGE=" + bases["node"]["pinned"],
               "--build-arg", "PYTHON_IMAGE=" + bases["python"]["pinned"],
               "--build-arg", "NODE_VERSION=20", "--build-arg", "PYTHON_VERSION=3.11",
               "--build-arg", "POETRY_VERSION=2.3.2", "--build-arg", "VERSION_OVERRIDE=" + args.version,
               "--build-arg", "BRANCH_OVERRIDE=" + args.branch, "--label", "org.opencontainers.image.revision=" + args.source,
               "--label", "org.opencontainers.image.version=" + args.version,
               "--label", "org.opencontainers.image.source=https://github.com/qq550723504/annotation-engine-label-studio",
               "-t", args.tag, "-f", "Dockerfile.debian", "-"]
    (evidence / "build-command.json").write_text(json.dumps(command, indent=2) + "\n", encoding="utf-8")
    with context.open("rb") as stdin, (evidence / "build.log").open("wb") as log:
        result = subprocess.run(command, stdin=stdin, stdout=log, stderr=subprocess.STDOUT, cwd=root)
    if result.returncode:
        raise SystemExit("Build failed; retained log: " + str(evidence / "build.log"))
    print(json.dumps({"build": "PASS", "metadata": str(evidence / "build-metadata.json")}), flush=True)


if __name__ == "__main__":
    main()
