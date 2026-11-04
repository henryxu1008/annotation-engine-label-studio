"""Native shared file primitives; Linux link/owner proof is distinct from Windows."""
import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core.utils import private_file as module


class TestPrivateFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='audit84-file-')
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.path = self.root / 'frozen.bin'
        self.data = b'SYNTHETIC_AUDIT_BYTES_0123456789'
        self.path.write_bytes(self.data)
        self.hash = hashlib.sha256(self.data).hexdigest()

    def test_same_fd_rewound_and_closed(self):
        with module.checked_file(self.path, self.hash, len(self.data)) as stream:
            assert stream.tell() == 0 and stream.read() == self.data
        assert stream.closed

    @unittest.skipUnless(os.name == 'posix', 'Native POSIX link/owner branch')
    def test_leaf_symlink_rejected(self):
        link = self.root / 'leaf'
        link.symlink_to(self.path)
        with self.assertRaises(module.PrivateFileError):
            with module.checked_file(link, self.hash, len(self.data)):
                pass

    @unittest.skipUnless(os.name == 'posix', 'Native POSIX link/owner branch')
    def test_ancestor_symlink_rejected(self):
        actual = self.root / 'actual'
        actual.mkdir()
        (actual / 'f').write_bytes(self.data)
        link = self.root / 'alias'
        link.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(module.PrivateFileError):
            with module.checked_file(link / 'f', self.hash, len(self.data)):
                pass

    def test_traversal_spelling_rejected(self):
        child = self.root / 'child'
        child.mkdir()
        with self.assertRaises(module.PrivateFileError):
            with module.checked_file(child / '..' / self.path.name, self.hash, len(self.data)):
                pass

    def test_missing_rejected(self):
        with self.assertRaises(module.PrivateFileError):
            with module.checked_file(self.root / 'missing', self.hash, len(self.data)):
                pass

    def test_size_rejected(self):
        with self.assertRaises(module.PrivateFileError):
            with module.checked_file(self.path, self.hash, len(self.data) + 1):
                pass

    def test_hash_rejected(self):
        with self.assertRaises(module.PrivateFileError):
            with module.checked_file(self.path, '0' * 64, len(self.data)):
                pass

    def test_directory_rejected(self):
        with self.assertRaises(module.PrivateFileError):
            with module.checked_file(self.root, self.hash, self.root.stat().st_size):
                pass

    @unittest.skipUnless(os.name == 'posix', 'Native POSIX link/owner branch')
    def test_leaf_substitution_at_open_rejected(self):
        other = self.root / 'other'
        other.write_bytes(b'PRIVATE_OTHER_RECORD')
        original = os.open

        def swap(path, *args, **kwargs):
            self.path.unlink()
            self.path.symlink_to(other)
            return original(path, *args, **kwargs)

        with patch.object(module.os, 'open', side_effect=swap), self.assertRaises(module.PrivateFileError):
            with module.checked_file(self.path, self.hash, len(self.data)):
                pass

    def test_in_place_tamper_after_hash_before_yield_rejected(self):
        original = hashlib.file_digest

        def tamper(handle, *args, **kwargs):
            result = original(handle, *args, **kwargs)
            self.path.write_bytes(b'X' * len(self.data))
            return result

        with patch.object(module.hashlib, 'file_digest', side_effect=tamper), self.assertRaises(
            module.PrivateFileError
        ):
            with module.checked_file(self.path, self.hash, len(self.data)):
                pass

    def test_authorized_os_writer_after_yield_is_outside_guarantee(self):
        with module.checked_file(self.path, self.hash, len(self.data)) as stream:
            self.path.write_bytes(b'Y' * len(self.data))
            assert stream.read() == b'Y' * len(self.data), 'an open FD is not an immutable inode snapshot'

    def test_independent_process_tamper_before_first_byte_is_rejected(self):
        original = hashlib.file_digest
        writers = []

        def after_hash(handle, *args, **kwargs):
            digest = original(handle, *args, **kwargs)
            child = subprocess.run(
                [
                    sys.executable,
                    '-B',
                    '-c',
                    'import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b"Z"*int(sys.argv[2])); print(os.getpid())',
                    str(self.path),
                    str(len(self.data)),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert child.returncode == 0, child.stderr
            writers.append(int(child.stdout.strip()))
            return digest

        with patch.object(module.hashlib, 'file_digest', side_effect=after_hash), self.assertRaises(
            module.PrivateFileError
        ):
            with module.checked_file(self.path, self.hash, len(self.data)):
                self.fail('No descriptor or first byte may escape after the independent write')
        assert len(writers) == 1 and writers[0] != os.getpid()

    @unittest.skipUnless(os.name == 'posix', 'Native POSIX link/owner branch')
    def test_private_owner_and_permissions_gate(self):
        self.root.chmod(0o700)
        module.directory_gate(self.root, owner_id=os.getuid())
        self.root.chmod(0o777)
        with self.assertRaises(module.PrivateFileError):
            module.directory_gate(self.root, owner_id=os.getuid())

    @unittest.skipUnless(os.name == 'posix', 'Native POSIX link/owner branch')
    def test_wrong_owner_gate(self):
        with self.assertRaises(module.PrivateFileError):
            module.directory_gate(self.root, owner_id=os.getuid() + 1)

    def test_public_equal_gate(self):
        with self.assertRaises(module.PrivateFileError):
            module.directory_gate(self.root, [self.root])

    def test_public_ancestor_gate(self):
        with self.assertRaises(module.PrivateFileError):
            module.directory_gate(self.root, [self.root.parent])

    def test_public_descendant_gate(self):
        with self.assertRaises(module.PrivateFileError):
            module.directory_gate(self.root, [self.root / 'public'])

    @unittest.skipUnless(os.name == 'posix', 'Native POSIX directory symlink branch')
    def test_directory_ancestor_link_gate(self):
        child = self.root / 'real'
        child.mkdir()
        link = self.root / 'alias'
        link.symlink_to(child, target_is_directory=True)
        with self.assertRaises(module.PrivateFileError):
            module.directory_gate(link)

    def test_fd_failure_no_leak(self):
        if os.name != 'posix':
            self.skipTest('Native POSIX descriptor count')
        before = len(list(pathlib.Path('/proc/self/fd').iterdir()))
        for _ in range(30):
            with self.assertRaises(module.PrivateFileError):
                with module.checked_file(self.path, '0' * 64, len(self.data)):
                    pass
        assert len(list(pathlib.Path('/proc/self/fd').iterdir())) == before


def unsafe_ancestor(self):
    parent = self.root / 'untrusted'
    parent.mkdir()
    parent.chmod(0o777)
    child = parent / 'private'
    child.mkdir(mode=0o700)
    with self.assertRaises(module.PrivateFileError):
        module.ancestor_gate(child, owner_id=os.getuid())


def sticky_ancestor(self):
    parent = self.root / 'sticky'
    parent.mkdir()
    parent.chmod(0o1777)
    child = parent / 'private'
    child.mkdir(mode=0o700)
    module.ancestor_gate(child, owner_id=os.getuid())


def trusted_ancestor(self):
    module.ancestor_gate(self.root, owner_id=os.getuid())


TestPrivateFiles.test_unsafe_ancestor = unittest.skipUnless(os.name == 'posix', 'Native POSIX ancestor branch')(
    unsafe_ancestor
)
TestPrivateFiles.test_sticky_ancestor = unittest.skipUnless(os.name == 'posix', 'Native POSIX ancestor branch')(
    sticky_ancestor
)
TestPrivateFiles.test_trusted_ancestor = unittest.skipUnless(os.name == 'posix', 'Native POSIX ancestor branch')(
    trusted_ancestor
)
