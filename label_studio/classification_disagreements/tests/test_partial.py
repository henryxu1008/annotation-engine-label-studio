import copy
from fractions import Fraction
from unittest.mock import patch

from classification_disagreements import batches, partial, service
from classification_disagreements.models import ClassificationBatch
from classification_disagreements.tests import test_batches as fixtures
from django.core import signing
from django.test import override_settings
from django.utils import timezone
from organizations.models import OrganizationMember
from rest_framework.test import APITestCase
from tasks.models import Submission
from tasks.submissions import _snapshot_hash


@override_settings(ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED=True,
                   ANNOTATION_CLASSIFICATION_BATCHES_ENABLED=True,
                   ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True)
class TestPartial(APITestCase):
    setUpTestData = classmethod(fixtures.TestClassificationBatches.setUpTestData.__func__)
    make_source = fixtures.TestClassificationBatches.make_source
    set_answer = fixtures.TestClassificationBatches.set_answer
    preview = fixtures.TestClassificationBatches.preview
    body = fixtures.TestClassificationBatches.body
    create = fixtures.TestClassificationBatches.create
    new_batch = fixtures.TestClassificationBatches.new_batch

    def setUp(self):
        fixtures.TestClassificationBatches.setUp(self)
        self.manual = self.base.replace('batches/', '') + f'tasks/{self.first.assignment.task_id}/'
        self.url = self.manual + 'partial-match/'
        self.multiple(self.first, ['A', 'B'])
        self.multiple(self.second, ['B', 'C'])

    def multiple(self, row, labels, *, second=None):
        snapshot = copy.deepcopy(row.result_snapshot)
        config = ('<View><Text name="text" value="$text"/><Choices name="sentiment" toName="text" choice="multiple">'
                  '<Choice value="A"/><Choice value="B"/><Choice value="C"/></Choices>')
        result = [{'from_name': 'sentiment', 'to_name': 'text', 'type': 'choices', 'value': {'choices': labels}}]
        if second is not None:
            config += '<Choices name="other" toName="text"><Choice value="A"/><Choice value="B"/></Choices>'
            result.append({'from_name': 'other', 'to_name': 'text', 'type': 'choices', 'value': {'choices': second}})
        snapshot['project']['label_config'] = config + '</View>'
        snapshot['annotation']['result'] = result
        Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        row.refresh_from_db()

    def source_receipt(self, rows=None):
        response = self.client.get(self.manual, {'submissions': ','.join(str(r.pk) for r in (rows or [self.first, self.second]))})
        assert response.status_code == 200, response.data
        return response.data

    def explain(self, frozen=None, source=None):
        return self.client.get(self.url, {'frozen': frozen} if frozen else {'source_frozen': source or self.source_receipt()['frozen']})

    def batch_url(self, batch):
        return f'{self.base}{batch.pk}/items/{self.first.assignment.task_id}/partial-match/'

    def test_jaccard_explains_sets_without_changing_exact_agreement(self):
        before = self.source_receipt()
        response = self.explain(source=before['frozen'])
        assert response.status_code == 200, response.data
        data = response.data
        assert data['base']['agreement'] == before['agreement'] == 0
        assert data['partial']['score'] == partial.fraction(Fraction(1, 3))
        pair = data['partial']['controls'][0]['pairs'][0]
        assert pair['shared'] == ['B'] and pair['only_left'] == ['A'] and pair['only_right'] == ['C']
        assert (pair['intersection_count'], pair['union_count']) == (1, 3)
        assert response['Cache-Control'] == 'private, no-store'
        assert response['Referrer-Policy'] == 'no-referrer'
        assert self.source_receipt()['locations'] == before['locations']

    def test_three_people_and_equal_weight_controls_have_exact_rational_means(self):
        third = self.make_source(worker=self.manager)
        third.assignment.task = self.first.assignment.task
        third.assignment.save()
        self.set_answer(third, 'Positive')
        self.multiple(third, ['B'])
        data = self.explain(source=self.source_receipt([third, self.second, self.first])['frozen']).data
        assert data['partial']['score'] == partial.fraction(Fraction(4, 9))
        assert data['base']['agreement'] == 0 and len(data['partial']['controls'][0]['pairs']) == 3
        for row, labels in ((self.first, ['A', 'B']), (self.second, ['B', 'C'])):
            self.multiple(row, labels, second=['A'])
        assert self.explain().data['partial']['score'] == partial.fraction(Fraction(2, 3))

    def test_equal_disjoint_and_single_choice(self):
        for left, right, score in ((['B', 'A'], ['A', 'B'], 1), (['A'], ['C'], 0)):
            self.multiple(self.first, left)
            self.multiple(self.second, right)
            assert self.explain().data['partial']['score'] == partial.fraction(Fraction(score))
        for row in (self.first, self.second):
            self.set_answer(row, 'Positive')
        assert self.explain().data['partial']['score'] == partial.fraction(Fraction(1))

    def test_empty_one_or_both_missing_null_cancelled_and_unknown_are_unavailable(self):
        for left, right in (([], ['A']), ([], []), (['A'], []), (['Unknown'], ['A'])):
            self.multiple(self.first, left)
            self.multiple(self.second, right)
            data = self.explain().data
            assert data['partial']['status'] == 'unavailable'
            assert data['partial']['score'] is None and data['partial']['controls'] == []
            assert data['base']['agreement'] is None and data['base']['locations'] == []
        for change in (lambda s: s['annotation'].update(result=[]),
                       lambda s: s['annotation'].update(result=None),
                       lambda s: s['annotation'].update(was_cancelled=True),
                       lambda s: s['annotation']['result'][0].update(value=None)):
            self.set_answer(self.first, 'Positive')
            self.set_answer(self.second, 'Positive', change)
            assert self.explain().data['partial']['score'] is None

    def test_frozen_replay_survives_new_revision_and_masks_deleted_input(self):
        first = self.explain().data
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.set_answer(newer, 'Negative')
        replay = self.explain(first['frozen']).data
        for key in ('origin', 'base', 'partial', 'receipt', 'frozen'):
            assert replay[key] == first[key]
        self.first.delete()
        response = self.explain(first['frozen'])
        assert response.status_code == 200
        assert response.data['partial']['status'] == 'unavailable'
        assert response.data['base']['locations'] == []
        assert response.data['frozen'] == first['frozen']

    def test_signed_receipt_and_source_identity_tampering_fail_closed(self):
        first = self.explain().data
        assert self.explain(first['frozen'] + 'x').status_code == 400
        payload = copy.deepcopy(first['receipt'])
        payload['rule'] = 'classification-jaccard-nonempty/v2'
        assert self.explain(signing.dumps(payload, salt=partial.SCHEMA)).status_code == 400
        payload = copy.deepcopy(first['receipt'])
        payload['origin']['task_id'] += 1
        assert self.explain(signing.dumps(payload, salt=partial.SCHEMA)).status_code == 404
        Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
        assert self.explain(first['frozen']).status_code == 409

    def test_old_batch_manifest_calculation_and_arbitration_identity_do_not_change(self):
        from classification_disagreements.arbitration import input_hash
        batch = self.new_batch()
        before = copy.deepcopy(batch.manifest)
        origin = input_hash(self.project, before['tasks'][0])
        rows_before = list(Submission.objects.order_by('pk').values())
        response = self.client.get(self.batch_url(batch))
        assert response.status_code == 200, response.data
        assert response.data['partial']['score'] == partial.fraction(Fraction(1, 3))
        assert 'manifest_hash' not in response.data['origin']
        replay = self.client.get(self.batch_url(batch), {'frozen': response.data['frozen']})
        assert replay.data['receipt'] == response.data['receipt']
        batch.refresh_from_db()
        assert batch.manifest == before and batch.manifest_hash == batches.digest(before)
        assert input_hash(self.project, batch.manifest['tasks'][0]) == origin
        assert ClassificationBatch.objects.count() == 1
        assert list(Submission.objects.order_by('pk').values()) == rows_before
        _, sources = batches.project_item_sources(batch, self.manager, 'manager', self.first.assignment.task_id)
        rows = [sources[e['source']['submission_id']] for e in before['tasks'][0]['cohort']]
        assert batches.frozen_result(service.calculate(rows, self.project.pk)) == before['tasks'][0]['result']

    def test_batch_missing_participant_never_upgrades_after_completion(self):
        from tasks.models import TaskAssignment
        assignment = TaskAssignment.objects.create(project=self.project, task=self.first.assignment.task, assignee=self.manager)
        batch = self.new_batch()
        first = self.client.get(self.batch_url(batch)).data
        assert first['partial']['reason'] == 'missing_submission'
        row = self.make_source(assignment=assignment, worker=self.manager)
        self.set_answer(row, 'Positive')
        self.multiple(row, ['A'])
        replay = self.client.get(self.batch_url(batch), {'frozen': first['frozen']})
        assert replay.data['partial'] == first['partial']

    def test_feature_modes_annotator_self_and_revocation_deny(self):
        source = self.source_receipt()['frozen']
        batch = self.new_batch()
        for actor in (self.worker, self.reviewer):
            self.client.force_authenticate(actor)
            assert self.explain(source=source).status_code in (403, 404)
            assert self.client.get(self.batch_url(batch)).status_code in (403, 404)
        self.client.force_authenticate(self.manager)
        with self.settings(ANNOTATION_CLASSIFICATION_PARTIAL_MATCH_ENABLED=False):
            assert self.explain(source=source).status_code == 404
        with self.settings(ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': 'core_only_internal'}):
            assert self.explain(source=source).status_code == 404
        OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(deleted_at=timezone.now())
        assert self.explain(source=source).status_code in (403, 404)

    def test_budget_boundaries_and_strict_queries(self):
        from rest_framework.exceptions import APIException
        value = {'字': '界'}
        size = len(batches.canonical(value))
        assert partial.bounded_size(value, size) == size
        with self.assertRaises(APIException):
            partial.bounded_size(value, size - 1)
        exact = {'x': '界' * ((partial.MAX_BYTES - 8) // 3)}
        exact['x'] += 'x' * (partial.MAX_BYTES - len(batches.canonical(exact)))
        assert partial.bounded_size(exact) == partial.MAX_BYTES
        with self.assertRaises(APIException):
            partial.bounded_size({'x': exact['x'] + 'x'})
        source = self.source_receipt()['frozen']
        valid = self.explain(source=source)
        business = {k: valid.data[k] for k in ('origin', 'base', 'partial', 'viewer')}
        size = len(batches.canonical(business))
        with patch.object(partial, 'MAX_BYTES', size):
            assert self.explain(source=source).status_code == 200
        with patch.object(partial, 'MAX_BYTES', size - 1):
            assert self.explain(source=source).status_code == 422
        token_size = len(valid.data['frozen'].encode())
        with patch.object(partial, 'MAX_TOKEN', token_size):
            assert self.explain(source=source).status_code == 200
        with patch.object(partial, 'MAX_TOKEN', token_size - 1):
            assert self.explain(source=source).status_code == 422
        for query in ({}, {'submissions': '1,2'}, {'source_frozen': source, 'threshold': '0.5'},
                      {'source_frozen': source, 'frozen': 'other'}):
            assert self.client.get(self.url, query).status_code == 400
        assert self.client.post(self.url, {}).status_code == 405

    def test_render_time_revocation_discards_answers(self):
        from rest_framework.response import Response
        render = Response.render
        def revoke(response):
            result = render(response)
            OrganizationMember.objects.filter(user=self.manager, organization=self.org).update(deleted_at=timezone.now())
            return result
        source = self.source_receipt()['frozen']
        with patch.object(Response, 'render', revoke):
            result = self.explain(source=source)
        assert result.status_code in (403, 404)
        assert 'partial' not in result.data
