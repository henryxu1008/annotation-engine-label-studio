"""Offline regressions for PR136's POSIX source/seed/host review findings."""
import ast
import copy
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

SUPPORT = Path(__file__).resolve().parents[1]
ROOT = SUPPORT.parents[1]
sys.path.insert(0, str(SUPPORT))
import ls84_contract as c


class SourceTests(unittest.TestCase):
    def test_digest_accepts_tracked_link_text_without_reading_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            link = root / 'tracked-link'
            link.write_text('elsewhere', encoding='utf-8')
            original = Path.lstat

            def lstat(path, *args, **kwargs):
                if path == link:
                    return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
                return original(path, *args, **kwargs)

            def git(workspace, *args):
                if '--stage' in args:
                    return ('120000 ' + 'a' * 40 + ' 0\ttracked-link\0').encode()
                if '--others' in args and '--cached' not in args:
                    return b''
                if 'ls-files' in args:
                    return b'tracked-link\0'
                if 'config' in args:
                    return b'true\n'
                return ('b' * 40).encode()

            with patch.object(c, 'git', side_effect=git), patch.object(Path, 'lstat', lstat), \
                    patch.object(os, 'readlink', return_value='../private-target') as readlink, \
                    patch.object(Path, 'read_bytes', side_effect=AssertionError('must not dereference target')):
                first = c.source_digest(root)
                self.assertEqual(len(first), 64)
                self.assertTrue(readlink.called)

    def test_git_symlink_placeholder_and_link_text_have_same_typed_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'link').write_bytes(b'../opaque-target')

            def git(workspace, *args):
                if '--stage' in args:
                    return ('120000 ' + 'a' * 40 + ' 0\tlink\0').encode()
                if '--others' in args:
                    return b''
                if 'config' in args:
                    return b'false\n'
                return ('b' * 40).encode()

            with patch.object(c, 'git', side_effect=git):
                first = c.source_digest(root)
                original = Path.lstat

                def lstat(path, *args, **kwargs):
                    if path == root / 'link':
                        return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
                    return original(path, *args, **kwargs)

                with patch.object(Path, 'lstat', lstat), patch.object(os, 'readlink', return_value='../opaque-target'):
                    self.assertEqual(first, c.source_digest(root))
                (root / 'link').write_bytes(b'../changed-target')
                self.assertNotEqual(first, c.source_digest(root))

    @unittest.skipUnless(os.name == 'posix', 'Native POSIX symlink shapes run in Linux CI')
    def test_native_file_directory_and_dangling_symlinks_never_hydrate_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / 'private-target'
            target.write_text('synthetic private content', encoding='utf-8')
            (root / 'file-link').symlink_to(target)
            (root / 'directory-link').symlink_to(root / 'private-dir', target_is_directory=True)
            (root / 'dangling-link').symlink_to(root / 'missing')
            (root / 'private-dir').mkdir()
            names = ('file-link', 'directory-link', 'dangling-link')

            def git(workspace, *args):
                if '--stage' in args:
                    return ''.join('120000 ' + 'a' * 40 + ' 0\t' + n + '\0' for n in names).encode()
                if '--others' in args:
                    return b''
                if 'config' in args:
                    return b'true\n'
                return ('b' * 40).encode()

            with patch.object(c, 'git', side_effect=git), patch.object(Path, 'read_bytes',
                    side_effect=AssertionError('must not read target')):
                first = c.source_digest(root)
                target.write_text('changed synthetic content', encoding='utf-8')
                self.assertEqual(first, c.source_digest(root))

    def test_unsupported_or_unmerged_index_entries_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for header in ('160000 ' + 'a' * 40 + ' 0', '100644 ' + 'a' * 40 + ' 1'):
                with patch.object(c, 'git', return_value=(header + '\tentry\0').encode()), \
                        self.assertRaises(c.SupportError):
                    c.source_digest(Path(directory).resolve())

    def test_plain_source_and_parent_reparse_substitutions_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'parent').mkdir()
            file = root / 'parent/source.py'
            file.write_text('synthetic', encoding='utf-8')

            def git(workspace, *args):
                if '--stage' in args:
                    return ('100644 ' + 'a' * 40 + ' 0\tparent/source.py\0').encode()
                if '--others' in args:
                    return b''
                if 'config' in args:
                    return b'true\n'
                return ('b' * 40).encode()

            original = c.is_link
            for substituted in (file, file.parent):
                with patch.object(c, 'git', side_effect=git), \
                        patch.object(c, 'is_link', side_effect=lambda p: p == substituted or original(p)), \
                        self.assertRaises(c.SupportError):
                    c.source_digest(root)


class SeedScopeTests(unittest.TestCase):
    def test_seed_scope_is_registered_before_submission_and_only_grants_submit(self):
        run = {'run_id': str(uuid4()), 'phase': 'seed', 'scopes': []}
        project = SimpleNamespace(pk=10, organization_id=9, created_by_id=1,
            created_by=SimpleNamespace(email='manager@' + c.DOMAIN), title='Synthetic supervision en-US',
            label_config='synthetic-static-config', _state=SimpleNamespace(adding=False))
        self.assertFalse(c.seed_submission_allowed(project, run, test_environment=True))
        c.register_seed_scope(run, project, 'synthetic-static-config', test_environment=True)
        self.assertTrue(c.seed_submission_allowed(project, run, test_environment=True))
        self.assertFalse(c.seed_submission_allowed(project, run, test_environment=False))
        other = copy.copy(project)
        other.pk = 999
        self.assertFalse(c.seed_submission_allowed(other, run, test_environment=True))
        with self.assertRaises(c.SupportError):
            c.register_seed_scope(run, project, 'synthetic-static-config', test_environment=True)

    def test_seed_scope_rejects_wrong_or_unpersisted_project_and_config(self):
        for attr, value in (('pk', 0), ('title', 'retained project'), ('label_config', 'different'),
                            ('_state', SimpleNamespace(adding=True))):
            run = {'run_id': str(uuid4()), 'phase': 'seed', 'scopes': []}
            project = SimpleNamespace(pk=10, organization_id=9, created_by_id=1,
                created_by=SimpleNamespace(email='manager@' + c.DOMAIN), title='Synthetic supervision en-US',
                label_config='static', _state=SimpleNamespace(adding=False))
            setattr(project, attr, value)
            with self.subTest(attr=attr), self.assertRaises(c.SupportError):
                c.register_seed_scope(run, project, 'static', test_environment=True)
            self.assertEqual(run['scopes'], [])

    def test_existing_seeder_body_registers_scope_before_first_submission(self):
        """Execute the actual command body against pure fakes, stopping at first submit.

        No Django import, models, database or complete seed runs in this test.
        """
        from ls84_fixture import supervision_fixture_class

        source = ast.parse((ROOT / 'label_studio/core/management/commands/seed_second_review_e2e.py').read_text(encoding='utf-8'))
        command = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'Command')
        handle = copy.deepcopy(next(n for n in command.body if isinstance(n, ast.FunctionDef) and n.name == 'handle'))
        handle.decorator_list = []
        run = {'run_id': str(uuid4()), 'phase': 'seed', 'scopes': []}
        sequence = []

        class ReachedAuthorizedSubmission(Exception):
            pass

        def user(*args, **kwargs):
            actor = SimpleNamespace(pk=len(sequence) + 1, email=args[0], save=lambda **kw: None)
            sequence.append('user')
            return actor

        def project(**kwargs):
            p = SimpleNamespace(pk=10, organization_id=9, created_by_id=kwargs['created_by'].pk,
                                _state=SimpleNamespace(adding=False), **kwargs)
            sequence.append('project')
            return p

        def submit(**kwargs):
            p = kwargs['assignment'].project
            self.assertTrue(c.seed_submission_allowed(p, run, test_environment=True))
            sequence.append('authorized_submit')
            raise ReachedAuthorizedSubmission()

        def register(p):
            sequence.append('register')
            c.register_seed_scope(run, p, 'static', test_environment=True)

        def assignment(**kwargs):
            return SimpleNamespace(pk=20, save=lambda **kw: None, **kwargs)

        globals_ = {'settings': SimpleNamespace(TEST_ENVIRONMENT=True),
            'connection': SimpleNamespace(vendor='postgresql', settings_dict={
                'NAME': 'ui_ci_second_review_ls84_test', 'HOST': '127.0.0.1'}),
            'require_owned_empty_database': lambda: None, 'Path': Path,
            'User': SimpleNamespace(objects=SimpleNamespace(create_user=user,
                filter=lambda **kw: SimpleNamespace(exists=lambda: False))),
            'Organization': SimpleNamespace(create_organization=lambda **kw:
                SimpleNamespace(pk=9, add_user=lambda actor: None)),
            'Project': SimpleNamespace(objects=SimpleNamespace(create=project)),
            'ProjectMember': SimpleNamespace(objects=SimpleNamespace(create=lambda **kw: None)),
            'Task': SimpleNamespace(objects=SimpleNamespace(create=lambda **kw: SimpleNamespace(pk=30, **kw))),
            'TaskAssignment': SimpleNamespace(objects=SimpleNamespace(create=assignment)),
            'Annotation': SimpleNamespace(objects=SimpleNamespace(create=lambda **kw: SimpleNamespace(pk=40, **kw))),
            'create_submission': submit, 'DOMAIN': c.DOMAIN, 'XML': 'static',
            'secrets': SimpleNamespace(token_urlsafe=lambda n: 'synthetic-only')}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[handle], type_ignores=[])), '<actual-seeder-body>', 'exec'),
             globals_)
        base = type('FakeBase', (), {'handle': globals_['handle']})
        scoped = supervision_fixture_class(base, register)()
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ReachedAuthorizedSubmission):
            scoped.handle(output=str(Path(directory) / 'fresh.json'))
        self.assertLess(sequence.index('register'), sequence.index('authorized_submit'))


class RunnerTests(unittest.TestCase):
    def test_offline_runner_rejects_product_import_without_loading_it(self):
        from check_support import ProductImportGuard

        for name in ('django', 'django.conf', 'label_studio', 'label_studio.core.settings', 'psycopg', 'redis', 'review_batches'):
            with self.subTest(name=name), self.assertRaises(AssertionError):
                ProductImportGuard().find_spec(name)

    def test_native_ci_budget_and_entrypoint_are_explicit(self):
        workflow = (ROOT / '.github/workflows/isolated-acceptance-support.yml').read_text(encoding='utf-8')
        self.assertIn('timeout-minutes: 5', workflow)
        self.assertIn('python -B -S scripts/acceptance/check_support.py', workflow)
        self.assertNotIn('services:', workflow)
        self.assertNotIn('workflow_dispatch:', workflow)
        self.assertNotIn('audit-cycle', workflow)


class BridgeHostTests(unittest.TestCase):
    def test_internal_api_client_host_allowed_only_in_explicit_bridge(self):
        module = ast.parse((SUPPORT / 'ls84_runtime.py').read_text(encoding='utf-8'))
        values = [ast.literal_eval(v) for node in ast.walk(module) if isinstance(node, ast.Dict)
                  for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant) and k.value == 'ALLOWED_HOSTS']
        self.assertEqual(values, [['127.0.0.1', 'localhost', 'testserver']])


if __name__ == '__main__':
    unittest.main()
