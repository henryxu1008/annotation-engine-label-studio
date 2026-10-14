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

    def invoke(self, source):
        destination = self.root / "output"
        args = ["build_fixed_candidate.py", "--repository", str(self.repo), "--source", source,
                "--output", str(destination), "--version", "1.23.0+fork.fixture", "--tag", "ls-fixture:local"]
        real_run, real_helper = subprocess.run, subject.run
        docker_calls = []

        def command(command, *positional, **keywords):
            if command[0] == "docker":
                docker_calls.append(command)
                return subprocess.CompletedProcess(command, 0)
            return real_run(command, *positional, **keywords)

        def helper(command, cwd=None):
            if command[0] == "docker":
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
            subject.main()
        self.assertEqual(len(docker_calls), 1)
        self.assertEqual((destination / "Dockerfile.debian").read_bytes(), self.original)
        metadata = json.loads((destination / "evidence/build-inputs.json").read_text())
        self.assertEqual(metadata["dockerfile_sha256"], hashlib.sha256(self.original).hexdigest())
        with tarfile.open(destination / "context.tar.gz") as context:
            self.assertEqual(context.extractfile("Dockerfile.debian").read(), self.original)
            self.assertEqual(context.extractfile("deploy/candidate/Dockerfile.debian").read(), self.original)

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


if __name__ == "__main__":
    unittest.main()
