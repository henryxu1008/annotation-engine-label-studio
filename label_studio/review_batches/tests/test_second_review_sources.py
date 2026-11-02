import copy
from unittest.mock import patch
from uuid import uuid4

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APITestCase
from review_batches import second_review_contract as c
from review_batches import second_review_origin as origin
from review_batches.models import ReviewBatch, SecondReviewTask
from tasks.models import ReviewDecision, Submission, TaskAssignment
from tasks.submissions import _snapshot_hash, review_submission

from .second_review_fixtures import SecondReviewFixtures


class TestSecondReviewSources(SecondReviewFixtures, APITestCase):
    def setUp(self):
        self.second_setup()

    def checked(self):
        source, batch = self.prepared()
        result, body = self.dispatch(source, batch)
        assert result.status_code == 201, result.data
        return source, batch, result.data['receipt']['task_id'], body

    def test_A02_confidence_history_uses_target_without_registry_grant_or_scoring(self):
        source, batch = self.prepared(confidence=True)
        with (
            self.settings(ANNOTATION_REVIEW_CONFIDENCE_PROFILES=[], ANNOTATION_REVIEW_CONFIDENCE_GRANTS=[]),
            patch('review_batches.confidence_priority.materialize', side_effect=AssertionError('no scoring')),
            patch('review_batches.service.read_sources', side_effect=AssertionError('no cohort')),
        ):
            result, _ = self.dispatch(source, batch)
            assert result.status_code == 201, result.data
            self.client.force_authenticate(self.supervisor)
            result = self.client.get(self.second_base + result.data['receipt']['task_id'] + '/source/')
            assert result.status_code == 200, result.data
            assert result.data['source_health'] == 'target_source_verified'
            assert result.data['metric_integrity'] == 'not_checked'
            assert 'score_units' not in str(result.data) and 'producer_namespace' not in str(result.data)

    def test_A04_initial_pending_unselected_superseded_and_nonlatest_rejected(self):
        source, batch = self.prepared()
        for status in ('pending', 'superseded'):
            with transaction.atomic():
                Submission.objects.filter(pk=source.pk).update(status=status)
                result = self.context(source, batch)
                assert result.status_code == 409, result.data
                transaction.set_rollback(True)
        self.make_source(assignment=source.assignment, revision=2)
        assert self.context(source, batch).status_code == 409
        assert not SecondReviewTask.objects.exists()

    def test_A04_genuine_unselected_and_deleted_initial_review_rejected(self):
        one, two = self.make_source(), self.make_source(self.second_worker)
        args = dict(name='one selected', rate=1, seed='fixed')
        preview = self.preview(args)
        saved = self.create_batch({**args, 'expected_plan_hash': preview.data['plan_hash']})
        assert saved.status_code == 201
        candidate = next(v for v in preview.data['plan']['candidates'] if not v['selected'])
        unselected = one if candidate['submission_id'] == one.pk else two
        review_submission(submission=unselected, reviewer=self.reviewer, decision='approved')
        result = self.context(unselected, saved.data['id'])
        assert result.status_code == 409 and not SecondReviewTask.objects.exists()
        source, batch = self.prepared()
        ReviewDecision.objects.filter(submission=source).delete()
        assert self.context(source, batch).status_code == 409 and not SecondReviewTask.objects.exists()

    def test_A05_static_contract_rejects_dynamic_url_html_media_and_foreign_types(self):
        variants = [
            '<View><Text name="text" value="$text" valueType="url"/><Choices name="label" toName="text"><Choice value="A"/></Choices></View>',
            '<View><Text name="text" value="$text"/><Choices name="label" toName="text" value="$labels"/></View>',
            '<View style="background:url(https://example.invalid)"><Text name="text" value="$text"/><Choices name="label" toName="text"><Choice value="A"/></Choices></View>',
            '<View><Image name="text" value="$text"/><Choices name="label" toName="text"><Choice value="A"/></Choices></View>',
            '<View><Text name="text" value="$text"/><Labels name="label" toName="text"><Label value="A"/></Labels></View>',
            '<View><Text name="text" value="$text"/><Choices name="label" toName="text"><Choice value="A"/></Choices><HyperText name="html" value="$html"/></View>',
        ]
        for xml in variants:
            source, batch = self.prepared()
            snapshot = copy.deepcopy(source.result_snapshot)
            snapshot['project']['label_config'] = xml
            Submission.objects.filter(pk=source.pk).update(
                result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
            )
            # Candidate pin still binds the original hash. There is no fallback.
            assert self.context(source, batch).status_code in (400, 409)
        assert not SecondReviewTask.objects.exists()

    def test_A06_swapped_exact_submission_not_task_id(self):
        source, batch = self.prepared()
        other = self.make_source(worker=self.second_worker)
        TaskAssignment.objects.filter(pk=other.assignment_id).update(task_id=source.assignment.task_id)
        result = self.context(other, batch)
        assert result.status_code == 409, result.data

    def test_A07_dispatch_before_new_revision_keeps_exact_historical_source(self):
        source, batch, pk, _ = self.checked()
        newer = self.make_source(assignment=source.assignment, revision=2)
        self.client.force_authenticate(self.supervisor)
        result = self.client.get(self.second_base + pk + '/source/')
        assert result.status_code == 200 and result.data['historical_revision'], result.data
        assert result.data['subject']['submission_id'] == source.pk
        assert self.decide(pk).status_code == 201
        newer.refresh_from_db()
        assert newer.status == 'pending' and not hasattr(newer, 'review')

    def test_A08_metadata_has_zero_snapshot_initial_reason_batch_or_proof_reads(self):
        source, batch, pk, _ = self.checked()
        self.client.force_authenticate(self.supervisor)
        with patch.object(origin, 'load', side_effect=AssertionError('source on metadata')), CaptureQueriesContext(
            connection
        ) as queries:
            assert self.client.get(self.second_base).status_code == 200
            detail = self.client.get(self.second_base + pk + '/')
        assert detail.status_code == 200 and detail.data['source_integrity'] == 'not_checked'
        sql = '\n'.join(q['sql'] for q in queries.captured_queries)
        assert 'result_snapshot' not in sql and 'tasks_reviewdecision' not in sql
        assert 'review_batches_reviewbatch' not in sql and '"proof"' not in sql

    def test_A09_deleting_each_original_node_retains_history_and_original_dispatch_receipt(self):
        for kind in ('batch', 'initial', 'submission', 'assignment'):
            source, batch, pk, body = self.checked()
            with self.subTest(kind=kind), transaction.atomic():
                if kind == 'batch':
                    ReviewBatch.objects.filter(pk=batch).delete()
                elif kind == 'initial':
                    ReviewDecision.objects.filter(submission=source).delete()
                elif kind == 'submission':
                    Submission.objects.filter(pk=source.pk).delete()
                else:
                    TaskAssignment.objects.filter(pk=source.assignment_id).delete()
                self.client.force_authenticate(self.supervisor)
                assert self.client.get(self.second_base + pk + '/').status_code == 200
                assert self.client.get(self.second_base + pk + '/source/').status_code in (404, 409)
                transaction.set_rollback(True)

    def test_C01_all_pins_and_rehashed_snapshot_changes_reject(self):
        for change in (
            'assignment',
            'task',
            'revision',
            'assignee',
            'submitter',
            'result',
            'data',
            'xml',
            'submission',
        ):
            source, batch, pk, _ = self.checked()
            with self.subTest(change=change), transaction.atomic():
                if change == 'assignment':
                    other = self.make_source()
                    Submission.objects.filter(pk=other.pk).delete()
                    Submission.objects.filter(pk=source.pk).update(assignment_id=other.assignment_id)
                elif change == 'task':
                    from tasks.tests.factories import TaskFactory

                    other = TaskFactory(project=self.project)
                    TaskAssignment.objects.filter(pk=source.assignment_id).update(task_id=other.pk)
                elif change == 'revision':
                    Submission.objects.filter(pk=source.pk).update(revision=99)
                elif change == 'assignee':
                    TaskAssignment.objects.filter(pk=source.assignment_id).update(assignee=self.second_worker)
                elif change == 'submitter':
                    Submission.objects.filter(pk=source.pk).update(submitted_by=self.second_worker)
                elif change == 'submission':
                    Submission.objects.filter(pk=source.pk).delete()
                else:
                    snapshot = copy.deepcopy(source.result_snapshot)
                    if change == 'result':
                        snapshot['annotation']['result'][0]['value']['choices'] = ['B']
                    elif change == 'data':
                        snapshot['task']['data']['text'] = 'other'
                    else:
                        snapshot['project']['label_config'] += ' '
                    Submission.objects.filter(pk=source.pk).update(
                        result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
                    )
                self.client.force_authenticate(self.supervisor)
                result = self.client.get(self.second_base + pk + '/source/')
                assert result.status_code in (404, 409), (change, result.data)
                transaction.set_rollback(True)

    def test_C02_first_review_full_reason_reviewer_id_decision_time_are_pinned(self):
        from django.utils import timezone

        source, batch, pk, _ = self.checked()
        for mutation in (
            {'reason': 'Altered full reason'},
            {'reviewer_id': self.second_worker.pk},
            {'decision': 'rejected'},
            {'created_at': timezone.now()},
        ):
            with transaction.atomic():
                ReviewDecision.objects.filter(submission=source).update(**mutation)
                self.client.force_authenticate(self.supervisor)
                result = self.client.get(self.second_base + pk + '/source/')
                assert result.status_code == 409, result.data
                transaction.set_rollback(True)

    def test_C03_C04_nullable_humans_before_and_after_freeze_are_unavailable(self):
        for after in (False, True):
            for field in ('submitter', 'assignee', 'first_reviewer'):
                source, batch = self.prepared()
                pk = None
                if after:
                    result, _ = self.dispatch(source, batch)
                    pk = result.data['receipt']['task_id']
                if field == 'submitter':
                    Submission.objects.filter(pk=source.pk).update(submitted_by=None)
                elif field == 'assignee':
                    # Assignment.assignee is already NOT NULL / CASCADE. Loss
                    # of that required human removes the source graph instead.
                    TaskAssignment.objects.filter(pk=source.assignment_id).delete()
                else:
                    ReviewDecision.objects.filter(submission=source).update(reviewer=None)
                if after:
                    self.client.force_authenticate(self.supervisor)
                    assert self.client.get(self.second_base + pk + '/').status_code == 200
                    result = self.client.get(self.second_base + pk + '/source/')
                else:
                    result = self.context(source, batch)
                assert result.status_code in (404, 409), (field, after, result.data)

    def test_C10_unknown_rule_algorithm_partial_marker_cannot_be_ordinary(self):
        source, batch = self.prepared()
        original = ReviewBatch.objects.get(pk=batch)
        for mutate in (
            lambda p: p.update(algorithm='sha256-ranked-v2'),
            lambda p: p.update(priority={'schema': 'review-confidence-priority/v99'}),
            lambda p: p['rule'].update(rate=True),
            lambda p: p['candidates'][0].update(selected=1),
        ):
            plan = copy.deepcopy(original.plan)
            mutate(plan)
            bad = ReviewBatch.objects.create(
                project=self.project,
                created_by=self.manager,
                actor_id_frozen=self.manager.pk,
                creator_principal=original.creator_principal,
                name='bad',
                idempotency_key=uuid4(),
                request_hash='a' * 64,
                plan=plan,
                plan_hash=c.digest(plan),
            )
            result = self.context(source, bad.pk)
            assert result.status_code in (400, 409), result.data

    def test_D08_render_mutation_rolls_back_command_and_allows_original_key_retry(self):
        source, batch = self.prepared()
        body, key = self.dispatch_body(source, batch), uuid4()
        original = JSONRenderer.render
        touched = []

        def render(renderer, data, *args, **kwargs):
            result = original(renderer, data, *args, **kwargs)
            if isinstance(data, dict) and data.get('receipt', {}).get('command') == 'dispatch' and not touched:
                touched.append(True)
                ReviewDecision.objects.filter(submission=source).update(reason='changed during render')
            return result

        with patch.object(JSONRenderer, 'render', render):
            result, _ = self.dispatch(source, batch, body, key)
        assert result.status_code == 409 and not SecondReviewTask.objects.exists(), result.data
        result, _ = self.dispatch(source, batch, body, key)
        assert result.status_code == 201, result.data

    def test_D08_render_actor_mode_and_snapshot_changes_roll_back(self):
        real_actor = c.resolve_actor
        for change in ('actor', 'mode', 'snapshot'):
            with self.subTest(change=change):
                source, batch = self.prepared()
                body, key = self.dispatch_body(source, batch), uuid4()
                original, touched = JSONRenderer.render, []
                mode = self.settings(ANNOTATION_SECOND_REVIEW_MODES={})

                def resolve(request):
                    principal, actor = real_actor(request)
                    if touched and change == 'actor':
                        principal = type(principal)(
                            'different-owner', principal.source, principal.username, principal.local_user_id
                        )
                    return principal, actor

                def render(renderer, data, *args, **kwargs):
                    output = original(renderer, data, *args, **kwargs)
                    if isinstance(data, dict) and data.get('receipt', {}).get('command') == 'dispatch' and not touched:
                        touched.append(True)
                        if change == 'mode':
                            mode.enable()
                        elif change == 'snapshot':
                            snapshot = copy.deepcopy(source.result_snapshot)
                            snapshot['task']['data']['text'] = 'changed during render'
                            Submission.objects.filter(pk=source.pk).update(
                                result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
                            )
                    return output

                try:
                    with patch.object(JSONRenderer, 'render', render), patch.object(c, 'resolve_actor', resolve):
                        result, _ = self.dispatch(source, batch, body, key)
                finally:
                    if change == 'mode' and touched:
                        mode.disable()
                assert result.status_code in (403, 409), result.data
                assert not SecondReviewTask.objects.filter(idempotency_key=key).exists()
                assert self.dispatch(source, batch, body, key)[0].status_code == 201

    def test_E08_exact_two_target_passes_and_zero_on_recovery(self):
        source, batch = self.prepared()
        body, key = self.dispatch_body(source, batch), uuid4()
        with patch.object(origin, 'load', wraps=origin.load) as passes:
            result, _ = self.dispatch(source, batch, body, key)
        assert result.status_code == 201 and passes.call_count == 2
        pk = result.data['receipt']['task_id']
        with patch.object(origin, 'load', wraps=origin.load) as passes:
            result, _ = self.dispatch(source, batch, body, key)
        assert result.status_code == 200 and passes.call_count == 0
        self.client.force_authenticate(self.supervisor)
        with patch.object(origin, 'load', wraps=origin.load) as passes:
            assert self.client.get(self.second_base + pk + '/source/').status_code == 200
        assert passes.call_count == 2

    def test_E08_physical_metadata_union_and_zero_live_recovery_sql(self):
        import json

        from review_batches.second_review_bounded import Budget

        source, batch = self.prepared()
        body, key = self.dispatch_body(source, batch), uuid4()
        rows, first_reasons, sql = [], [], []
        bounded_rows, load = Budget.rows, origin.load

        def materialize(budget, query, limits, **kwargs):
            before = budget.stored, budget.canonical
            result = bounded_rows(budget, query, limits, **kwargs)
            rows.append(
                dict(
                    table=query.model._meta.db_table,
                    columns=sorted(limits),
                    stored=budget.stored - before[0],
                    canonical=budget.canonical - before[1],
                )
            )
            return result

        def targeted(*args, **kwargs):
            value = load(*args, **kwargs)
            first_reasons.append(
                dict(
                    stored=value['row'].ner_reason_total + value['row'].ner_review_timestamp_size,
                    canonical=c.bounded(value['row'].review.reason)
                    + c.bounded(origin.utc(value['row'].review.created_at)),
                    snapshot_stored=value['row'].ner_row_size,
                    snapshot_canonical=c.bounded(value['row'].result_snapshot),
                )
            )
            return value

        def execute(fn, statement, params, many, context):
            if statement.lstrip().upper().startswith('SELECT'):
                sql.append(statement)
            return fn(statement, params, many, context)

        with patch.object(Budget, 'rows', materialize), patch.object(
            origin, 'load', targeted
        ), connection.execute_wrapper(execute):
            result, _ = self.dispatch(source, batch, body, key)
        assert result.status_code == 201, result.data
        assert len(first_reasons) == 2
        assert any('receipt' in r['columns'] and r['table'].endswith('secondreviewtask') for r in rows)
        assert any('cancellation' in r['columns'] for r in rows)
        assert any(r['table'].endswith('secondreviewdecision') for r in rows)
        metadata_stored = sum(r['stored'] for r in rows) + sum(r['stored'] for r in first_reasons)
        metadata_canonical = sum(r['canonical'] for r in rows) + sum(r['canonical'] for r in first_reasons)
        assert metadata_stored <= 64 * c.MIB and metadata_canonical <= 16 * c.MIB
        assert sum(r['snapshot_stored'] for r in first_reasons) <= 4 * c.MIB
        assert sum(r['snapshot_canonical'] for r in first_reasons) <= 512 * 1024
        assert len([q for q in sql if 'ner_snapshot' in q]) == 2
        sql.clear()
        rows.clear()
        first_reasons.clear()
        with patch.object(Budget, 'rows', materialize), patch.object(
            origin, 'load', targeted
        ), connection.execute_wrapper(execute):
            replay, _ = self.dispatch(source, batch, body, key)
        assert replay.status_code == 200 and not first_reasons
        assert rows and all(
            r['table'] in ('review_batches_secondreviewtask', 'review_batches_secondreviewdecision') for r in rows
        )
        assert not any(
            any(
                name in query
                for name in (
                    'tasks_submission',
                    'tasks_reviewdecision',
                    'review_batches_reviewbatch',
                    'review_batches_reviewconfidenceobservation',
                    'result_snapshot',
                    '"proof"',
                )
            )
            for query in sql
        )
        print(
            'SECOND_REVIEW_PHYSICAL_BUDGET',
            json.dumps(
                dict(
                    metadata_stored=metadata_stored,
                    metadata_canonical=metadata_canonical,
                    target_passes=2,
                    recovery_target_passes=0,
                    recovery_metadata_reads=rows,
                    recovery_selects=len(sql),
                    scorer_calls=0,
                )
            ),
        )
        pk = result.data['receipt']['task_id']
        opinion_body, opinion_key = self.opinion_body(pk), uuid4()
        sql.clear()
        rows.clear()
        first_reasons.clear()
        with patch.object(Budget, 'rows', materialize), patch.object(
            origin, 'load', targeted
        ), connection.execute_wrapper(execute):
            decision = self.decide(pk, opinion_body, opinion_key)
        assert decision.status_code == 201 and len(first_reasons) == 2
        assert any('proof' in r['columns'] for r in rows)
        assert sum(r['stored'] for r in rows) + sum(r['stored'] for r in first_reasons) <= 64 * c.MIB
        assert sum(r['canonical'] for r in rows) + sum(r['canonical'] for r in first_reasons) <= 16 * c.MIB
        sql.clear()
        rows.clear()
        first_reasons.clear()
        with patch.object(Budget, 'rows', materialize), patch.object(
            origin, 'load', targeted
        ), connection.execute_wrapper(execute):
            repeated = self.decide(pk, opinion_body, opinion_key)
        assert repeated.status_code == 200 and not first_reasons
        assert not any(
            any(
                name in query
                for name in (
                    'tasks_submission',
                    'tasks_reviewdecision',
                    'review_batches_reviewbatch',
                    'review_batches_reviewconfidenceobservation',
                    'result_snapshot',
                    '"proof"',
                )
            )
            for query in sql
        )
