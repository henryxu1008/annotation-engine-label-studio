import copy
import json
from unittest.mock import patch
from uuid import uuid4

from access_control.identity import Principal, get_identity_provider
from access_control.project_operations import ProjectOperationPolicy
from classification_disagreements import arbitration as a
from classification_disagreements import batches
from classification_disagreements.models import ArbitrationDecision, ArbitrationTask
from classification_disagreements.tests import test_batches as fixtures
from django.db import DatabaseError, transaction
from django.test import override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from projects.models import ProjectMember
from rest_framework.response import Response
from rest_framework.test import APITestCase
from tasks.models import ReviewDecision, Submission, TaskAssignment
from tasks.submissions import _snapshot_hash
from users.tests.factories import UserFactory


class NoReadPolicy:
    def evaluate(self, project):
        return ProjectOperationPolicy('arbitration-test/v1', 'paused', frozenset())


@override_settings(
    ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
    ANNOTATION_CLASSIFICATION_ARBITRATION_ENABLED=True,
)
class TestArbitration(APITestCase):
    setUpTestData = classmethod(fixtures.TestClassificationBatches.setUpTestData.__func__)
    make_source = fixtures.TestClassificationBatches.make_source
    set_answer = fixtures.TestClassificationBatches.set_answer
    preview = fixtures.TestClassificationBatches.preview
    body = fixtures.TestClassificationBatches.body
    create = fixtures.TestClassificationBatches.create
    new_batch = fixtures.TestClassificationBatches.new_batch

    def setUp(self):
        fixtures.TestClassificationBatches.setUp(self)
        self.set_answer(self.second, 'Negative')
        self.arbiter = UserFactory(active_organization=self.org)
        self.other_arbiter = UserFactory(active_organization=self.org)
        for user in (self.arbiter, self.other_arbiter):
            OrganizationMember.objects.get_or_create(organization=self.org, user=user)
            ProjectMember.objects.create(project=self.project, user=user, role='reviewer')
        self.arb_mode = self.settings(
            ANNOTATION_CLASSIFICATION_ARBITRATION_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'}
        )
        self.arb_mode.enable()
        self.addCleanup(self.arb_mode.disable)
        self.arb_base = f'/api/projects/{self.project.pk}/classification-arbitrations/'

    def dispatch_body(self, batch=None, target=None):
        batch = batch or self.new_batch()
        return {
            'batch_id': batch.pk,
            'task_id': self.first.assignment.task_id,
            'expected_manifest_hash': batch.manifest_hash,
            'assignee_id': (target or self.arbiter).pk,
        }

    def dispatch(self, value=None, key=None):
        return self.client.post(
            self.arb_base, value or self.dispatch_body(), format='json', HTTP_IDEMPOTENCY_KEY=str(key or uuid4())
        )

    def pending(self):
        response = self.dispatch()
        assert response.status_code == 201, response.data
        return ArbitrationTask.objects.get(pk=response.data['id'])

    def command_body(self, row, *, cancel=False):
        return {
            'expected_version': row.version,
            'expected_input_set_hash': row.input_set_hash,
            'reason': 'Synthetic 合成依据',
            **({} if cancel else {'winner_submission_id': row.origin['item']['cohort'][0]['source']['submission_id']}),
        }

    def command(self, row, value=None, key=None, *, cancel=False):
        action = 'cancel' if cancel else 'decision'
        return self.client.post(
            f'{self.arb_base}{row.pk}/{action}/',
            value or self.command_body(row, cancel=cancel),
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(key or uuid4()),
        )

    def get_detail(self, row):
        return self.client.get(f'{self.arb_base}{row.pk}/')

    def test_dispatch_adopt_existing_revision_without_modifying_review_or_submissions(self):
        before = list(Submission.objects.order_by('pk').values())
        row = self.pending()
        self.client.force_authenticate(self.arbiter)
        detail = self.get_detail(row)
        assert detail.status_code == 200 and detail.data['available'] is True
        assert detail.data['texts'] == [{'name': 'text', 'text': 'Synthetic 合成'}]
        assert 'origin_manifest_hash' not in str(detail.data)
        assert detail.data['can_decide'] is True and detail.data['can_cancel'] is False
        response = self.command(row)
        assert response.status_code == 201, response.data
        row.refresh_from_db()
        assert row.status == 'resolved' and row.version == 2
        decision = ArbitrationDecision.objects.get()
        assert decision.winner == row.origin['item']['cohort'][0]['source']
        assert self.get_detail(row).data['decision']['actor_id'] == self.arbiter.pk
        assert list(Submission.objects.order_by('pk').values()) == before
        assert not ReviewDecision.objects.exists()
        assert self.get_detail(row)['Cache-Control'] == 'private, no-store'

    def test_deduplication_ignores_batch_names_and_times_and_cancel_allows_new_dispatch(self):
        row = self.pending()
        self.args['name'] = 'Another batch 同一输入'
        other = self.new_batch()
        duplicate = self.dispatch(self.dispatch_body(other))
        assert duplicate.status_code == 409
        assert duplicate.data['display_context']['reason'] == 'ARBITRATION_DUPLICATE_ACTIVE'
        assert self.command(row, cancel=True).status_code == 201
        assert self.dispatch(self.dispatch_body(other, self.other_arbiter)).status_code == 201
        assert ArbitrationTask.objects.count() == 2
        assert ArbitrationTask.objects.filter(status='pending').count() == 1

    def test_only_assigned_independent_reviewer_can_read_and_decide(self):
        value = self.dispatch_body(target=self.reviewer)
        assert self.dispatch(value).status_code == 409  # A frozen participant cannot arbitrate.
        row = self.pending()
        assert self.command(row).status_code == 404  # Manager creation does not grant adoption.
        for actor in (self.worker, self.other_arbiter):
            self.client.force_authenticate(actor)
            assert self.get_detail(row).status_code in (403, 404)
            assert self.command(row).status_code in (403, 404)
            listing = self.client.get(self.arb_base)
            assert listing.status_code in (403, 404) or listing.data['count'] == 0
        self.client.force_authenticate(self.arbiter)
        assert self.command(row, cancel=True).status_code == 404
        listing = self.client.get(self.arb_base)
        assert listing.data['count'] == 1 and 'origin_manifest_hash' not in str(listing.data)
        assert (
            self.client.get(
                self.arb_base.replace(str(self.project.pk), str(self.other.pk), 1) + f'{row.pk}/'
            ).status_code
            == 404
        )

    def test_strict_fields_cas_input_set_and_foreign_winner(self):
        value = self.dispatch_body()
        for field in ('actor', 'reviewer', 'result', 'status', 'viewer_hash'):
            assert self.dispatch({**value, field: self.manager.pk}).status_code == 400
        row = self.pending()
        self.client.force_authenticate(self.arbiter)
        args = self.command_body(row)
        for changed in ({'expected_version': 2}, {'expected_input_set_hash': '0' * 64}):
            assert self.command(row, {**args, **changed}).status_code == 409
        foreign = self.make_source(project=self.other)
        assert self.command(row, {**args, 'winner_submission_id': foreign.pk}).status_code == 404
        for changed in (
            {'winner_submission_id': True},
            {'reason': ' '},
            {'reason': 'x' * (a.MAX_REASON + 1)},
            {'actor': self.manager.pk},
        ):
            assert self.command(row, {**args, **changed}).status_code == 400
        assert not ArbitrationDecision.objects.exists()

    def test_invalid_unicode_reason_is_rejected_without_writes(self):
        row = self.pending()
        for cancel in (False, True):
            self.client.force_authenticate(self.manager if cancel else self.arbiter)
            action = 'cancel' if cancel else 'decision'
            for reason in ('\ud800', '\udfff', '\x00'):
                value = {**self.command_body(row, cancel=cancel), 'reason': reason}
                # A real UTF-8 JSON body can carry an escaped unpaired surrogate.
                response = self.client.post(
                    f'{self.arb_base}{row.pk}/{action}/',
                    json.dumps(value, ensure_ascii=True),
                    content_type='application/json',
                    HTTP_IDEMPOTENCY_KEY=str(uuid4()),
                )
                assert response.status_code == 400
                assert response.data['display_context']['reason'] == 'ARBITRATION_INVALID_INPUT'
        row.refresh_from_db()
        assert row.status == 'pending' and row.version == 1 and row.cancellation is None
        assert not ArbitrationDecision.objects.exists()

    def test_all_original_keys_recover_after_state_advance_source_deletion_and_key_rotation(self):
        create_body, create_key = self.dispatch_body(), uuid4()
        created = self.dispatch(create_body, create_key)
        row = ArbitrationTask.objects.get(pk=created.data['id'])
        self.client.force_authenticate(self.arbiter)
        value, key = self.command_body(row), uuid4()
        decided = self.command(row, value, key)
        assert decided.status_code == 201
        self.first.delete()
        with self.settings(SECRET_KEY='rotated', SECRET_KEY_FALLBACKS=[]):
            recovered = self.command(row, value, key)
            assert recovered.status_code == 200 and recovered.data['replayed']
            assert recovered.data['decision_id'] == decided.data['decision_id']
            self.client.force_authenticate(self.manager)
            replay = self.dispatch(create_body, create_key)
            assert (
                replay.status_code == 200
                and replay.data['id'] == row.pk
                and replay.data['receipt']['version_at_commit'] == 1
            )
        assert ArbitrationTask.objects.count() == ArbitrationDecision.objects.count() == 1
        self.client.force_authenticate(self.arbiter)
        detail = self.get_detail(row)
        assert detail.data['available'] is False and detail.data['report'] is None and detail.data['texts'] == []
        assert detail.data['decision']['id'] == decided.data['decision_id']

    def test_cancel_has_original_receipt_without_read_or_available_source(self):
        row = self.pending()
        value, key = self.command_body(row, cancel=True), uuid4()
        self.first.delete()
        with self.settings(
            ANNOTATION_PROJECT_OPERATION_PROVIDER='classification_disagreements.tests.test_arbitration.NoReadPolicy'
        ):
            assert self.get_detail(row).status_code == 403
            assert self.command(row, value, key, cancel=True).status_code == 201
            replay = self.command(row, value, key, cancel=True)
            assert replay.status_code == 200 and replay.data['replayed']
            assert set(replay.data) == {'id', 'status', 'version', 'replayed', 'receipt'}
        self.client.force_authenticate(self.arbiter)
        assert self.command(row).status_code == 409
        assert not ArbitrationDecision.objects.exists()

    def test_key_binds_body_actor_and_action(self):
        value, key = self.dispatch_body(), uuid4()
        assert self.dispatch(value, key).status_code == 201
        assert self.dispatch({**value, 'assignee_id': self.other_arbiter.pk}, key).status_code == 409
        row = ArbitrationTask.objects.get()
        self.client.force_authenticate(self.arbiter)
        decision_body, decision_key = self.command_body(row), uuid4()
        assert self.command(row, decision_body, decision_key).status_code == 201
        assert self.command(row, {**decision_body, 'reason': 'Other'}, decision_key).status_code == 409
        self.client.force_authenticate(self.other_arbiter)
        assert self.command(row, decision_body, decision_key).status_code == 404

    def test_newer_revision_never_replaces_frozen_candidates(self):
        row = self.pending()
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.set_answer(newer, 'Negative')
        self.client.force_authenticate(self.arbiter)
        assert self.command(row).status_code == 201
        assert ArbitrationDecision.objects.get().winner['submission_id'] == self.first.pk

    def test_source_corruption_blocks_adoption_and_masks_answers(self):
        row = self.pending()
        Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
        self.client.force_authenticate(self.arbiter)
        detail = self.get_detail(row)
        assert detail.data['available'] is False and detail.data['report'] is None and detail.data['texts'] == []
        assert self.command(row).status_code == 409
        assert not ArbitrationDecision.objects.exists()

    def test_exact_utf8_text_budget_and_excess_no_partial_task(self):
        text = '界' * 21845 + 'x'
        assert len(text.encode()) == a.MAX_TEXT_BYTES
        for source in (self.first, self.second):
            self.set_answer(
                source,
                'Positive' if source.pk == self.first.pk else 'Negative',
                lambda s: s['task']['data'].update(text=text),
            )
        value = self.dispatch_body()
        assert self.dispatch(value).status_code == 201
        with patch.object(a, 'MAX_TEXT_BYTES', len(text.encode()) - 1):
            assert self.dispatch(value).status_code == 422
        assert ArbitrationTask.objects.count() == 1
        for source in (self.first, self.second):
            self.set_answer(
                source,
                'Positive' if source.pk == self.first.pk else 'Negative',
                lambda s: s['task']['data'].update(text=text + 'x'),
            )
        assert self.dispatch(self.dispatch_body()).status_code == 422
        assert ArbitrationTask.objects.count() == 1

    def test_exact_snapshot_and_participant_budgets(self):
        snapshot = copy.deepcopy(self.first.result_snapshot)
        snapshot['task']['meta'] = {'padding': ''}
        size = len(batches.canonical(snapshot)) + len(batches.canonical(self.second.result_snapshot))
        snapshot['task']['meta']['padding'] = 'x' * (a.MAX_SNAPSHOT_BYTES - size)
        Submission.objects.filter(pk=self.first.pk).update(
            result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
        )
        value = self.dispatch_body()
        assert self.dispatch(value).status_code == 201
        with patch.object(a, 'MAX_SNAPSHOT_BYTES', a.MAX_SNAPSHOT_BYTES - 1):
            assert self.dispatch(value).status_code == 422
        with patch.object(a, 'MAX_PARTICIPANTS', 1):
            assert self.dispatch(value).status_code == 422
        snapshot['task']['meta']['padding'] += 'x'
        Submission.objects.filter(pk=self.first.pk).update(
            result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
        )
        # The durable batch uses the same 8 MiB cap, so the real +1 byte
        # source set is rejected before it can become an arbitration origin.
        response = self.client.post(self.base + 'preview/', self.args, format='json')
        assert response.status_code == 422
        assert response.data['display_context']['reason'] == 'CLASSIFICATION_BATCH_TOO_LARGE'
        assert ArbitrationTask.objects.count() == 1

    def test_budget_blocks_new_adoption_but_keeps_original_receipts_and_cancellation(self):
        value, key = self.dispatch_body(), uuid4()
        created = self.dispatch(value, key)
        row = ArbitrationTask.objects.get(pk=created.data['id'])
        with patch.object(a, 'MAX_TEXT_BYTES', 1):
            assert self.dispatch(value, key).status_code == 200
            self.client.force_authenticate(self.arbiter)
            assert self.get_detail(row).data['available'] is False
            assert self.command(row).status_code == 422
            assert not ArbitrationDecision.objects.exists()
            self.client.force_authenticate(self.manager)
            assert self.command(row, cancel=True).status_code == 201
            assert self.dispatch(value, key).status_code == 200
        row.refresh_from_db()
        assert row.status == 'cancelled' and row.version == 2

    def test_exact_reason_boundary_and_whitespace_normalization(self):
        row = self.pending()
        self.client.force_authenticate(self.arbiter)
        body = {**self.command_body(row), 'reason': '  ' + '界' * a.MAX_REASON + '  '}
        assert self.command(row, body).status_code == 201
        assert ArbitrationDecision.objects.get().reason == '界' * a.MAX_REASON

    def test_mode_flags_and_operations_rechecked_on_all_routes(self):
        value = self.dispatch_body()
        row = self.pending()
        for setting in (
            {'ANNOTATION_CLASSIFICATION_ARBITRATION_ENABLED': False},
            {'ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED': False},
            {'ANNOTATION_CLASSIFICATION_BATCHES_ENABLED': False},
            {'ANNOTATION_CLASSIFICATION_ARBITRATION_MODES': {}},
            {'ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES': {}},
            {
                'ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES': {
                    f'{self.org.pk}:{self.project.pk}': 'core_only_internal'
                }
            },
            {
                'ANNOTATION_CLASSIFICATION_ARBITRATION_MODES': {
                    f'{self.org.pk}:{self.project.pk}': 'core_only_internal'
                }
            },
        ):
            with self.settings(**setting):
                for path in ('capability/', '', f'{row.pk}/'):
                    assert self.client.get(self.arb_base + path).status_code == 404
                assert (
                    self.client.get(
                        self.arb_base + 'assignees/',
                        {k: value[k] for k in ('batch_id', 'task_id', 'expected_manifest_hash')},
                    ).status_code
                    == 404
                )
                assert self.dispatch(value).status_code == 404
                assert self.command(row, cancel=True).status_code == 404
                self.client.force_authenticate(self.arbiter)
                assert self.command(row).status_code == 404
                self.client.force_authenticate(self.manager)
        with self.settings(ANNOTATION_PROJECT_OPERATION_PROVIDER='review_batches.tests.test_batches.ReadOnlyPolicy'):
            assert not self.client.get(self.arb_base + 'capability/').data['can_create']
            assert self.dispatch(value).status_code == 403

    def test_read_viewer_pin_binds_actor_principal_project_and_role(self):
        row = self.pending()
        manager_pin = self.client.get(self.arb_base + 'capability/').data['viewer_hash']
        assert self.client.get(self.arb_base).data['viewer_hash'] == manager_pin
        assert self.get_detail(row).data['viewer_hash'] == manager_pin
        value = self.dispatch_body()
        roster = self.client.get(
            self.arb_base + 'assignees/', {k: value[k] for k in ('batch_id', 'task_id', 'expected_manifest_hash')}
        )
        assert roster.data['viewer_hash'] == manager_pin
        self.client.force_authenticate(self.arbiter)
        reviewer_pin = self.client.get(self.arb_base + 'capability/').data['viewer_hash']
        assert reviewer_pin != manager_pin
        assert self.get_detail(row).data['viewer_hash'] == reviewer_pin
        assert self.client.get(self.arb_base).data['results'][0]['viewer_hash'] == reviewer_pin
        with patch.object(
            get_identity_provider(),
            'resolve',
            return_value=Principal('other-subject', 'test', self.arbiter.email, self.arbiter.pk),
        ):
            assert self.get_detail(row).data['viewer_hash'] != reviewer_pin
        ProjectMember.objects.filter(project=self.project, user=self.arbiter).update(role='manager')
        assert self.get_detail(row).data['viewer_hash'] != reviewer_pin
        self.client.force_authenticate(self.other_arbiter)
        listing = self.client.get(self.arb_base)
        assert listing.data['count'] == 0 and listing.data['viewer_hash'] != reviewer_pin

    def test_revocation_during_read_render_and_adoption_render_discards_payload(self):
        row = self.pending()
        self.client.force_authenticate(self.arbiter)
        original = Response.render

        def revoke(response):
            result = original(response)
            OrganizationMember.objects.filter(organization=self.org, user=self.arbiter).update(
                deleted_at=timezone.now()
            )
            return result

        with patch.object(Response, 'render', revoke):
            response = self.command(row)
        assert response.status_code in (403, 404) and 'decision_id' not in response.data
        assert not ArbitrationDecision.objects.exists()
        row.refresh_from_db()
        assert row.status == 'pending'

    def test_source_change_during_response_rolls_back_adoption(self):
        row = self.pending()
        self.client.force_authenticate(self.arbiter)
        original = Response.render

        def corrupt(response):
            result = original(response)
            if response.status_code == 201:
                Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
            return result

        with patch.object(Response, 'render', corrupt):
            assert self.command(row).status_code == 409
        assert not ArbitrationDecision.objects.exists()
        row.refresh_from_db()
        assert row.status == 'pending' and row.version == 1

    def test_frozen_task_decision_and_terminal_state_guards(self):
        row = self.pending()
        for field, value in {
            'id': row.pk + 1000,
            'input_set_hash': '0' * 64,
            'origin': {},
            'participant_ids': [],
            'assignee_id_frozen': self.other_arbiter.pk,
        }.items():
            with self.assertRaises(DatabaseError), transaction.atomic():
                ArbitrationTask.objects.filter(pk=row.pk).update(**{field: value})
        self.client.force_authenticate(self.arbiter)
        assert self.command(row).status_code == 201
        decision = ArbitrationDecision.objects.get()
        for field, value in {
            'id': decision.pk + 1000,
            'winner': {},
            'reason': 'Changed',
            'payload_hash': '0' * 64,
            'actor_id_frozen': self.manager.pk,
        }.items():
            with self.assertRaises(DatabaseError), transaction.atomic():
                ArbitrationDecision.objects.filter(pk=decision.pk).update(**{field: value})
        with self.assertRaises(DatabaseError), transaction.atomic():
            ArbitrationTask.objects.filter(pk=row.pk).update(status='pending', version=1)
        ArbitrationDecision.objects.filter(pk=decision.pk).update(actor=None)
        assert self.get_detail(row).status_code == 200

    def test_assignee_list_is_scoped_independent_and_uses_mapped_actor(self):
        value = self.dispatch_body()
        query = {k: value[k] for k in ('batch_id', 'task_id', 'expected_manifest_hash')}
        listing = self.client.get(self.arb_base + 'assignees/', query)
        assert listing.status_code == 200, listing.data
        assert {r['id'] for r in listing.data['results']} == {self.arbiter.pk, self.other_arbiter.pk}
        mapped = UserFactory(active_organization=self.org)
        ProjectMember.objects.create(project=self.project, user=mapped, role='manager')
        OrganizationMember.objects.get_or_create(organization=self.org, user=mapped)
        self.client.force_authenticate(self.worker)
        with patch.object(
            get_identity_provider(),
            'resolve',
            return_value=Principal('mapped-manager', 'test', mapped.email, mapped.pk),
        ):
            assert self.client.get(self.arb_base + 'assignees/', query).status_code == 200
            created = self.dispatch(value)
            assert created.status_code == 201, created.data
            assert ArbitrationTask.objects.get().actor_id_frozen == mapped.pk
        self.client.force_authenticate(self.arbiter)
        assert self.client.get(self.arb_base + 'assignees/', query).status_code == 404

    def test_actual_twenty_participant_boundary(self):
        for user in UserFactory.create_batch(18, active_organization=self.org):
            assignment = TaskAssignment.objects.create(
                project=self.project, task=self.first.assignment.task, assignee=user
            )
            source = self.make_source(worker=user, assignment=assignment)
            self.set_answer(source, 'Positive')
        value = self.dispatch_body()
        assert self.dispatch(value).status_code == 201
        with patch.object(a, 'MAX_PARTICIPANTS', 19):
            assert self.dispatch(value).status_code == 422

    def test_foreign_source_graph_and_actor_remap_fail_closed(self):
        row = self.pending()
        self.client.force_authenticate(self.arbiter)
        TaskAssignment.objects.filter(pk=self.first.assignment_id).update(project=self.other)
        assert self.get_detail(row).data['available'] is False
        assert self.command(row).status_code == 409
        TaskAssignment.objects.filter(pk=self.first.assignment_id).update(project=self.project)
        original = a.detail
        provider = get_identity_provider()
        resolve = provider.resolve
        changed = False

        def drift(*args):
            nonlocal changed
            result = original(*args)
            changed = True
            return result

        def remap(request):
            return (
                Principal('mapped-other', 'test', self.other_arbiter.email, self.other_arbiter.pk)
                if changed
                else resolve(request)
            )

        with patch.object(a, 'detail', drift), patch.object(provider, 'resolve', remap):
            response = self.get_detail(row)
        assert response.status_code == 409 and 'report' not in response.data
