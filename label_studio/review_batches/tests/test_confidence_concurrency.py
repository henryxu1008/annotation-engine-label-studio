"""Real independent PostgreSQL connections, barriers and observable lock waits."""
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.models import JSONField
from django.db.models.query import QuerySet
from django.test import TransactionTestCase
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient
from review_batches import confidence_contract as c
from review_batches import confidence_observations as o
from review_batches import confidence_priority as p
from review_batches.models import ReviewBatch
from review_batches.models import ReviewConfidenceObservation as Fact
from tasks.models import Submission

from .confidence_fixtures import ConfidenceFixtures


class TestConfidenceConcurrency(ConfidenceFixtures, TransactionTestCase):
    client_class = APIClient

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Independent PostgreSQL connections required')
        ConfidenceFixtures.setUpTestData.__func__(type(self))
        self.confidence_setup()
        self.source = self.make_source()
        self.fact_body_ready = self.fact_body(self.source)
        preview = self.preview()
        assert preview.status_code == 200, preview.data
        self.batch_body = {**self.args, 'expected_plan_hash': preview.data['plan_hash']}

    def run_request(self, producer=False, *, body=None, key=None, attempted=None, pid=None):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_backend_pid()')
                if pid is not None:
                    pid.append(cursor.fetchone()[0])
            client = APIClient()
            client.force_authenticate(self.producer if producer else self.manager)
            if attempted is not None:
                attempted.set()
            return client.post(
                self.fact_base if producer else self.base,
                body or (self.fact_body_ready if producer else self.batch_body),
                format='json',
                HTTP_IDEMPOTENCY_KEY=str(key or uuid4()),
            )
        finally:
            connection.close()

    def blocked(self, pid):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            with connection.cursor() as cursor:
                cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', [pid])
                row = cursor.fetchone()
            if row and row[0] == 'Lock':
                return True
            time.sleep(0.02)
        return False

    def ordering(self, first_producer):
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        first_module = o if first_producer else p
        original = first_module.lock_project_operation
        calls = []

        def hold(*args, **kwargs):
            result = original(*args, **kwargs)
            if not calls:
                calls.append(1)
                entered.set()
                assert release.wait(15)
            return result

        pid = []
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(first_module, 'lock_project_operation', hold):
            first = pool.submit(self.run_request, first_producer)
            try:
                assert entered.wait(15)
                second = pool.submit(self.run_request, not first_producer, attempted=attempted, pid=pid)
                assert attempted.wait(15) and self.blocked(pid[0])
            finally:
                release.set()
            return first.result(30), second.result(30)

    def test_absent_fact_ingest_first_invalidates_waiting_expected_plan(self):
        produced, created = self.ordering(True)
        assert produced.status_code == 201, produced.data
        assert created.status_code == 409, created.data
        assert Fact.objects.count() == 1 and not ReviewBatch.objects.exists()

    def test_absent_fact_batch_first_freezes_unknown_before_waiting_ingest(self):
        created, produced = self.ordering(False)
        assert created.status_code == produced.status_code == 201, (created.data, produced.data)
        batch = ReviewBatch.objects.get()
        assert batch.plan['priority']['evidence'][0]['state'] == 'unknown'
        current = self.preview()
        assert current.data['plan']['priority']['evidence'][0]['state'] == 'low_confidence'
        batch.refresh_from_db()
        assert batch.plan_hash == created.data['plan_hash']

    def test_same_event_two_connections_has_one_fact_and_original_receipt(self):
        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                f.result(30)
                for f in [pool.submit(self.run_request, True, key=key), pool.submit(self.run_request, True, key=key)]
            ]
        assert sorted(r.status_code for r in results) == [200, 201], [r.data for r in results]
        assert results[0].data['observation_id'] == results[1].data['observation_id'] and Fact.objects.count() == 1

    def test_different_events_scores_compete_for_one_subject(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                f.result(30)
                for f in [
                    pool.submit(self.run_request, True, body={**self.fact_body_ready, 'score_units': score})
                    for score in (1, 2)
                ]
            ]
        assert sorted(r.status_code for r in results) == [201, 409], [r.data for r in results]
        assert Fact.objects.count() == 1
        assert Fact.objects.get().proof['score_units'] == (1 if results[0].status_code == 201 else 2)

    def test_same_batch_key_waits_and_recovers_without_materialization(self):
        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(p, 'materialize', wraps=p.materialize) as passes:
            results = [
                f.result(30) for f in [pool.submit(self.run_request, key=key), pool.submit(self.run_request, key=key)]
            ]
        assert sorted(r.status_code for r in results) == [200, 201], [r.data for r in results]
        assert results[0].data['id'] == results[1].data['id'] and ReviewBatch.objects.count() == 1
        assert passes.call_count == 3

    def test_same_batch_key_different_body_one_winner(self):
        key = uuid4()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                f.result(30)
                for f in [
                    pool.submit(self.run_request, key=key, body={**self.batch_body, 'name': name})
                    for name in ('one', 'two')
                ]
            ]
        assert sorted(r.status_code for r in results) == [201, 409], [r.data for r in results]
        assert ReviewBatch.objects.count() == 1

    def test_revocation_first_rejects_waiting_producer_and_batch(self):
        for producer in (True, False):
            attempted, pid = threading.Event(), []
            actor = self.producer if producer else self.manager
            with ThreadPoolExecutor(max_workers=1) as pool:
                with transaction.atomic():
                    Project.objects.select_for_update().get(pk=self.project.pk)
                    job = pool.submit(self.run_request, producer, attempted=attempted, pid=pid)
                    assert attempted.wait(15) and self.blocked(pid[0])
                    OrganizationMember.objects.filter(organization=self.org, user=actor).update(
                        deleted_at=timezone.now()
                    )
                result = job.result(30)
            assert result.status_code in (403, 404), result.data
        assert not Fact.objects.exists() and not ReviewBatch.objects.exists()

    def test_new_revision_first_rejects_waiting_stale_ingest_and_preview(self):
        attempted, pid = threading.Event(), []
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                job = pool.submit(self.run_request, True, attempted=attempted, pid=pid)
                assert attempted.wait(15) and self.blocked(pid[0])
                self.make_source(revision=2, assignment=self.source.assignment)
            assert job.result(30).status_code == 409
        assert self.run_request().status_code == 409

    def test_project_fence_is_held_during_render_for_both_write_paths(self):
        original, observed = JSONRenderer.render, []

        def probe():
            close_old_connections()
            try:
                try:
                    with transaction.atomic():
                        Project.objects.select_for_update(nowait=True).get(pk=self.project.pk)
                    return False
                except DatabaseError as exc:
                    assert getattr(exc.__cause__, 'sqlstate', None) == '55P03'
                    return True
            finally:
                connection.close()

        def rendering(renderer, data, *args, **kwargs):
            if (
                isinstance(data, dict)
                and data.get('replayed') is False
                and data.get('schema') in (c.SCHEMA, c.RECEIPT)
            ):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    observed.append(pool.submit(probe).result(10))
            return original(renderer, data, *args, **kwargs)

        with patch.object(JSONRenderer, 'render', rendering):
            assert self.run_request().status_code == 201
            assert self.run_request(True).status_code == 201
        assert observed == [True, True]

    def test_snapshot_growth_after_preflight_gated_in_actual_hydrating_select(self):
        ready, release = threading.Event(), threading.Event()
        iterator, decoder = QuerySet.iterator, JSONField.from_db_value
        intercepted = []

        def wait(query, *args, **kwargs):
            if query.model is Submission and 'ner_snapshot' in query.query.annotations and not intercepted:
                intercepted.append(1)
                ready.set()
                assert release.wait(15)
            return iterator(query, *args, **kwargs)

        def decode(field, value, expression, db):
            assert 'confidence-concurrent-growth' not in str(value)
            return decoder(field, value, expression, db)

        def preview():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.manager)
                return client.post(self.base + 'preview/', self.args, format='json')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(QuerySet, 'iterator', wait), patch.object(
            JSONField, 'from_db_value', decode
        ):
            job = pool.submit(preview)
            try:
                assert ready.wait(15)
                snapshot = copy.deepcopy(self.source.result_snapshot)
                snapshot['confidence-concurrent-growth'] = 'x' * (c.MAX_SINGLE_STORED + 1)
                Submission.objects.filter(pk=self.source.pk).update(result_snapshot=snapshot)
            finally:
                release.set()
            result = job.result(30)
        assert result.status_code == 422, result.data

    def test_metadata_union_growth_after_inspection_never_reaches_json_decoder(self):
        from rest_framework.exceptions import APIException

        from .confidence_fixtures import INSTANCE, NAMESPACE, PROFILE

        ready, release = threading.Event(), threading.Event()
        iterator, decoder = QuerySet.iterator, JSONField.from_db_value
        intercepted = []

        def wait(query, *args, **kwargs):
            if query.model is Fact and '_meta_value_proof' in query.query.annotations and not intercepted:
                intercepted.append(1)
                ready.set()
                assert release.wait(15)
            return iterator(query, *args, **kwargs)

        def decode(field, value, expression, db):
            assert 'concurrent-metadata-union' not in str(value)
            return decoder(field, value, expression, db)

        def read():
            close_old_connections()
            try:
                try:
                    c.metadata_budget().rows(Fact.objects.filter(project=self.project), o.FIELDS, o.LIMITS)
                    return None
                except APIException as exc:
                    return exc.status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(QuerySet, 'iterator', wait), patch.object(
            JSONField, 'from_db_value', decode
        ):
            job = pool.submit(read)
            try:
                assert ready.wait(15)
                proof = {'concurrent-metadata-union': 'x' * (31 * 1024)}
                Fact.objects.bulk_create(
                    [
                        Fact(
                            project=self.project,
                            instance_id=INSTANCE,
                            producer_namespace=NAMESPACE,
                            profile_id=PROFILE,
                            profile_digest=self.profile_digest,
                            event_uuid=uuid4(),
                            submission_id_frozen=10000 + i,
                            actor_id_frozen=self.producer.pk,
                            creator_principal='synthetic',
                            creator_source='synthetic',
                            request_hash='a' * 64,
                            proof=proof,
                            receipt=proof,
                            fact_hash='b' * 64,
                            receipt_hash='c' * 64,
                        )
                        for i in range(270)
                    ]
                )
            finally:
                release.set()
            assert job.result(30) == 422

    def test_ordinary_review_competes_with_ingest_without_reverse_locks(self):
        def review():
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.reviewer)
                return client.post(
                    f'/api/submissions/{self.source.pk}/review/', {'decision': 'approved'}, format='json'
                )
            finally:
                connection.close()

        for i in range(4):
            if i:
                self.source = self.make_source()
                self.fact_body_ready = self.fact_body(self.source)
            with ThreadPoolExecutor(max_workers=2) as pool:
                produced, reviewed = [f.result(30) for f in [pool.submit(self.run_request, True), pool.submit(review)]]
            assert produced.status_code in (201, 409), produced.data
            assert reviewed.status_code == 200, reviewed.data
