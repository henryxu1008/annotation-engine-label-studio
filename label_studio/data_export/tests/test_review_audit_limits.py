"""Count/byte intersections and regression vectors from independent review."""
import copy
import io
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from uuid import uuid4

from data_export import approved, audit_files, audit_format, audit_source
from data_export import audit_contract as c
from data_export import review_audit as audit
from data_export.audit_models import AuditPackage
from django.db import DatabaseError, transaction
from rest_framework.exceptions import APIException, AuthenticationFailed
from rest_framework.test import APITestCase
from review_batches.second_review_bounded import Budget
from tasks.models import Submission, Task
from tasks.submissions import _snapshot_hash, review_submission

from .test_review_audit import AuditFixtures, denied


class TestAuditLimits(AuditFixtures, APITestCase):
    def setUp(self):
        self.audit_setup()

    def test_B08_local_provider_subclass_without_background_hook_is_unsupported(self):
        from access_control.identity import LocalIdentityProvider

        class Custom(LocalIdentityProvider):
            pass

        with patch.object(audit, 'get_identity_provider', return_value=Custom()):
            denied(self.client.get(self.audit_base + 'capabilities/'), (503,))
        assert not AuditPackage.objects.exists()

    def test_B06_custom_local_subclass_cannot_bypass_creator_revocation_hook(self):
        from access_control.identity import LocalIdentityProvider

        called = []

        class Custom(LocalIdentityProvider):
            def resolve_background(self, principal):
                called.append(principal)
                error = AuthenticationFailed('PII_PROVIDER_REVOKE_CANARY')
                error.display_context = {'reason': 'PII_PROVIDER_CONTEXT_CANARY'}
                raise error

        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        with patch.object(audit.approved, 'api_queue', return_value=MagicMock()):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        self.client.force_authenticate(self.producer)
        with patch.object(audit, 'get_identity_provider', return_value=Custom()):
            with self.assertLogs('core.utils.common', level='ERROR') as logs:
                response = self.client.get(self.audit_base + f'{package.pk}/')
            denied(response, (401, 403, 404))
            assert 'PII_PROVIDER_REVOKE_CANARY' not in str(response.data)
            assert 'PII_PROVIDER_REVOKE_CANARY' not in '\n'.join(logs.output)
            assert 'PII_PROVIDER_CONTEXT_CANARY' not in str(response.data)
            import traceback

            assert all(
                'PII_PROVIDER' not in ''.join(traceback.format_exception(*record.exc_info)) for record in logs.records
            )
            caps = self.client.get(self.audit_base + 'capabilities/')
            assert caps.status_code == 200 and caps.data['collection_supported'] is False
            denied(self.client.get(self.audit_base), (503,))
            audit.generate(str(package.pk), str(package.token))
        package.refresh_from_db()
        assert package.state == 'blocked_auth' and called
        assert set(called) == {package.creator_principal} and not hasattr(package, 'artifact')

    def test_G08_unknown_provider_exception_and_fixed_code_context_never_reach_logs(self):
        import traceback

        from access_control.identity import LocalIdentityProvider
        from rest_framework.exceptions import PermissionDenied

        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        fixed = PermissionDenied('AUDIT_FORBIDDEN')
        fixed.display_context = {'reason': 'PII_UNKNOWN_PROVIDER_CONTEXT_CANARY'}
        for error in (RuntimeError('PII_UNKNOWN_PROVIDER_CANARY'), OSError('PII_UNKNOWN_PROVIDER_CANARY'), fixed):

            class Custom(LocalIdentityProvider):
                def resolve_background(self, principal):
                    raise error

            with patch.object(audit, 'get_identity_provider', return_value=Custom()), self.assertLogs(
                'core.utils.common', level='ERROR'
            ) as logs:
                response = self.client.get(self.audit_base + f'{package.pk}/')
            assert response.status_code == (403 if error is fixed else 500)
            assert 'X-Audit-Command-State' not in response and 'PII_UNKNOWN_PROVIDER' not in str(response.data)
            assert 'PII_UNKNOWN_PROVIDER' not in '\n'.join(logs.output)
            assert all(
                'PII_UNKNOWN_PROVIDER' not in ''.join(traceback.format_exception(*record.exc_info))
                for record in logs.records
            )

    def test_G08_unknown_custom_provider_failure_cannot_escape_to_rq(self):
        from access_control.identity import LocalIdentityProvider

        class Custom(LocalIdentityProvider):
            def resolve_background(self, principal):
                raise RuntimeError('PII_RQ_PROVIDER_CANARY')

        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        with patch.object(audit.approved, 'api_queue', return_value=MagicMock()):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        with patch.object(audit, 'get_identity_provider', return_value=Custom()):
            audit.generate(str(package.pk), str(package.token))
        package.refresh_from_db()
        assert package.state == 'failed_final' and package.error_code == 'AUDIT_GENERATION_FAILED'
        assert not hasattr(package, 'artifact')

    def test_B08_missing_background_and_collection_contract_fail_closed(self):
        class Missing:
            pass

        with patch.object(audit, 'get_identity_provider', return_value=Missing()):
            denied(self.client.get(self.audit_base + 'capabilities/'), (503,))

        class Background:
            def resolve_background(self, principal):
                return None

        with patch.object(audit, 'get_identity_provider', return_value=Background()):
            caps = self.client.get(self.audit_base + 'capabilities/')
            assert caps.status_code == 200 and caps.data['collection_supported'] is False
            denied(self.client.get(self.audit_base), (503,))

    def test_A07_H02_real_multi_text_input_and_retained_only_proof_rejected(self):
        row = self.make_source()
        snapshot = copy.deepcopy(row.result_snapshot)
        snapshot['project']['label_config'] = snapshot['project']['label_config'].replace(
            '</View>',
            '<Text name="other" value="$other"/><Choices name="second" toName="other"><Choice value="A"/></Choices></View>',
        )
        snapshot['task']['data']['other'] = 'second static text'
        snapshot['annotation']['result'].append(
            dict(id='other', from_name='second', to_name='other', type='choices', value={'choices': ['A']})
        )
        Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        row.refresh_from_db()
        args = {k: v for k, v in self.args.items() if k != 'priority'}
        preview = self.preview(args)
        assert preview.status_code == 200, preview.data
        batch = self.create_batch({**args, 'expected_plan_hash': preview.data['plan_hash']}).data['id']
        review_submission(submission=row, reviewer=self.reviewer, decision='approved', reason='synthetic')
        row.refresh_from_db()
        denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (400,))
        assert self.dispatch(row, batch)[0].status_code == 201
        row.delete()
        denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (422,))
        assert not AuditPackage.objects.exists()

    def test_H02_legacy_single_text_receipt_without_arity_evidence_is_unprovable(self):
        row, batch = self.prepared()
        assert self.dispatch(row, batch)[0].status_code == 201
        row.delete()
        denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (422,))

    def test_E06_weak_snapshot_scope_ids_cannot_pass_numeric_equality(self):
        source, batch = self.prepared(decision='rejected')
        successor = self.successor(source)
        snapshot = copy.deepcopy(successor.result_snapshot)
        snapshot['project']['id'] = float(self.project.pk)
        Submission.objects.filter(pk=successor.pk).update(
            result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot)
        )
        denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (409,))

    def test_A03_json_number_boolean_input_types_are_not_equal(self):
        source, batch = self.prepared(decision='rejected')
        for value in (1, True, 1.0):
            source.assignment.task.data = {**source.assignment.task.data, 'typed': value}
            source.assignment.task.save(update_fields=['data'])
            source = self.successor(source)
        assert self.capture(batch).status_code == 202
        records = c.strict(AuditPackage.objects.get().projection)['records']
        diffs = [row for row in records if row['type'] == 'difference']
        assert len(diffs) == 3 and all(row['state'] == 'unavailable' for row in diffs)

    def test_A01_confidence_selection_reason_is_the_frozen_protocol_enum(self):
        source, batch = self.prepared(confidence=True)
        assert self.capture(batch).status_code == 202
        projection = json.loads(AuditPackage.objects.get().projection)
        root = next(r for r in projection['records'] if r['type'] == 'candidate')
        assert root['selection_reason'] == 'frozen_low_confidence'

    def test_E05_counts_and_duplicate_edges_are_not_just_well_typed(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        original = json.loads(AuditPackage.objects.get().projection)
        for kind in ('counts', 'edge', 'candidate'):
            bad = copy.deepcopy(original)
            if kind == 'counts':
                bad['counts']['revision'] += 1
            elif kind == 'edge':
                bad['records'].append(next(r for r in bad['records'] if r['type'] == 'edge'))
            else:
                bad['records'] = [r for r in bad['records'] if r['type'] != 'candidate']
                bad['counts'].pop('candidate')
            with self.assertRaises(APIException):
                audit_format.projection(c.canonical(bad).decode())

    def test_H03_actual_db_count_boundaries_are_an_intersection(self):
        source, batch = self.prepared(decision='rejected')
        self.successor(source)
        for limits, expected in (
            ({'roots': 1, 'assignments': 1, 'revisions': 2, 'per_assignment': 2}, 200),
            ({'revisions': 1}, 422),
            ({'per_assignment': 1}, 422),
        ):
            with self.settings(ANNOTATION_REVIEW_AUDIT_LIMITS=limits):
                result = self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json')
                assert result.status_code == expected, result.data

    def test_H04_literal_entity_edge_and_cohort_count_caps(self):
        g = audit_source.Graph(Budget())
        g.reserve_entities(5000)
        with self.assertRaises(APIException):
            g.reserve_entities(1)
        a = g.add('batch', 1, {})
        b = g.add('candidate', 1, {})
        for i in range(10000):
            g.link(a, b, str(i))
        with self.assertRaises(APIException):
            g.link(a, b, '10001')
        assert c.DEFAULT_LIMITS['cohort'] == 2000 and c.DEFAULT_LIMITS['chunk'] == 250

    def test_H05_snapshot_union_is_not_reset_by_chunking(self):
        source, batch = self.prepared(decision='rejected')
        later = self.successor(source)
        # Individually below256KiB, jointly above512KiB with a third revision.
        last = self.successor(later)
        for row in (source, later, last):
            snapshot = copy.deepcopy(row.result_snapshot)
            snapshot['padding'] = 'x' * (180 * 1024)
            Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        for chunk in (1, 250):
            with self.settings(ANNOTATION_REVIEW_AUDIT_LIMITS={'chunk': chunk}):
                denied(
                    self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (422,)
                )

    def test_H06_real_shared_budget_equal_and_one_byte_over(self):
        source, batch = self.prepared()
        measured = Budget()
        audit_source.load(self.project, batch, measured)
        audit_source.load(self.project, batch, measured)
        for limits, expected in (
            ({'canonical': measured.canonical, 'stored': measured.stored}, 200),
            ({'canonical': measured.canonical - 1}, 422),
            ({'stored': measured.stored - 1}, 422),
        ):
            with self.settings(ANNOTATION_REVIEW_AUDIT_LIMITS=limits):
                response = self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json')
                assert response.status_code == expected, response.data

    def test_H07_capped_writer_checks_footer_and_exact_boundary(self):
        stream = io.BytesIO()
        writer = audit_format.CappedWriter(stream, 4)
        assert writer.write(b'abcd') == 4
        with self.assertRaises(APIException):
            writer.write(b'e')
        assert stream.getvalue() == b'abcd'

    def test_H01_chunked_short_reads_obey_exact_transport_byte_boundary(self):
        from data_export.audit_api import AuditJSONParser

        class ShortReader:
            def __init__(self, value):
                self.value = value

            def read(self, maximum):
                result, self.value = self.value[: min(7, maximum)], self.value[min(7, maximum) :]
                return result

        for stream in (io.BytesIO, ShortReader):
            raw = b'{"review_batch_id":1}'
            assert AuditJSONParser().parse(stream(raw + b' ' * (16384 - len(raw)))) == {'review_batch_id': 1}
            with self.assertRaises(APIException):
                AuditJSONParser().parse(stream(raw + b' ' * (16385 - len(raw))))

    def test_H03_twenty_revisions_are_complete_and_twenty_first_refuses(self):
        source, batch = self.prepared(decision='rejected')
        for _ in range(19):
            source = self.successor(source)
        result = self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json')
        assert result.status_code == 200, result.data
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        assert c.strict(package.summary_record)['counts']['revision'] == 20
        self.successor(source)
        denied(self.client.post(self.audit_base + 'preview/', dict(review_batch_id=batch), format='json'), (422,))
        assert AuditPackage.objects.count() == 1

    def test_F07_ready_bytes_keep_a_scratch_reservation(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = self.ready()
        lease = audit_files.ScratchLease(package, str(uuid4()))
        with self.assertRaises(APIException):
            audit_files.ScratchLease(package, str(uuid4()))
        assert len(list(lease.parent.glob('.scratch-*'))) == 2
        lease.release()
        assert len(list(lease.parent.glob('.scratch-*'))) == 1

    def test_J06_queryset_mutation_does_not_re_evaluate_growing_predicate(self):
        from data_export import audit_deletion

        source, batch = self.prepared()
        original = audit_deletion.coordinated
        late = []

        @contextmanager
        def grow(query, kind, **kwargs):
            with original(query, kind, **kwargs) as fixed:
                late.append(Task.objects.create(project=self.project, data={'text': 'phantom'}))
                yield fixed

        with patch.object(audit_deletion, 'coordinated', grow):
            Task.objects.filter(project=self.project).delete()
        assert Task.objects.filter(pk=late[0].pk).exists()

    def test_G08_worker_exception_detail_never_becomes_public_error_code(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        package = AuditPackage.objects.get()
        with patch.object(approved, 'api_queue', return_value=MagicMock()):
            self.real_enqueue(str(package.pk))
        package.refresh_from_db()
        with patch.object(audit, 'creator', side_effect=AuthenticationFailed('PII_WORKER_DETAIL_CANARY')):
            audit.generate(str(package.pk), str(package.token))
        package.refresh_from_db()
        assert package.error_code == 'AUDIT_CREATOR_REVOKED'
        response = self.client.get(self.audit_base + f'{package.pk}/')
        assert response.status_code == 200 and 'PII_WORKER_DETAIL_CANARY' not in str(response.data)

    def test_J01_birth_scope_json_types_and_error_allowlist_are_db_guarded(self):
        source, batch = self.prepared()
        assert self.capture(batch).status_code == 202
        original = AuditPackage.objects.get()
        values = {f.attname: getattr(original, f.attname) for f in AuditPackage._meta.fields}
        identity = uuid4()
        bundle = json.loads(values['input_bundle'])
        projection = json.loads(values['projection'])
        bundle['package_id'] = projection['package_id'] = str(identity)
        bundle['project_id'] = float(self.project.pk)
        values.update(
            id=identity,
            idempotency_key=uuid4(),
            input_bundle=c.exact(bundle).decode(),
            projection=c.canonical(projection).decode(),
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            AuditPackage.objects.create(**values)
        with self.assertRaises(DatabaseError), transaction.atomic():
            AuditPackage.objects.filter(pk=original.pk).update(error_code='PII_RAW_ERROR')
