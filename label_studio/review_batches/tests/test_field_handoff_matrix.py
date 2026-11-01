"""Matrix edge cases: trusted mapped actors and independent lock orderings."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from access_control.identity import LocalIdentityProvider, Principal
from access_control.project_operations import ProjectOperationPolicy, local_principal
from classification_disagreements import field_api, fields
from classification_disagreements import field_arbitration as a
from classification_disagreements.models import FieldArbitrationTask, FieldBatch
from django.db import IntegrityError, close_old_connections, connection, models, transaction
from django.db.models import JSONField
from django.test import TransactionTestCase, override_settings
from organizations.tests.factories import OrganizationFactory
from projects.models import Project, ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APITestCase
from review_batches import field_handoff as h
from review_batches import field_handoff_api as api
from review_batches import field_priority as priority
from review_batches.models import FieldReviewArbitrationHandoff, ReviewBatch
from tasks.models import ReviewDecision, Submission
from tasks.submissions import create_submission
from users.models import User
from users.tests.factories import UserFactory

from . import test_field_handoff_concurrency as races
from .test_field_handoff import FieldHandoffFixtures

GATES = dict(
    ANNOTATION_REVIEW_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_DISAGREEMENTS_ENABLED=True,
    ANNOTATION_FIELD_BATCHES_ENABLED=True,
    ANNOTATION_FIELD_REVIEW_PRIORITY_ENABLED=True,
    ANNOTATION_FIELD_ARBITRATION_ENABLED=True,
    ANNOTATION_FIELD_REVIEW_ARBITRATION_HANDOFF_ENABLED=True,
)


def database_actor_state(identifier, **values):
    """Synthetic independent database-state fault, without session-policy side effects."""
    return models.QuerySet.update(User.objects.filter(pk=identifier), **values)


class NoReadPolicy:
    def evaluate(self, project):
        return ProjectOperationPolicy('no-read/v1', 'active', frozenset({'organize_field_arbitration'}))


@override_settings(**GATES)
class TestFieldHandoffMatrixEdges(FieldHandoffFixtures, APITestCase):
    def clone_review(self, plan):
        values = ReviewBatch.objects.values().get(pk=self.batch.pk)
        values.pop('id')
        values.update(plan=plan, plan_hash=fields.digest(plan), idempotency_key=uuid4())
        return ReviewBatch.objects.create(**values)

    def reject_plan(self, plan, original_body):
        batch = self.clone_review(plan)
        body = {**original_body, 'expected_plan_hash': batch.plan_hash}
        path = f'{self.base}{batch.pk}/sources/{self.first.pk}/field-arbitration-handoffs/'
        with patch.object(field_api, 'load', side_effect=AssertionError('Invalid envelope must precede snapshots')):
            result = self.client.post(path, body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        assert result.status_code in (403, 404, 409, 422), result.data
        assert not FieldReviewArbitrationHandoff.objects.exists() and not FieldArbitrationTask.objects.exists()
        return result

    def test_all_nine_legal_but_wrong_pins_and_priority_versions_reject_rehashed_fresh_commands(self):
        original = self.batch.plan
        value = self.handoff_body()
        for name in field_api.PIN_IDS + field_api.PIN_HASHES:
            with self.subTest(pin=name):
                plan = copy.deepcopy(original)
                item = next(e for e in plan['priority']['evidence'] if e['submission_id'] == self.first.pk)
                current = item['source_pin'][name]
                item['source_pin'][name] = (
                    current + 100000 if type(current) is int else ('0' if current != '0' * 64 else '1') * 64
                )
                if name in ('config_hash', 'task_data_hash'):
                    priority.validate(plan)
                result = self.reject_plan(plan, value)
                if name in ('config_hash', 'task_data_hash'):
                    assert result.data['display_context']['reason'] == 'REVIEW_FIELD_HANDOFF_SOURCE_CHANGED'
        for name in (*priority.VERSIONS, 'schema', 'kind'):
            with self.subTest(version=name):
                plan = copy.deepcopy(original)
                plan['priority'][name] = 'future-invalid/v9'
                self.reject_plan(plan, value)
        plan = copy.deepcopy(original)
        plan['algorithm'] = 'future-invalid/v9'
        self.reject_plan(plan, value)
        plan = copy.deepcopy(original)
        next(e for e in plan['priority']['evidence'] if e['submission_id'] == self.first.pk)['calculation_hash'] = (
            '0' * 64
        )
        self.reject_plan(plan, value)

    def test_all_field_manifest_versions_results_cost_and_hash_reject_rehashed_envelopes(self):
        original = FieldBatch.objects.values().get(pk=self.analysis['id'])
        value = self.handoff_body()
        for name in ('schema', 'rule', 'selection', 'explanation', 'engine', 'queue_rule', 'result', 'cost', 'hash'):
            with self.subTest(field=name):
                manifest = copy.deepcopy(original['manifest'])
                if name == 'result':
                    manifest['tasks'][0]['result']['calculation_hash'] = '0' * 64
                elif name == 'cost':
                    manifest['cost_per_pass']['normalized_values'] += 1
                elif name != 'hash':
                    manifest[name] = 'future-invalid/v9'
                values = {k: v for k, v in original.items() if k != 'id'}
                values.update(manifest=manifest, manifest_hash=fields.digest(manifest), idempotency_key=uuid4())
                analysis = FieldBatch.objects.create(**values)
                plan = copy.deepcopy(self.batch.plan)
                plan['priority'].update(
                    batch_id=analysis.pk, manifest_hash='0' * 64 if name == 'hash' else analysis.manifest_hash
                )
                priority.validate(plan)
                self.reject_plan(plan, value)

    def test_nearby_target_with_same_task_and_old_pair_but_an_extra_cohort_cannot_link(self):
        actor = UserFactory(active_organization=self.org)
        self.org.add_user(actor)
        ProjectMember.objects.create(project=self.project, user=actor, role='annotator')
        source = self.make_source(worker=actor)
        source.assignment.task = self.first.assignment.task
        source.assignment.save()
        self.write(source, ('third', 'cohort', '001'))
        other = self.freeze_analysis()
        result = self.standalone(self.standalone_body(other))
        assert result.status_code == 201, result.data
        target = FieldArbitrationTask.objects.get()
        value = self.handoff_body(target)
        assert value['expected_input_set_hash'] != target.input_set_hash
        denied = self.handoff(value)
        assert denied.status_code == 409 and 'receipt' not in denied.data, denied.data
        assert not FieldReviewArbitrationHandoff.objects.exists() and FieldArbitrationTask.objects.count() == 1

    def test_original_parent_recovers_after_real_target_deletion_without_loading_live_json(self):
        value, key = self.handoff_body(), uuid4()
        saved = self.handoff(value, key)
        assert saved.status_code == 201, saved.data
        FieldArbitrationTask.objects.get().delete()
        assert FieldReviewArbitrationHandoff.objects.get().field_arbitration_id is None
        with patch.object(
            h, 'Metadata', side_effect=AssertionError('Recovery cannot read live metadata')
        ), patch.object(
            field_api, 'load', side_effect=AssertionError('Recovery cannot hydrate snapshots')
        ), patch.object(
            fields, 'normalize', side_effect=AssertionError('Recovery cannot score')
        ):
            result = self.handoff(value, key)
        assert result.status_code == 200 and result.data['receipt'] == saved.data['receipt'], result.data
        assert not FieldArbitrationTask.objects.exists()

    def test_real_unique_violations_are_conflicts_and_both_inserts_roll_back(self):
        original = h.create_record
        for conflict in ('parent_key', 'binding', 'active_target'):
            with self.subTest(conflict=conflict), transaction.atomic():
                value, key = self.handoff_body(), uuid4()

                def duplicate(*args, **kwargs):
                    row = original(*args, **kwargs)
                    if conflict == 'active_target':
                        values = FieldArbitrationTask.objects.values().get(pk=row.field_arbitration_id)
                        values.pop('id')
                        values['idempotency_key'] = uuid4()
                        FieldArbitrationTask.objects.create(**values)
                    else:
                        values = FieldReviewArbitrationHandoff.objects.values().get(pk=row.pk)
                        values.pop('id')
                        if conflict == 'parent_key':
                            values['submission_id_frozen'] += 100000
                        else:
                            values['idempotency_key'] = uuid4()
                        FieldReviewArbitrationHandoff.objects.create(**values)
                    raise AssertionError('The database must reject the duplicate')

                with patch.object(h, 'create_record', duplicate):
                    denied = self.handoff(value, key)
                assert denied.status_code == 409, denied.data
                assert denied.data['display_context']['reason'] == 'REVIEW_FIELD_HANDOFF_UNIQUE_CONFLICT'
                assert not FieldReviewArbitrationHandoff.objects.exists() and not FieldArbitrationTask.objects.exists()
                retry = self.handoff(value, key)
                assert retry.status_code == 201, retry.data
                assert FieldReviewArbitrationHandoff.objects.count() == FieldArbitrationTask.objects.count() == 1
                transaction.set_rollback(True)
        assert not api.unique_conflict(IntegrityError('unrecognized database failure'))

    def test_known_handoff_and_target_ids_cannot_cross_project_or_organization_routes(self):
        saved = self.handoff()
        assert saved.status_code == 201, saved.data
        foreign = ProjectFactory(organization=OrganizationFactory())
        for project in (self.other, foreign):
            base = f'/api/projects/{project.pk}/review-batches/{self.batch.pk}/'
            for suffix in (
                f'sources/{self.first.pk}/field-arbitration-handoffs/',
                f'field-arbitration-handoffs/{saved.data["id"]}/',
                f'field-arbitration-handoffs/{saved.data["id"]}/provenance/',
            ):
                result = self.client.get(base + suffix)
                assert result.status_code in (403, 404), result.data
                assert 'results' not in result.data and 'receipt' not in result.data
            result = self.client.get(f'/api/projects/{project.pk}/field-arbitrations/{saved.data["arbitration_id"]}/')
            assert result.status_code in (403, 404), result.data
        # Correct project, wrong batch and source remain separately scoped.
        for batch_id, source_id in ((self.batch.pk + 100000, self.first.pk), (self.batch.pk, self.second.pk + 100000)):
            result = self.client.get(f'{self.base}{batch_id}/sources/{source_id}/field-arbitration-handoffs/')
            assert result.status_code == 404 or (result.status_code == 200 and result.data['count'] == 0), result.data
        wrong = self.client.get(f'{self.base}{self.batch.pk + 100000}/field-arbitration-handoffs/{saved.data["id"]}/')
        assert wrong.status_code == 404

    def test_link_does_not_restore_an_inactive_assignees_adoption_rights(self):
        pending = self.standalone()
        assert pending.status_code == 201, pending.data
        target = FieldArbitrationTask.objects.get()
        value = self.handoff_body(target)
        database_actor_state(self.independent.pk, is_active=False)
        result = self.handoff(value)
        assert result.status_code == 201, result.data
        target.refresh_from_db()
        assert target.assignee_id == self.independent.pk and target.status == 'pending'
        self.client.force_authenticate(self.independent)
        self.arbitrate(target, expected=401)
        self.client.force_authenticate(self.manager)
        self.arbitrate(target, expected=404)
        assert not ReviewDecision.objects.exists()

    def test_read_withdrawal_denies_handoff_but_preserves_known_target_security_cancel(self):
        value, key = self.handoff_body(), uuid4()
        saved = self.handoff(value, key)
        assert saved.status_code == 201, saved.data
        target = FieldArbitrationTask.objects.get()
        with self.settings(ANNOTATION_PROJECT_OPERATION_PROVIDER=__name__ + '.NoReadPolicy'):
            for path in (
                self.context_url,
                self.handoff_url,
                f'{self.base}{self.batch.pk}/field-arbitration-handoffs/{saved.data["id"]}/',
            ):
                assert self.client.get(path).status_code in (403, 404)
            assert self.handoff(value, key).status_code in (403, 404)
            assert self.handoff(value).status_code in (403, 404)
            self.arbitrate(target, cancel=True)
        target.refresh_from_db()
        assert target.status == 'cancelled' and not ReviewDecision.objects.exists()

    def test_user_fk_cleanup_preserves_frozen_actor_then_project_cascades_new_ledger(self):
        actor = UserFactory(active_organization=self.org)
        self.org.add_user(actor)
        ProjectMember.objects.create(project=self.project, user=actor, role='manager')
        self.client.force_authenticate(actor)
        saved = self.handoff()
        assert saved.status_code == 201, saved.data
        row = FieldReviewArbitrationHandoff.objects.get()
        frozen, origin_hash = actor.pk, row.origin_hash
        actor.delete()
        row.refresh_from_db()
        assert row.created_by_id is None and row.actor_id_frozen == frozen and row.origin_hash == origin_hash
        self.project.delete()
        assert not FieldReviewArbitrationHandoff.objects.exists() and not FieldArbitrationTask.objects.exists()

    def test_multiple_proofs_share_page_budget_before_decode_and_smaller_pages_keep_count(self):
        identifiers = []
        for index in range(3):
            result = self.handoff()
            assert result.status_code == 201, result.data
            identifiers.append(result.data['id'])
            if index < 2:
                self.arbitrate(FieldArbitrationTask.objects.get(pk=result.data['arbitration_id']), cancel=True)
        measured = []
        lengths = []
        add = h.Metadata.add

        def measure(budget, *args):
            result = add(budget, *args)
            measured.append((budget.canonical, budget.stored))
            return result

        with patch.object(h.Metadata, 'add', measure):
            for page in (1, 2, 3):
                result = self.client.get(self.handoff_url, {'page_size': 1, 'page': page})
                assert result.status_code == 200 and result.data['count'] == 3, result.data
                lengths.append(len(result.content))
        canonical = max(item[0] for item in measured)
        stored = max(item[1] for item in measured)
        budget = h.Metadata()
        h.review_batch((local_principal(self.manager), self.manager, self.project, 'manager'), self.batch.pk, budget)
        proof_sizes = list(h.rows(self.project).values_list('_size_origin', flat=True))
        # The already-reserved review plan is outside the remaining proof-page allowance.
        assert sum(proof_sizes) > stored - budget.stored >= max(proof_sizes)
        decoded = []
        decode = JSONField.from_db_value

        def guarded_decode(field, value, *args):
            if isinstance(value, str) and h.SCHEMA in value:
                decoded.append(value)
                raise AssertionError('Over-budget page proof decoded')
            return decode(field, value, *args)

        with patch.object(h, 'MAX_STORED_METADATA', stored), patch.object(JSONField, 'from_db_value', guarded_decode):
            denied = self.client.get(self.handoff_url)
        assert denied.status_code == 422 and not decoded, denied.data
        for cap in ({'MAX_STORED_METADATA': stored}, {'MAX_METADATA': canonical}):
            with patch.multiple(h, **cap):
                assert self.client.get(self.handoff_url).status_code == 422
                seen = []
                for page in (1, 2, 3):
                    result = self.client.get(self.handoff_url, {'page_size': 1, 'page': page})
                    assert result.status_code == 200 and result.data['count'] == 3, result.data
                    seen.extend(row['id'] for row in result.data['results'])
                assert sorted(seen) == sorted(identifiers)
        limit = max(lengths) + 64
        assert len(self.client.get(self.handoff_url).content) > limit
        with patch.object(fields, 'MAX_OUTPUT', limit):
            assert self.client.get(self.handoff_url).status_code == 422
            reduced = self.client.get(self.handoff_url, {'page_size': 1})
            assert reduced.status_code == 200 and reduced.data['count'] == 3, reduced.data


@override_settings(**GATES)
class TestFieldHandoffMappedActor(FieldHandoffFixtures, APITestCase):
    def test_real_transport_token_uses_trusted_actor_for_authorization_and_audit(self):
        self.org.jwt.legacy_api_tokens_enabled = True
        self.org.jwt.save(update_fields=['legacy_api_tokens_enabled'])
        transport, actor = self.worker, self.manager
        calls = []

        class Provider:
            source = 'trusted-platform'

            def resolve(provider, request):
                assert request.user.pk == transport.pk
                calls.append(request.user.pk)
                return Principal('platform:manager', provider.source, actor.email, actor.pk)

        provider = Provider()
        self.client.force_authenticate(None)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {transport.get_token().key}')
        with patch('access_control.identity.get_identity_provider', return_value=provider):
            value, key = self.handoff_body(), uuid4()
            created = self.handoff(value, key)
            assert created.status_code == 201, created.data
            row = FieldReviewArbitrationHandoff.objects.get()
            target = FieldArbitrationTask.objects.get()
            assert row.actor_id_frozen == row.created_by_id == actor.pk != transport.pk
            assert target.actor_id_frozen == actor.pk and target.created_by_id == actor.pk
            assert row.creator_principal == 'platform:manager' and row.creator_source == provider.source
            assert target.creator_principal == row.creator_principal
            assert created.data['actor_id'] == actor.pk and calls
            assert self.handoff(value, key).status_code == 200
            provider.source = 'different-platform'
            denied = self.handoff(value, key)
            assert denied.status_code == 409 and 'receipt' not in denied.data
        # Authentication B alone has no Manager authority despite knowing A's receipt.
        denied = self.handoff(value, key)
        assert denied.status_code in (403, 404) and 'receipt' not in denied.data
        assert FieldReviewArbitrationHandoff.objects.count() == FieldArbitrationTask.objects.count() == 1


@override_settings(**GATES)
class TestFieldHandoffMatrixRaces(FieldHandoffFixtures, TransactionTestCase):
    setUp = races.TestFieldHandoffConcurrency.setUp
    client_class = races.TestFieldHandoffConcurrency.client_class
    client_for = races.TestFieldHandoffConcurrency.client_for
    post = races.TestFieldHandoffConcurrency.post
    competing = races.TestFieldHandoffConcurrency.competing
    command_body = races.TestFieldHandoffConcurrency.command_body

    def test_b_detail_last_equality_loses_only_a_then_final_provenance_masks_joint_read(self):
        other = self.freeze_analysis()
        created = self.standalone(self.standalone_body(other))
        assert created.status_code == 201, created.data
        target = FieldArbitrationTask.objects.get()
        saved = self.handoff(self.handoff_body(target))
        assert saved.status_code == 201, saved.data
        equal, calls = fields.equal, []

        def remove_a():
            close_old_connections()
            try:
                FieldBatch.objects.filter(pk=self.analysis['id']).delete()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:

            def equality(left, right):
                result = equal(left, right)
                calls.append(1)
                if len(calls) == 12:
                    pool.submit(remove_a).result(10)
                return result

            with patch.object(fields, 'equal', equality):
                detail = self.client.get(f'/api/projects/{self.project.pk}/field-arbitrations/{target.pk}/')
        assert (
            len(calls) == 12 and detail.status_code == 200 and detail.data['source_status'] == 'verified'
        ), detail.data
        assert FieldBatch.objects.filter(pk=other['id']).exists()
        with patch.object(fields, 'normalize', side_effect=AssertionError('No provenance scoring')):
            proof = self.client.get(
                f'{self.base}{self.batch.pk}/field-arbitration-handoffs/{saved.data["id"]}/provenance/'
            )
        assert (
            proof.status_code == 200
            and proof.data['provenance_status'] == 'unavailable'
            and proof.data['target'] is None
        ), proof.data

    def test_handoff_commits_before_new_revision_without_performing_review(self):
        value, key = self.handoff_body(), uuid4()
        entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
        evidence = a.evidence

        def hold(*args, **kwargs):
            result = evidence(*args, **kwargs)
            if kwargs.get('lock'):
                entered.set()
                assert release.wait(20)
            return result

        def revision():
            close_old_connections()
            try:

                def observe(execute, sql, params, many, context):
                    if 'FOR UPDATE' in sql and '"project"' in sql:
                        attempted.set()
                    return execute(sql, params, many, context)

                with connection.execute_wrapper(observe):
                    return create_submission(
                        assignment=self.first.assignment, annotation=self.first.annotation, actor=self.worker
                    ).pk
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(a, 'evidence', hold):
            first = pool.submit(self.post, self.client_for(self.manager), self.handoff_url, value, key)
            try:
                assert entered.wait(20)
                later = pool.submit(revision)
                assert attempted.wait(20) and not later.done()
            finally:
                release.set()
            saved, newer_id = first.result(25), later.result(25)
        assert saved.status_code == 201, saved.data
        assert Submission.objects.get(pk=newer_id).revision == 2
        assert Submission.objects.get(pk=self.first.pk).status == 'superseded'
        assert not ReviewDecision.objects.exists()
        assert FieldArbitrationTask.objects.get().status == 'pending'
        recovered = self.handoff(value, key)
        assert recovered.status_code == 200 and recovered.data['receipt'] == saved.data['receipt']

    def test_disabled_account_organization_and_provider_changes_precede_waiting_fresh_and_recovery(self):
        value, key = self.handoff_body(), uuid4()
        saved = self.handoff(value, key)
        assert saved.status_code == 201, saved.data
        other = OrganizationFactory()
        original = LocalIdentityProvider.resolve
        roles = list(ProjectMember.objects.filter(project=self.project).values())
        for recovery in (False, True):
            for change in ('disabled', 'organization', 'provider'):
                with self.subTest(recovery=recovery, change=change):
                    attempted, shifted = threading.Event(), threading.Event()

                    def principal(provider, request):
                        current = original(provider, request)
                        return replace(current, source='changed-provider') if shifted.is_set() else current

                    with ThreadPoolExecutor(max_workers=1) as pool, patch.object(
                        LocalIdentityProvider, 'resolve', principal
                    ):
                        with transaction.atomic():
                            Project.objects.select_for_update().get(pk=self.project.pk)
                            future = pool.submit(
                                self.post,
                                self.client_for(self.manager),
                                self.handoff_url,
                                value,
                                key if recovery else uuid4(),
                                attempted,
                            )
                            assert attempted.wait(20) and not future.done()
                            if change == 'disabled':
                                database_actor_state(self.manager.pk, is_active=False)
                            elif change == 'organization':
                                User.objects.filter(pk=self.manager.pk).update(active_organization=other)
                            else:
                                shifted.set()
                        result = future.result(25)
                    assert result.status_code in (401, 403, 404, 409), result.data
                    assert 'receipt' not in result.data
                    database_actor_state(self.manager.pk, is_active=True, active_organization=self.org)
                    assert list(ProjectMember.objects.filter(project=self.project).values()) == roles
        assert FieldReviewArbitrationHandoff.objects.count() == FieldArbitrationTask.objects.count() == 1

    def mutation_after_writer(self, field):
        value, key = self.handoff_body(), uuid4()
        entered, release, attempted, completed = [threading.Event() for _ in range(4)]
        evidence = a.evidence
        other = OrganizationFactory() if field == 'active_organization' else None

        def hold(*args, **kwargs):
            result = evidence(*args, **kwargs)
            if kwargs.get('lock'):
                entered.set()
                assert release.wait(20)
            return result

        def mutate():
            close_old_connections()
            try:

                def observe(execute, sql, params, many, context):
                    if sql.lstrip().startswith('UPDATE') and '"' + User._meta.db_table + '"' in sql:
                        attempted.set()
                    return execute(sql, params, many, context)

                with connection.execute_wrapper(observe):
                    database_actor_state(self.manager.pk, **{field: other if other else False})
                completed.set()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(a, 'evidence', hold):
            first = pool.submit(self.post, self.client_for(self.manager), self.handoff_url, value, key)
            try:
                assert entered.wait(20)
                later = pool.submit(mutate)
                assert attempted.wait(20) and not completed.wait(0.15)
            finally:
                release.set()
            saved = first.result(25)
            later.result(25)
        assert saved.status_code == 201, saved.data
        assert completed.is_set()
        denied = self.handoff(value, key)
        assert denied.status_code in (401, 403, 404) and 'receipt' not in denied.data
        assert FieldReviewArbitrationHandoff.objects.count() == FieldArbitrationTask.objects.count() == 1

    def test_writer_holds_account_lock_until_commit_then_disable_blocks_recovery(self):
        self.mutation_after_writer('is_active')

    def test_writer_holds_organization_lock_until_commit_then_switch_blocks_recovery(self):
        self.mutation_after_writer('active_organization')

    def opposite_origins(self, reverse):
        extra = self.make_source()
        peer = self.make_source(worker=self.reviewer)
        peer.assignment.task = extra.assignment.task
        peer.assignment.save()
        self.write(extra)
        self.write(peer, ('different', 'fields', '2'))
        task_ids = [self.first.assignment.task_id, extra.assignment.task_id]
        origins = [self.freeze_analysis(task_ids), self.freeze_analysis(task_ids)]
        entries = []
        for index, source in enumerate((self.first, extra)):
            analysis, original = origins[index], origins[1 - index]
            target_body = {**self.standalone_body(original), 'task_id': source.assignment.task_id}
            created = self.standalone(target_body)
            assert created.status_code == 201, created.data
            target = FieldArbitrationTask.objects.get(pk=created.data['id'])
            self.analysis = analysis
            self.args['priority'].update(batch_id=analysis['id'], manifest_hash=analysis['manifest_hash'])
            batch = self.create()
            assert batch.status_code == 201, batch.data
            self.batch = ReviewBatch.objects.get(pk=batch.data['id'])
            self.handoff_url = f'{self.base}{self.batch.pk}/sources/{source.pk}/field-arbitration-handoffs/'
            self.context_url = self.handoff_url.replace('field-arbitration-handoffs/', 'field-arbitration-context/')
            entries.append((self.handoff_url, self.handoff_body(target), target, source))
        first, second = entries[::-1] if reverse else entries
        traces = {}
        original_post = self.post

        def traced(client, path, body, key, attempted=None):
            close_old_connections()
            trace = traces.setdefault(path, [])

            def observe(execute, sql, params, many, context):
                if 'FOR UPDATE' in sql:
                    for model in (FieldArbitrationTask, FieldBatch):
                        if '"' + model._meta.db_table + '"' in sql:
                            trace.append((model._meta.db_table, params[-1]))
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe):
                return original_post(client, path, body, key, attempted)

        with patch.object(self, 'post', traced):
            result = self.competing(
                self.manager, self.manager, first[0], second[0], first[1], second[1], uuid4(), uuid4()
            )
        assert [item.status_code for item in result] == [201, 201]
        for path, _, target, _ in entries:
            trace = traces[path]
            assert trace[0] == (FieldArbitrationTask._meta.db_table, target.pk), trace
            batch_locks = [pk for table, pk in trace if table == FieldBatch._meta.db_table]
            assert batch_locks == sorted(origin['id'] for origin in origins), trace
        target_a, source_a = entries[0][2:]
        target_b = entries[1][2]
        decision = {**self.command_body(target_a), 'winner_submission_id': source_a.pk}
        cancel = self.command_body(target_b, cancel=True)
        paths = [
            f'/api/projects/{self.project.pk}/field-arbitrations/{target.pk}/{action}/'
            for target, action in ((target_a, 'decision'), (target_b, 'cancel'))
        ]
        terminal = self.competing(
            self.independent, self.manager, paths[0], paths[1], decision, cancel, uuid4(), uuid4()
        )
        assert [item.status_code for item in terminal] == [201, 201]
        assert FieldReviewArbitrationHandoff.objects.count() == 2
        assert sorted(FieldArbitrationTask.objects.values_list('status', flat=True)) == ['cancelled', 'resolved']
        assert not ReviewDecision.objects.exists()

    def test_opposite_origins_link_in_forward_order_then_independent_decision_and_cancel(self):
        self.opposite_origins(False)

    def test_opposite_origins_link_in_reverse_order_then_independent_decision_and_cancel(self):
        self.opposite_origins(True)
