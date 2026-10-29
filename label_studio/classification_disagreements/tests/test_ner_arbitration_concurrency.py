import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from classification_disagreements import ner
from classification_disagreements import ner_arbitration as a
from classification_disagreements.models import NerArbitrationDecision as ArbitrationDecision
from classification_disagreements.models import NerArbitrationTask as ArbitrationTask
from classification_disagreements.tests import test_ner_arbitration as fixtures
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import Project, ProjectMember
from rest_framework.response import Response
from rest_framework.test import APIClient
from tasks.models import Submission


@override_settings(
    ANNOTATION_NER_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_NER_BATCHES_ENABLED=True,
    ANNOTATION_NER_ARBITRATION_ENABLED=True,
)
class TestNerArbitrationConcurrency(TransactionTestCase):
    client_class = APIClient
    make_source = fixtures.TestNerArbitration.make_source
    write = fixtures.TestNerArbitration.write
    preview = fixtures.TestNerArbitration.preview
    create = fixtures.TestNerArbitration.create
    dispatch_body = fixtures.TestNerArbitration.dispatch_body
    dispatch = fixtures.TestNerArbitration.dispatch

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Independent PostgreSQL connections required')
        fixtures.TestNerArbitration.setUpTestData.__func__(self)
        fixtures.TestNerArbitration.setUp(self)
        self.arbiter = self.independent

    def pending(self):
        return ArbitrationTask.objects.get(pk=self.dispatch()[0].data['id'])

    def command_body(self, row, cancel=False):
        body = {
            'expected_version': row.version,
            'expected_input_set_hash': row.input_set_hash,
            'reason': 'Frozen NER reason',
        }
        if not cancel:
            body['winner_submission_id'] = self.first.pk
        return body

    def client_for(self, actor):
        client = APIClient()
        client.force_authenticate(actor)
        client.handler.load_middleware()
        return client

    def post(self, client, path, body, key, attempted=None):
        close_old_connections()
        try:

            def observe(execute, sql, params, many, context):
                if attempted is not None and 'FOR UPDATE' in sql and '"project"' in sql:
                    attempted.set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe):
                return client.post(path, body, format='json', HTTP_IDEMPOTENCY_KEY=str(key))
        finally:
            connection.close()

    def competing(
        self,
        first_actor,
        second_actor,
        first_path,
        second_path,
        first_body,
        second_body,
        first_key,
        second_key,
        *,
        cancel_first=False,
    ):
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        evidence, render = a.evidence, Response.render

        def hold(*args, **kwargs):
            result = evidence(*args, **kwargs)
            if kwargs.get('lock'):
                entered.set()
                assert release.wait(20)
            return result

        def hold_render(response):
            result = render(response)
            if response.status_code == 201 and response.data.get('receipt', {}).get('operation') == 'cancel':
                entered.set()
                assert release.wait(20)
            return result

        clients = [self.client_for(first_actor), self.client_for(second_actor)]
        with (
            ThreadPoolExecutor(max_workers=2) as pool,
            patch.object(a, 'evidence', evidence if cancel_first else hold),
            patch.object(Response, 'render', hold_render if cancel_first else render),
        ):
            first = pool.submit(self.post, clients[0], first_path, first_body, first_key)
            try:
                assert entered.wait(20)
                second = pool.submit(self.post, clients[1], second_path, second_body, second_key, attempted)
                assert attempted.wait(20)
            finally:
                release.set()
            return first.result(25), second.result(25)

    def test_same_create_key_recovers_one_task(self):
        value, key = self.dispatch_body(), uuid4()
        responses = self.competing(self.manager, self.manager, self.arb_base, self.arb_base, value, value, key, key)
        assert [r.status_code for r in responses] == [201, 200]
        assert responses[0].data['id'] == responses[1].data['id']
        assert ArbitrationTask.objects.count() == 1

    def test_different_create_keys_same_input_have_one_active_task(self):
        value = self.dispatch_body()
        responses = self.competing(
            self.manager, self.manager, self.arb_base, self.arb_base, value, value, uuid4(), uuid4()
        )
        assert [r.status_code for r in responses] == [201, 409]
        assert ArbitrationTask.objects.count() == 1

    def test_same_adoption_key_recovers_one_decision(self):
        row, key = self.pending(), uuid4()
        path, value = f'{self.arb_base}{row.pk}/decision/', self.command_body(row)
        responses = self.competing(self.arbiter, self.arbiter, path, path, value, value, key, key)
        assert [r.status_code for r in responses] == [201, 200]
        assert responses[0].data['decision_id'] == responses[1].data['decision_id']
        assert ArbitrationDecision.objects.count() == 1

    def test_competing_winners_cannot_overwrite_first(self):
        row = self.pending()
        path, value = f'{self.arb_base}{row.pk}/decision/', self.command_body(row)
        changed = {**value, 'winner_submission_id': self.second.pk}
        responses = self.competing(self.arbiter, self.arbiter, path, path, value, changed, uuid4(), uuid4())
        assert [r.status_code for r in responses] == [201, 409]
        assert ArbitrationDecision.objects.get().winner['submission_id'] == self.first.pk

    def test_same_cancellation_key_recovers_one_terminal_receipt(self):
        row, key = self.pending(), uuid4()
        path, value = f'{self.arb_base}{row.pk}/cancel/', self.command_body(row, cancel=True)
        responses = self.competing(self.manager, self.manager, path, path, value, value, key, key, cancel_first=True)
        assert [r.status_code for r in responses] == [201, 200]
        assert responses[0].data['receipt'] == responses[1].data['receipt']
        row.refresh_from_db()
        assert row.status == 'cancelled' and row.version == 2
        assert not ArbitrationDecision.objects.exists()

    def test_different_cancellation_keys_cannot_replace_the_first_reason(self):
        row = self.pending()
        path, value = f'{self.arb_base}{row.pk}/cancel/', self.command_body(row, cancel=True)
        responses = self.competing(
            self.manager,
            self.manager,
            path,
            path,
            value,
            {**value, 'reason': 'Second cancellation'},
            uuid4(),
            uuid4(),
            cancel_first=True,
        )
        assert [r.status_code for r in responses] == [201, 409]
        row.refresh_from_db()
        assert row.cancellation['reason'] == value['reason']

    def test_adoption_wins_then_cancel_conflicts(self):
        row = self.pending()
        responses = self.competing(
            self.arbiter,
            self.manager,
            f'{self.arb_base}{row.pk}/decision/',
            f'{self.arb_base}{row.pk}/cancel/',
            self.command_body(row),
            self.command_body(row, cancel=True),
            uuid4(),
            uuid4(),
        )
        assert [r.status_code for r in responses] == [201, 409]
        row.refresh_from_db()
        assert row.status == 'resolved' and ArbitrationDecision.objects.count() == 1

    def test_cancel_wins_then_old_adoption_cannot_commit(self):
        row = self.pending()
        responses = self.competing(
            self.manager,
            self.arbiter,
            f'{self.arb_base}{row.pk}/cancel/',
            f'{self.arb_base}{row.pk}/decision/',
            self.command_body(row, cancel=True),
            self.command_body(row),
            uuid4(),
            uuid4(),
            cancel_first=True,
        )
        assert [r.status_code for r in responses] == [201, 409]
        assert not ArbitrationDecision.objects.exists()
        row.refresh_from_db()
        assert row.status == 'cancelled' and row.version == 2

    def test_revocation_while_original_key_recovery_waits(self):
        row, key = self.pending(), uuid4()
        path, value = f'{self.arb_base}{row.pk}/decision/', self.command_body(row)
        first = self.post(self.client_for(self.arbiter), path, value, key)
        assert first.status_code == 201
        attempted = threading.Event()
        client = self.client_for(self.arbiter)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                Project.objects.select_for_update().get(pk=self.project.pk)
                future = pool.submit(self.post, client, path, value, key, attempted)
                assert attempted.wait(20)
                OrganizationMember.objects.filter(organization=self.org, user=self.arbiter).update(
                    deleted_at=timezone.now()
                )
            result = future.result(25)
        assert result.status_code in (403, 404) and 'decision_id' not in result.data
        assert ArbitrationDecision.objects.count() == 1

    def test_project_fence_held_until_adoption_render_finishes(self):
        row = self.pending()
        entered, release = threading.Event(), threading.Event()
        original = Response.render

        def hold(response):
            result = original(response)
            if response.status_code == 201:
                entered.set()
                assert release.wait(20)
            return result

        client = self.client_for(self.arbiter)
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(Response, 'render', hold):
            future = pool.submit(
                self.post, client, f'{self.arb_base}{row.pk}/decision/', self.command_body(row), uuid4()
            )
            try:
                assert entered.wait(20)
                with self.assertRaises(DatabaseError), transaction.atomic():
                    Project.objects.select_for_update(nowait=True).get(pk=self.project.pk)
            finally:
                release.set()
            assert future.result(25).status_code == 201

    def test_source_change_after_read_materialization_discards_answers(self):
        row = self.pending()
        entered, release = threading.Event(), threading.Event()
        original = a.evidence

        def hold(*args, **kwargs):
            result = original(*args, **kwargs)
            if not entered.is_set():
                entered.set()
                assert release.wait(20)
            return result

        client = self.client_for(self.arbiter)

        def read():
            close_old_connections()
            try:
                return client.get(f'{self.arb_base}{row.pk}/')
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(a, 'evidence', hold):
            future = pool.submit(read)
            try:
                assert entered.wait(20)
                Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
            finally:
                release.set()
            result = future.result(25)
        assert result.status_code == 409 and 'report' not in result.data

    def read_race(self, *, last_counter=False, revoke=False):
        row = self.pending()
        entered, release = threading.Event(), threading.Event()
        scorer, render = ner.check_engine, Response.render
        counters = []

        def hold():
            entered.set()
            assert release.wait(20)

        def counter(value):
            scorer(value)
            counters.append(1)
            if len(counters) == 2:
                hold()

        def rendered(response):
            value = render(response)
            if response.status_code == 200 and response.data.get('comparison'):
                hold()
            return value

        client = self.client_for(self.arbiter)

        def read():
            close_old_connections()
            try:
                return client.get(f'{self.arb_base}{row.pk}/')
            finally:
                connection.close()

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch.object(ner, 'check_engine', counter if last_counter else scorer),
            patch.object(Response, 'render', render if last_counter else rendered),
        ):
            future = pool.submit(read)
            try:
                assert entered.wait(20)
                if revoke:
                    ProjectMember.objects.filter(project=self.project, user=self.arbiter).update(enabled=False)
                else:
                    Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
            finally:
                release.set()
            value = future.result(25)
        assert value.status_code in (403, 404, 409) and 'comparison' not in value.data
        assert b'Alice' not in value.content

    def test_independent_source_writer_at_last_counter_discards_rendered_answers(self):
        self.read_race(last_counter=True)

    def test_independent_revocation_during_render_discards_answers(self):
        self.read_race(revoke=True)
