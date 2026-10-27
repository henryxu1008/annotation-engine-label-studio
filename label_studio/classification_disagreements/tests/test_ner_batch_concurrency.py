"""True independent PostgreSQL writers/readers, never SQLite concurrency claims."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from access_control.project_operations import ORGANIZE_NER, READ_WORK, ProjectOperationPolicy
from classification_disagreements import ner
from classification_disagreements.models import NerBatch
from classification_disagreements.tests import test_ner_batches as f
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.models.query import QuerySet
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project
from rest_framework.response import Response
from rest_framework.test import APIClient
from tasks.models import Submission
from tasks.submissions import _snapshot_hash


class ReadPolicy:
    def evaluate(self, project):
        return ProjectOperationPolicy('ner-pg-policy/v1', 'active',
            frozenset({ORGANIZE_NER} if project.description == 'deny-read' else {READ_WORK, ORGANIZE_NER}))


@override_settings(ANNOTATION_NER_DISAGREEMENTS_ENABLED=True, ANNOTATION_NER_BATCHES_ENABLED=True)
class TestNerBatchConcurrency(TransactionTestCase):
    client_class = APIClient
    make_source = f.TestNerBatches.make_source
    write = f.TestNerBatches.write
    preview = f.TestNerBatches.preview
    create = f.TestNerBatches.create
    item = f.TestNerBatches.item

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Requires real PostgreSQL independent connections')
        f.TestNerBatches.setUpTestData.__func__(self)
        f.TestNerBatches.setUp(self)

    def post(self, body, key, barrier=None):
        close_old_connections()
        try:
            client = APIClient()
            client.force_authenticate(self.manager)
            if barrier:
                barrier.wait(15)
            return client.post(self.base, body, format='json', HTTP_IDEMPOTENCY_KEY=key)
        finally:
            connection.close()

    def body(self, preview=None):
        p = preview or self.preview().data
        return {'preview_token':p['preview_token'], 'expected_plan_hash':p['plan_hash']}

    def test_same_key_concurrent_create_commits_one_batch(self):
        body, key, barrier = self.body(), str(uuid4()), threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.post,body,key,barrier) for _ in range(2)]
            rows = [v.result(35) for v in futures]
        self.assertEqual(sorted(r.status_code for r in rows), [200,201], [r.data for r in rows])
        self.assertEqual(len({r.data['id'] for r in rows}), 1)
        self.assertEqual(NerBatch.objects.count(), 1)

    def test_same_key_different_requests_cannot_create_two_batches(self):
        bodies = [self.body(), self.body(self.preview(name='Different frozen name').data)]
        key, barrier = str(uuid4()), threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.post,body,key,barrier) for body in bodies]
            rows = [v.result(35) for v in futures]
        self.assertEqual(sorted(r.status_code for r in rows), [201,409], [r.data for r in rows])
        self.assertEqual(NerBatch.objects.count(), 1)

    def test_project_write_fence_survives_actual_response_rendering(self):
        entered, release = threading.Event(), threading.Event()
        render = Response.render
        def wait(response):
            value = render(response)
            if response.status_code == 201:
                entered.set()
                assert release.wait(20)
            return value
        body, key = self.body(), str(uuid4())
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(Response,'render',wait):
            future = pool.submit(self.post,body,key)
            try:
                self.assertTrue(entered.wait(20))
                with self.assertRaises(DatabaseError), transaction.atomic():
                    Project.objects.select_for_update(nowait=True).get(pk=self.project.pk)
            finally:
                release.set()
            self.assertEqual(future.result(25).status_code, 201)

    def test_revocation_committed_first_denies_waiting_creation(self):
        body, key, entered = self.body(), str(uuid4()), threading.Event()
        def request():
            entered.set()
            return self.post(body,key)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                future = pool.submit(request)
                self.assertTrue(entered.wait(15))
                OrganizationMember.objects.filter(user=self.manager,organization=self.org).update(deleted_at=timezone.now())
            response = future.result(25)
        self.assertIn(response.status_code,(401,403,404))
        self.assertEqual(NerBatch.objects.count(),0)

    def test_new_revision_committed_first_rejects_preview_and_original_key_is_unused(self):
        body, key, entered = self.body(), str(uuid4()), threading.Event()
        def request():
            entered.set()
            return self.post(body,key)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                future = pool.submit(request)
                self.assertTrue(entered.wait(15))
                newer = self.make_source(assignment=self.first.assignment,revision=2)
                self.write(newer,[])
            response = future.result(25)
        self.assertEqual(response.status_code,409,response.data)
        self.assertEqual(NerBatch.objects.count(),0)
        response = self.post(self.body(),key)
        self.assertEqual(response.status_code,201,response.data)

    def interleave_read(self, change, expected, *, stage='render'):
        created,_,_ = self.create()
        url = f'{self.base}{created.data["id"]}/items/{self.task_id}/'
        entered, release = threading.Event(), threading.Event()
        render, iterate = Response.render, QuerySet.iterator
        def rendering(response):
            value = render(response)
            if stage=='render' and response.status_code==200 and not entered.is_set():
                entered.set()
                assert release.wait(20)
            return value
        def iterating(query,*args,**kwargs):
            if stage=='hydrate' and query.model is Submission and not entered.is_set():
                entered.set()
                assert release.wait(20)
            return iterate(query,*args,**kwargs)
        def request():
            close_old_connections()
            try:
                client=APIClient()
                client.force_authenticate(self.manager)
                return client.get(url)
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(Response,'render',rendering), patch.object(QuerySet,'iterator',iterating):
            future=pool.submit(request)
            try:
                self.assertTrue(entered.wait(20))
                change()
            finally:
                release.set()
            response=future.result(25)
        self.assertIn(response.status_code,expected,response.data)
        if response.status_code!=200:
            self.assertNotIn('Alice',response.content.decode())
        return response

    def test_read_revocation_after_render_discards_answer(self):
        # Project creators intentionally keep manager semantics after a role-row
        # edit. Revoke real organization authority, not that ineffective change.
        self.interleave_read(lambda:OrganizationMember.objects.filter(user=self.manager,organization=self.org).update(
            deleted_at=timezone.now()),(401,403,404))

    @override_settings(ANNOTATION_PROJECT_OPERATION_PROVIDER='classification_disagreements.tests.test_ner_batch_concurrency.ReadPolicy')
    def test_independent_read_operation_withdrawal_after_render_discards_answer(self):
        self.interleave_read(lambda:Project.objects.filter(pk=self.project.pk).update(description='deny-read'),(403,))

    def test_read_source_deletion_after_render_discards_answer(self):
        self.interleave_read(lambda:Submission.objects.filter(pk=self.first.pk).delete(),(409,))

    def test_read_source_corruption_after_render_discards_answer(self):
        self.interleave_read(lambda:Submission.objects.filter(pk=self.first.pk).update(result_hash='0'*64),(409,))

    def test_new_revision_during_read_never_replaces_frozen_selection(self):
        def append():
            row=self.make_source(assignment=self.first.assignment,revision=2)
            self.write(row,[])
            Submission.objects.filter(pk=self.first.pk).update(status=Submission.Status.SUPERSEDED)
        response=self.interleave_read(append,(200,))
        self.assertEqual([p['submission_id'] for p in response.data['item']['inputs']],[self.first.pk,self.second.pk])

    def test_sql_growth_after_preflight_is_rejected_before_json_decode_and_scoring(self):
        # Creation must finish before installing the read-only boundary trap.
        created,_,_=self.create()
        entered,release=threading.Event(),threading.Event()
        original=QuerySet.iterator
        def iterate(query,*args,**kwargs):
            if query.model is Submission and not entered.is_set():
                entered.set()
                assert release.wait(20)
            return original(query,*args,**kwargs)
        def request():
            close_old_connections()
            try:
                client=APIClient()
                client.force_authenticate(self.manager)
                return client.get(f'{self.base}{created.data["id"]}/items/{self.task_id}/')
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(QuerySet,'iterator',iterate), patch.object(
                ner,'MAX_STORED_BYTES',8192), patch.object(ner,'Evaluator') as scorer:
            future=pool.submit(request)
            try:
                self.assertTrue(entered.wait(20))
                value=copy.deepcopy(self.first.result_snapshot)
                value['padding']='x'*8192
                Submission.objects.filter(pk=self.first.pk).update(result_snapshot=value,result_hash=_snapshot_hash(value))
            finally:
                release.set()
            response=future.result(25)
        self.assertEqual(response.status_code,422,response.data)
        scorer.assert_not_called()
        self.assertNotIn('Alice',response.content.decode())
