import copy
import io
import json
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.db.models import JSONField
from django.db.models.query import QuerySet
from rest_framework.exceptions import APIException
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APITestCase
from review_batches import second_review_contract as c
from review_batches import second_review_origin as origin
from review_batches.models import ReviewBatch, SecondReviewTask
from review_batches.second_review_api import SecondReviewJSONParser
from review_batches.second_review_bounded import Budget
from tasks.models import ReviewDecision, Submission
from tasks.submissions import _snapshot_hash

from .second_review_fixtures import SecondReviewFixtures


class TestSecondReviewBudgets(SecondReviewFixtures, APITestCase):
    def setUp(self):
        self.second_setup()

    def fake_batch(self, plan, principal='synthetic'):
        return ReviewBatch.objects.create(
            project=self.project,
            created_by=self.manager,
            actor_id_frozen=self.manager.pk,
            creator_principal=principal,
            name='budget',
            idempotency_key=uuid4(),
            request_hash='a' * 64,
            plan=plan,
            plan_hash='b' * 64,
        )

    def test_E01_snapshot_canonical_exact_and_one_multibyte_byte_over(self):
        source, batch = self.prepared()
        # Keep the frozen candidate binding by creating a batch after padding.
        snapshot = copy.deepcopy(source.result_snapshot)
        snapshot['padding'] = ''
        maximum = 256 * 1024
        snapshot['padding'] = 'x' * (maximum - c.bounded(snapshot))
        assert c.bounded(snapshot, maximum) == maximum
        Submission.objects.filter(pk=source.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        from review_batches import service

        ctx = c.guard(
            self.project.pk,
            __import__('access_control.project_operations', fromlist=['local_principal']).local_principal(
                self.manager
            ),
            self.manager,
            'organize',
        )
        # Direct shared loader tests the byte boundary before candidate validation.
        from classification_disagreements.snapshot_loading import load

        assert (
            len(
                load(
                    Submission.objects.filter(pk=source.pk),
                    policy=origin.SnapshotPolicy,
                    validate=service.valid_source,
                    projection='review',
                )
            )
            == 1
        )
        snapshot['padding'] += '中'
        Submission.objects.filter(pk=source.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        with self.assertRaises(APIException) as raised:
            origin.load(ctx, batch, source.pk)
        assert raised.exception.status_code == 422

    def test_E01_sql_stored_limit_is_checked_before_snapshot_decoder(self):
        source, batch = self.prepared()
        snapshot = {**source.result_snapshot, 'oversize-marker': 'x' * (2 * c.MIB)}
        Submission.objects.filter(pk=source.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        original, seen = JSONField.from_db_value, []

        def decode(field, value, expression, db):
            if value and 'oversize-marker' in str(value):
                seen.append(value)
            return original(field, value, expression, db)

        with patch.object(JSONField, 'from_db_value', decode):
            result = self.context(source, batch)
        assert result.status_code == 422 and not seen

    def test_E02_metadata_multicolumn_and_union_exact_budgets(self):
        row = self.fake_batch({'padding': 'x' * 100})
        limits = {'plan': (1024, 4096)}
        budget = Budget(canonical=8192, stored=8192)
        rows = budget.rows(ReviewBatch.objects.filter(pk=row.pk), limits)
        assert len(rows) == 1 and budget.stored >= 100 and budget.canonical >= 100
        with self.assertRaises(APIException) as raised:
            Budget(canonical=8192, stored=99).rows(ReviewBatch.objects.filter(pk=row.pk), limits)
        assert raised.exception.status_code == 422
        self.fake_batch({'padding': 'x' * 100})
        with self.assertRaises(APIException):
            Budget(canonical=150, stored=8192).rows(ReviewBatch.objects.filter(name='budget'), limits)
        # The row union is separate from the total, including all repeated scalars.
        with self.assertRaises(APIException):
            Budget().rows(ReviewBatch.objects.filter(pk=row.pk), limits, union=(99, 4096))

    def test_E02_sqlite_does_not_trust_varchar_length_for_metadata(self):
        if connection.vendor != 'sqlite':
            self.skipTest('PostgreSQL already enforces varchar(n)')
        row = self.fake_batch({}, principal='x' * (2 * c.MIB))
        decoded = []
        original = JSONField.from_db_value

        def decode(field, value, expression, db):
            decoded.append(value)
            return original(field, value, expression, db)

        with patch.object(JSONField, 'from_db_value', decode), self.assertRaises(APIException):
            Budget().rows(ReviewBatch.objects.filter(pk=row.pk), {'plan': (1024, 4096)})
        assert not decoded

    def test_E03_first_review_reason_full_escaped_unicode_and_overflow(self):
        source, batch = self.prepared(reason='完整 Initial\n"reason"')
        result, _ = self.dispatch(source, batch)
        assert result.status_code == 201
        self.client.force_authenticate(self.supervisor)
        result = self.client.get(self.second_base + result.data['receipt']['task_id'] + '/source/')
        assert result.status_code == 200 and result.data['first_review']['reason'] == '完整 Initial\n"reason"'
        for reason in ('x' * 8193, '中' * 2731, '\\' * 5000, 'x' * 32769):
            ReviewDecision.objects.filter(submission=source).update(reason=reason)
            assert self.context(source, batch).status_code == 422
        for reason in ('', ' \n ', '\t'):
            with self.assertRaises(APIException):
                c.parse(
                    dict(
                        expected_version=1,
                        expected_source_hash='a' * 64,
                        outcome='concern',
                        reason=reason,
                        expected_viewer_hash='b' * 64,
                    ),
                    'decide',
                )

    def test_E04_actual_select_gates_late_snapshot_and_text_reason_growth(self):
        source, batch = self.prepared()
        original_iter = QuerySet.iterator
        original_decode = JSONField.from_db_value
        for kind in ('snapshot', 'reason'):
            touched, decoded = [], []
            saved = copy.deepcopy(source.result_snapshot)

            def iterator(query, *args, **kwargs):
                if query.model is Submission and 'ner_snapshot' in query.query.annotations and not touched:
                    touched.append(True)
                    if kind == 'snapshot':
                        huge = {**saved, 'late-growth-marker': 'x' * (2 * c.MIB)}
                        Submission.objects.filter(pk=source.pk).update(
                            result_snapshot=huge, result_hash=_snapshot_hash(huge)
                        )
                    else:
                        ReviewDecision.objects.filter(submission=source).update(
                            reason='late-growth-marker' + 'x' * 32769
                        )
                return original_iter(query, *args, **kwargs)

            def decode(field, value, expression, db):
                if value and 'late-growth-marker' in str(value):
                    decoded.append(value)
                return original_decode(field, value, expression, db)

            with patch.object(QuerySet, 'iterator', iterator), patch.object(JSONField, 'from_db_value', decode):
                result = self.context(source, batch)
            assert result.status_code == 422 and touched and not decoded, (kind, result.data)
            Submission.objects.filter(pk=source.pk).update(result_snapshot=saved, result_hash=source.result_hash)
            ReviewDecision.objects.filter(submission=source).update(reason='Initial 初审')

    def test_E04_metadata_union_late_growth_is_gated_before_decoder(self):
        self.fake_batch({'safe': True})
        original, decode, touched, seen = QuerySet.iterator, JSONField.from_db_value, [], []

        def iterator(query, *args, **kwargs):
            if query.model is ReviewBatch and '_sr_value_plan' in query.query.annotations and not touched:
                touched.append(True)
                # Existing batches are frozen: inject a fresh conflicting row in
                # the broad test union after preflight instead of mutating history.
                self.fake_batch({'metadata-growth-marker': 'x' * 5000})
            return original(query, *args, **kwargs)

        def decoder(field, value, expression, db):
            if value and 'metadata-growth-marker' in str(value):
                seen.append(value)
            return decode(field, value, expression, db)

        with patch.object(QuerySet, 'iterator', iterator), patch.object(
            JSONField, 'from_db_value', decoder
        ), self.assertRaises(APIException):
            Budget(stored=4096).rows(ReviewBatch.objects.filter(name='budget'), {'plan': (1024, 4096)})
        assert touched and not seen

    def test_E05_actual_response_wrapper_overflow_and_mutation_roll_back(self):
        source, batch = self.prepared()
        body, key = self.dispatch_body(source, batch), uuid4()
        original = JSONRenderer.render
        for mode in ('overflow', 'representation'):

            def render(renderer, data, *args, **kwargs):
                output = original(renderer, data, *args, **kwargs)
                if isinstance(data, dict) and data.get('receipt', {}).get('command') == 'dispatch':
                    return (
                        output + b' ' * c.MAX_OUTPUT
                        if mode == 'overflow'
                        else output.replace(b'"replayed":false', b'"replayed":true')
                    )
                return output

            with patch.object(JSONRenderer, 'render', render):
                result, _ = self.dispatch(source, batch, body, key)
            assert result.status_code in (409, 422) and not SecondReviewTask.objects.exists()
        assert self.dispatch(source, batch, body, key)[0].status_code == 201

    def test_E06_real_stream_chunked_short_reads_duplicate_and_oversize(self):
        parser = SecondReviewJSONParser()

        class Short(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 7))

        assert parser.parse(Short(b'{"a":"b"}')) == {'a': 'b'}
        for raw in (b'', b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'\xff', b' ' * (c.MAX_BODY + 1)):
            with self.assertRaises(APIException):
                parser.parse(Short(raw))
        # Claimed Content-Length is not a size authority for parser streams.
        with self.assertRaises(APIException) as raised:
            parser.parse(
                Short(b' ' * c.MAX_BODY + b'{}'),
                parser_context={'request': type('Request', (), {'META': {'CONTENT_LENGTH': '1'}})()},
            )
        assert raised.exception.status_code == 422

    def test_E07_page_100_101_corrupt_expired_and_scope_bound_cursor(self):
        source, batch = self.prepared()
        self.client.force_authenticate(self.manager)
        base = self.base + f'{batch}/sources/{source.pk}/second-review-assignees/'
        assert self.client.get(base, {'page_size': 100}).status_code == 200
        assert self.client.get(self.second_base, {'page_size': 100}).status_code == 200
        for params in ({'page_size': 101}, {'page_size': True}, {'page_size': '1.0'}, {'cursor': 'tampered'}):
            assert self.client.get(base, params).status_code == 400
            assert self.client.get(self.second_base, params).status_code == 400
        context = self.context(source, batch).data
        assignees = dict(
            project=self.project.pk, source=context['source_hash'], viewer=context['viewer_hash'], order='id'
        )
        history = dict(project=self.project.pk, viewer=context['viewer_hash'], order='created_at,id')
        for endpoint, binding in ((base, assignees), (self.second_base, history)):
            token = c.cursor(binding, 0)
            assert self.client.get(endpoint, {'cursor': token}).status_code == 200
            with patch('django.core.signing.time.time', return_value=1):
                expired = c.cursor(binding, 0)
            assert self.client.get(endpoint, {'cursor': expired}).status_code == 400
            for field in binding:
                wrong = {**binding, field: self.project.pk + 1 if field == 'project' else 'changed'}
                assert self.client.get(endpoint, {'cursor': c.cursor(wrong, 0)}).status_code == 400

    def test_E02_unused_legacy_sqlite_datetime_and_uuid_are_not_projected(self):
        if connection.vendor != 'sqlite':
            self.skipTest('PostgreSQL has native timestamp/UUID types')
        source, batch_id = self.prepared()
        original = ReviewBatch.objects.get(pk=batch_id)
        names = [f.column for f in original._meta.fields if not f.primary_key]
        values = []
        for f in original._meta.fields:
            if f.primary_key:
                continue
            value = getattr(original, f.attname)
            if f.name in ('created_at', 'idempotency_key'):
                value = 'unused-legacy-scalar-marker' + 'x' * (2 * c.MIB)
            elif f.name == 'plan':
                value = json.dumps(value)
            values.append(value)
        quoted = ','.join(connection.ops.quote_name(n) for n in names)
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO review_batches_reviewbatch ('
                + quoted
                + ') VALUES ('
                + ','.join(['%s'] * len(values))
                + ')',
                values,
            )
            bad_id = cursor.lastrowid
        selects = []

        def trace(execute, sql, params, many, context):
            if '_sr_value_plan' in sql:
                selects.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(trace):
            response = self.context(source, bad_id)
        assert response.status_code == 200, response.data
        assert selects and all('"created_at"' not in query and '"idempotency_key"' not in query for query in selects)

    def test_E04_sqlite_initial_timestamp_growth_is_gated_in_hydrating_select(self):
        if connection.vendor != 'sqlite':
            self.skipTest('PostgreSQL native timestamp cannot contain oversized text')
        from django.db.models.expressions import RawSQL

        source, batch = self.prepared()
        original, touched = QuerySet.iterator, []

        def iterator(query, *args, **kwargs):
            if query.model is Submission and 'ner_snapshot' in query.query.annotations and not touched:
                touched.append(True)
                ReviewDecision.objects.filter(submission=source).update(created_at=RawSQL('%s', ['x' * (2 * c.MIB)]))
            return original(query, *args, **kwargs)

        with patch.object(QuerySet, 'iterator', iterator):
            result = self.context(source, batch)
        assert result.status_code == 422 and touched, result.data

    def test_E02_sqlite_integer_affinity_cannot_hydrate_oversized_or_float_actor(self):
        if connection.vendor != 'sqlite':
            self.skipTest('PostgreSQL integer columns enforce their physical type')
        source, batch_id = self.prepared()
        original = ReviewBatch.objects.get(pk=batch_id)
        fields = [f for f in original._meta.fields if not f.primary_key]
        for bad_actor in ('oversize-integer-marker' + 'x' * (2 * c.MIB), 1.5):
            values = []
            for field in fields:
                value = getattr(original, field.attname)
                if field.name == 'actor_id_frozen':
                    value = bad_actor
                elif field.name == 'idempotency_key':
                    value = uuid4().hex
                elif field.name == 'created_at':
                    value = value.isoformat()
                elif field.name == 'plan':
                    value = json.dumps(value)
                values.append(value)
            names = ','.join(connection.ops.quote_name(f.column) for f in fields)
            with connection.cursor() as cursor:
                cursor.execute(
                    'INSERT INTO review_batches_reviewbatch ('
                    + names
                    + ') VALUES ('
                    + ','.join(['%s'] * len(values))
                    + ')',
                    values,
                )
                pk = cursor.lastrowid
            decoded = []
            decoder = JSONField.from_db_value

            def decode(field, value, expression, db):
                if value is not None:
                    decoded.append(value)
                return decoder(field, value, expression, db)

            with patch.object(JSONField, 'from_db_value', decode), self.assertRaises(APIException):
                Budget().rows(
                    ReviewBatch.objects.filter(pk=pk).only('pk', 'actor_id_frozen', 'plan'),
                    {'plan': (4 * c.MIB, 16 * c.MIB)},
                )
            assert not decoded
