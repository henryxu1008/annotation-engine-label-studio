"""Independent PostgreSQL connections for NER ReviewBatch planning."""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from classification_disagreements import fields
from django.db import close_old_connections, connection, transaction
from django.db.models import JSONField
from django.db.models.query import QuerySet
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient
from review_batches import field_priority as p
from review_batches.models import ReviewBatch
from review_batches.tests import test_concurrency as existing
from review_batches.tests import test_field_priority as fixtures
from tasks.models import Submission


@override_settings(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_FIELD_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED=True,
)
class TestFieldPriorityConcurrency(TransactionTestCase):
    client_class = APIClient
    make_source = fixtures.TestFieldPriority.make_source
    write = fixtures.TestFieldPriority.write
    freeze_analysis = fixtures.TestFieldPriority.freeze_analysis
    preview = fixtures.TestFieldPriority.preview
    body = fixtures.TestFieldPriority.body
    run_request = existing.TestBatchConcurrency.run_request

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Independent PostgreSQL connections required')
        fixtures.TestFieldPriority.setUpTestData.__func__(type(self))
        fixtures.TestFieldPriority.setUp(self)
        self.payload = self.body()

    def test_waiting_same_key_recovers_one_plan_with_no_rescore(self):
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        original = p.materialize
        calls = []

        def hold(*args, **kwargs):
            calls.append(1)
            entered.set()
            assert release.wait(15)
            return original(*args, **kwargs)

        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(p, 'materialize', side_effect=hold):
            first = pool.submit(self.run_request, self.manager, self.base, self.payload, key=key)
            try:
                assert entered.wait(15)
                second = pool.submit(
                    self.run_request, self.manager, self.base, self.payload, key=key, attempted=attempted
                )
                assert attempted.wait(15)
            finally:
                release.set()
            results = [first.result(30), second.result(30)]
        assert sorted(r.status_code for r in results) == [200, 201]
        assert results[0].data['id'] == results[1].data['id']
        assert ReviewBatch.objects.count() == 1
        assert len(calls) == 2  # Original initial/final reads; recovery has neither.

    def test_same_key_different_body_has_one_winner(self):
        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [
                pool.submit(self.run_request, self.manager, self.base, {**self.payload, 'name': name}, key=key)
                for name in ('first', 'second')
            ]
            results = [job.result(30) for job in jobs]
        assert sorted(r.status_code for r in results) == [201, 409]
        assert ReviewBatch.objects.count() == 1

    def test_revocation_first_denies_waiting_create(self):
        attempted = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                pending = pool.submit(
                    self.run_request, self.manager, self.base, self.payload, key=uuid4(), attempted=attempted
                )
                assert attempted.wait(15)
                OrganizationMember.objects.filter(organization=self.org, user=self.manager).update(
                    deleted_at=timezone.now()
                )
            result = pending.result(30)
        assert result.status_code in (403, 404), result.data
        assert not ReviewBatch.objects.exists()

    def test_new_revision_first_denies_waiting_stale_preview(self):
        attempted = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                pending = pool.submit(
                    self.run_request, self.manager, self.base, self.payload, key=uuid4(), attempted=attempted
                )
                assert attempted.wait(15)
                newer = self.make_source(assignment=self.first.assignment, revision=2)
                self.write(newer)
            result = pending.result(30)
        assert result.status_code == 409, result.data
        assert not ReviewBatch.objects.exists()

    def test_post_render_fence_blocks_an_independent_nowait_writer(self):
        observed = []
        render = JSONRenderer.render

        def probe():
            close_old_connections()
            try:
                try:
                    with transaction.atomic():
                        Project.objects.select_for_update(nowait=True).get(pk=self.project.pk)
                    return False
                except Exception as exc:
                    from django.db import DatabaseError

                    assert isinstance(exc, DatabaseError)
                    return True
            finally:
                connection.close()

        def rendering(renderer, data, *args, **kwargs):
            if isinstance(data, dict) and data.get('replayed') is False:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    observed.append(pool.submit(probe).result(10))
            return render(renderer, data, *args, **kwargs)

        with patch.object(JSONRenderer, 'render', rendering):
            result = self.client.post(self.base, self.payload, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        assert result.status_code == 201, result.data
        assert observed == [True]

    def test_snapshot_growth_after_sql_preflight_never_reaches_json_decoder(self):
        ready, release = threading.Event(), threading.Event()
        iterator = QuerySet.iterator
        decoder = JSONField.from_db_value
        entered = []

        def wait_for_growth(query, *args, **kwargs):
            if query.model is Submission and 'ner_snapshot' in query.query.annotations and not entered:
                entered.append(1)
                ready.set()
                assert release.wait(15)
            return iterator(query, *args, **kwargs)

        def decoding(field, value, expression, db):
            if isinstance(value, str) and 'concurrent-budget-marker' in value:
                raise AssertionError('Concurrent overflow reached the decoder')
            return decoder(field, value, expression, db)

        def read():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.manager)
                return client.post(self.base + 'preview/', self.args, format='json')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(
            QuerySet, 'iterator', wait_for_growth
        ), patch.object(JSONField, 'from_db_value', decoding), patch.object(
            fields, 'MAX_STORED_BYTES', 1024 * 1024
        ), patch.object(
            fields, 'normalize', wraps=fields.normalize
        ) as engine:
            pending = pool.submit(read)
            try:
                assert ready.wait(15)
                value = dict(self.first.result_snapshot)
                value['synthetic_padding'] = 'concurrent-budget-marker' * 100000
                Submission.objects.filter(pk=self.first.pk).update(result_snapshot=value)
            finally:
                release.set()
            result = pending.result(30)
        assert result.status_code == 422, result.data
        assert engine.call_count == 0
        assert not ReviewBatch.objects.exists()

    def test_independent_mutation_during_last_equality_discards_preview(self):
        equal, calls = fields.equal, []

        def mutate():
            close_old_connections()
            try:
                Submission.objects.filter(pk=self.first.pk).update(result_hash='f' * 64)
            finally:
                connection.close()

        def compare(left, right):
            calls.append(1)
            if len(calls) == 12:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(mutate).result(15)
            return equal(left, right)

        with patch.object(fields, 'equal', compare):
            result = self.preview()
        assert result.status_code == 409, result.data
        assert len(calls) == 12 and not ReviewBatch.objects.exists()
