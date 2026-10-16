"""Build input provenance without network, a Docker daemon, or real images."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import build_fixed_candidate as subject


class BuildContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ls76-build-test-")
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(self.temporary.cleanup)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Build contract fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.repo / ".dockerignore").write_text(".git/hooks\n", encoding="utf-8")
        (self.repo / "source.txt").write_text("synthetic input\n", encoding="utf-8")
        for name in ["poetry.lock", "pyproject.toml", "web/yarn.lock", "web/package.json"]:
            path = self.repo / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("{}\n" if name.endswith(".json") else "# synthetic lock input\n", encoding="utf-8")
        self.commit()
        self.without_recipe = self.git("rev-parse", "HEAD")
        self.recipe = self.repo / subject.RECIPE_PATH if hasattr(subject, "RECIPE_PATH") else self.repo / "deploy/candidate/Dockerfile.debian"
        self.recipe.parent.mkdir(parents=True)
        self.original = b"FROM scratch\nCOPY source.txt /input\n"
        self.recipe.write_bytes(self.original)
        self.commit()
        self.source = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.PIPE).decode().strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-m", "synthetic build input")

    def invoke(self, source, *, retain=False, failure=None):
        destination = self.root / "output"
        args = ["build_fixed_candidate.py", "--repository", str(self.repo), "--source", source,
                "--output", str(destination), "--version", "1.23.0+fork.fixture", "--tag", "ls-fixture:local"]
        if retain:
            args.append("--retain-build-workspace")
        real_run, real_helper = subprocess.run, subject.run
        docker_calls = []
        contexts = []

        def command(command, *positional, **keywords):
            if command[0] == "docker":
                docker_calls.append(command)
                # Inspect while Docker owns its input stream; it is scratch storage.
                with tarfile.open(fileobj=keywords["stdin"], mode="r:gz") as context:
                    contexts.append({name: context.extractfile(name).read() for name in
                                     ["Dockerfile.debian", subject.RECIPE_PATH, "source.txt"]})
                if failure == "interrupt":
                    raise KeyboardInterrupt()
                if failure == "docker":
                    return subprocess.CompletedProcess(command, 1)
                return subprocess.CompletedProcess(command, 0)
            return real_run(command, *positional, **keywords)

        def helper(command, cwd=None):
            if command[0] == "docker":
                if failure == "manifest":
                    raise RuntimeError("synthetic manifest lookup failed")
                return json.dumps({"digest": "sha256:" + "1" * 64, "manifests": [
                    {"digest": "sha256:" + "2" * 64, "platform": {"os": "linux", "architecture": "amd64"}}]})
            return real_helper(command, cwd)

        with patch.object(sys, "argv", args), patch.object(subject, "__file__", str(self.repo / "scripts/build_fixed_candidate.py")), \
                patch.object(subject.subprocess, "run", side_effect=command), patch.object(subject, "run", side_effect=helper), \
                patch.object(subject.urllib.request, "urlopen", return_value=io.BytesIO(b"# synthetic installer\n")) as network, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if source == self.without_recipe:
                with self.assertRaises(SystemExit):
                    subject.main()
                network.assert_not_called()
                self.assertFalse(destination.exists())
                self.assertEqual(docker_calls, [])
                return
            if failure:
                expected = {"docker": SystemExit, "manifest": RuntimeError,
                            "interrupt": KeyboardInterrupt}[failure]
                with self.assertRaises(expected):
                    subject.main()
            else:
                subject.main()
        receipt = json.loads((destination / "evidence/build-workspace.json").read_text())
        self.assertEqual(receipt["status"], "FAILED" if failure else "SUCCEEDED")
        self.assertEqual(receipt["released"], not retain)
        workspace = Path(receipt["workspace"])
        self.assertEqual(workspace.parent, destination)
        self.assertEqual(workspace.exists(), retain)
        self.assertEqual(list(destination.glob(".build-work-*")), [workspace] if retain else [])
        self.assertFalse((destination / "source").exists())
        self.assertFalse((destination / "source.tar").exists())
        self.assertFalse((destination / "context.tar.gz").exists())
        if failure == "manifest":
            self.assertEqual(docker_calls, [])
            return
        self.assertEqual(len(docker_calls), 1)
        self.assertEqual((destination / "Dockerfile.debian").read_bytes(), self.original)
        metadata = json.loads((destination / "evidence/build-inputs.json").read_text())
        self.assertEqual(metadata["dockerfile_sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(contexts[0]["Dockerfile.debian"], self.original)
        self.assertEqual(contexts[0][subject.RECIPE_PATH], self.original)
        self.assertEqual(contexts[0]["source.txt"], subprocess.check_output(
            ["git", "show", source + ":source.txt"], cwd=self.repo))
        if failure:
            self.assertTrue((destination / "evidence/build.log").exists())
            self.assertTrue((destination / "evidence/build-command.json").exists())
        if retain:
            with tarfile.open(workspace / "context.tar.gz") as context:
                self.assertEqual(context.extractfile("Dockerfile.debian").read(), self.original)

    def test_current_source_is_preserved(self):
        self.invoke(self.source)

    def test_dirty_checkout_recipe_is_not_a_build_input(self):
        self.recipe.write_bytes(b"FROM unrelated-dirty-checkout\n")
        self.invoke(self.source)

    def test_older_source_cannot_borrow_a_newer_recipe(self):
        self.recipe.write_bytes(b"FROM newer-committed-checkout\n")
        self.commit()
        self.invoke(self.source)

    def test_source_without_recipe_fails_before_network_or_output(self):
        self.invoke(self.without_recipe)

    def test_failed_docker_build_releases_scratch_and_keeps_evidence(self):
        self.invoke(self.source, failure="docker")

    def test_failed_manifest_lookup_releases_checkout(self):
        self.invoke(self.source, failure="manifest")

    def test_keyboard_interrupt_releases_scratch(self):
        self.invoke(self.source, failure="interrupt")

    def test_explicit_debug_retention_keeps_owned_workspace(self):
        self.invoke(self.source, retain=True)

    def test_explicit_debug_retention_also_keeps_failed_workspace(self):
        self.invoke(self.source, retain=True, failure="docker")

    def test_cleanup_preserves_other_builds_and_runtime_files(self):
        other = self.root / "other-candidate"
        other.mkdir()
        sentinel = other / "backup.tar"
        sentinel.write_bytes(b"retained backup")
        self.invoke(self.source)
        self.assertEqual(sentinel.read_bytes(), b"retained backup")

    def test_existing_output_is_rejected_without_cleaning_it(self):
        destination = self.root / "output"
        destination.mkdir()
        sentinel = destination / "backup.tar"
        sentinel.write_bytes(b"existing candidate backup")
        with self.assertRaises(FileExistsError):
            self.invoke(self.source)
        self.assertEqual(sentinel.read_bytes(), b"existing candidate backup")


if __name__ == "__main__":
    unittest.main()
