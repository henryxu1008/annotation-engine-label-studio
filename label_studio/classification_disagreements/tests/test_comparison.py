import copy
from unittest.mock import patch

from classification_disagreements import service
from django.test import override_settings
from rest_framework.test import APITestCase
from review_batches.tests import test_batches as fixtures
from tasks.models import Submission
from tasks.submissions import _snapshot_hash

CONFIG = (
    '<View><Text name="text" value="$text"/><Choices name="sentiment" toName="text">'
    '<Choice value="Positive"/><Choice value="Negative"/></Choices></View>'
)


@override_settings(ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=True)
class TestClassification(APITestCase):
    setUpTestData = classmethod(fixtures.TestReviewBatches.setUpTestData.__func__)
    make_source = fixtures.TestReviewBatches.make_source

    def setUp(self):
        self.project.label_config = CONFIG
        self.project.save()
        self.client.force_authenticate(self.manager)
        self.mode = self.settings(
            ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': 'fork_review'}
        )
        self.mode.enable()
        self.addCleanup(self.mode.disable)
        self.first = self.make_source()
        self.second = self.make_source(worker=self.reviewer, assignment=None)
        # Independent assignments on exactly the same task.
        self.second.assignment.task = self.first.assignment.task
        self.second.assignment.save()
        for row in (self.first, self.second):
            self.set_answer(row, 'Positive')
        self.base = f'/api/projects/{self.project.pk}/classification-disagreements/'
        self.url = f'{self.base}tasks/{self.first.assignment.task_id}/'

    def set_answer(self, row, answer, mutate=None):
        snapshot = copy.deepcopy(row.result_snapshot)
        snapshot['project']['label_config'] = CONFIG
        snapshot['annotation']['was_cancelled'] = False
        snapshot['task'] = {'id': self.first.assignment.task_id, 'data': {'text': 'Synthetic 合成'}, 'meta': {}}
        snapshot['annotation']['result'] = [
            {'from_name': 'sentiment', 'to_name': 'text', 'type': 'choices', 'value': {'choices': [answer]}}
        ]
        if mutate:
            mutate(snapshot)
        row.result_snapshot, row.result_hash = snapshot, _snapshot_hash(snapshot)
        # Synthetic immutable fixture created before any read, not a product edit path.
        Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=row.result_hash)

    def read(self, rows=None, frozen=None):
        return self.client.get(
            self.url,
            {'frozen': frozen}
            if frozen
            else {'submissions': ','.join(str(row.pk) for row in (rows or [self.first, self.second]))},
        )

    def test_exact_agreement_and_three_person_split_with_locations(self):
        response = self.read()
        assert response.status_code == 200, response.data
        assert response.data['agreement'] == 1
        assert response['Cache-Control'] == 'private, no-store'
        self.set_answer(self.second, 'Negative')
        response = self.read()
        assert response.data['agreement'] == 0
        assert response.data['locations'][0]['from_name'] == 'sentiment'
        assert response.data['locations'][0]['disagrees'] is True
        third = self.make_source(worker=self.manager)
        third.assignment.task = self.first.assignment.task
        third.assignment.save()
        self.set_answer(third, 'Positive')
        data = self.read([self.first, self.second, third]).data
        assert (data['matching_pairs'], data['total_pairs'], data['agreement']) == (1, 3, 1 / 3)

    def test_frozen_replay_pins_ids_hashes_config_time_and_never_uses_newest(self):
        before = list(Submission.objects.values())
        first = self.read().data
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.set_answer(newer, 'Negative')
        replay = self.read(frozen=first['frozen']).data
        for key in ('manifest', 'manifest_hash', 'agreement', 'locations', 'frozen'):
            assert replay[key] == first[key]
        assert first['manifest']['inputs'][0]['config_hash']
        assert list(Submission.objects.filter(pk__in=[r['id'] for r in before]).values()) == before

    def test_one_source_and_duplicate_submitter_are_unavailable_not_perfect(self):
        assert self.read([self.first]).data['reason'] == 'insufficient_inputs'
        newer = self.make_source(assignment=self.first.assignment, revision=2)
        self.set_answer(newer, 'Positive')
        data = self.read([self.first, newer]).data
        assert data['reason'] == 'duplicate_submitter'
        assert data['agreement'] is None and data['locations'] == []

    def test_unavailable_boundaries_never_return_answer_values(self):
        cases = [
            ('cancelled_or_missing', lambda s: s['annotation'].update(was_cancelled=True)),
            ('missing_or_malformed', lambda s: s['annotation'].update(result=[])),
            ('config_mismatch', lambda s: s['project'].update(label_config=CONFIG + ' ')),
            ('task_data_mismatch', lambda s: s['task'].update(data={'text': 'Changed'})),
        ]
        for reason, mutate in cases:
            self.set_answer(self.second, 'Positive', mutate)
            data = self.read().data
            assert data['reason'] == reason, data
            assert data['agreement'] is None and data['locations'] == []

    def test_bad_labels_and_region_values_are_unavailable(self):
        self.set_answer(self.second, 'Unknown')
        assert self.read().data['reason'] == 'missing_or_malformed'
        self.set_answer(self.second, 'Positive', lambda s: s['annotation']['result'][0]['value'].update(start=1))
        assert self.read().data['reason'] == 'missing_or_malformed'

    def test_multiple_choice_order_and_result_ids_do_not_change_metric(self):
        config = CONFIG.replace('toName="text"', 'toName="text" choice="multiple"')
        for row, values in ((self.first, ['Positive', 'Negative']), (self.second, ['Negative', 'Positive'])):
            snapshot = copy.deepcopy(row.result_snapshot)
            snapshot['project']['label_config'] = config
            snapshot['annotation']['result'][0]['value']['choices'] = values
            snapshot['annotation']['result'][0]['id'] = str(row.pk)
            Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        assert self.read().data['agreement'] == 1

    def test_invalid_hash_missing_submitter_and_unsupported_configuration(self):
        Submission.objects.filter(pk=self.second.pk).update(result_hash='0' * 64)
        assert self.read().data['reason'] == 'invalid_source'
        self.set_answer(self.second, 'Positive')
        Submission.objects.filter(pk=self.second.pk).update(submitted_by=None)
        assert self.read().data['reason'] == 'missing_submitter'
        Submission.objects.filter(pk=self.second.pk).update(submitted_by=self.reviewer)
        for row in (self.first, self.second):
            self.set_answer(
                row,
                'Positive',
                lambda s: s['project'].update(label_config='<View><Image name="x" value="$x"/></View>'),
            )
        assert self.read().data['reason'] == 'unsupported_config'

    def test_request_limits_reject_duplicates_empty_ids_and_more_than_twenty(self):
        for value in (
            '',
            '0',
            '-1',
            'abc',
            f'{self.first.pk},{self.first.pk}',
            ','.join(str(i) for i in range(1, 22)),
        ):
            assert self.client.get(self.url, {'submissions': value}).status_code == 400

    def test_conditional_containers_aliases_per_region_and_entities_are_unsupported(self):
        for config in (
            CONFIG.replace('<View>', '<View visibleWhen="choice-selected">'),
            CONFIG.replace('<View>', '<View visiblewhen="choice-selected">'),
            CONFIG.replace('value="Positive"', 'value="Positive" alias="Alias"'),
            CONFIG.replace('<Choices ', '<Choices perRegion="true" '),
            CONFIG.replace('<Choices ', '<Choices perregion="true" '),
            CONFIG.replace('<Choices ', '<Choices PERITEM="true" '),
            '<!DOCTYPE x [<!ENTITY unsafe "Positive">]>' + CONFIG,
        ):
            for row in (self.first, self.second):
                self.set_answer(row, 'Positive', lambda snapshot: snapshot['project'].update(label_config=config))
            data = self.read().data
            assert data['reason'] == 'unsupported_config'
            assert data['agreement'] is None and data['locations'] == []

    def test_url_backed_text_is_unavailable_even_with_identical_frozen_urls(self):
        for attribute in (
            'valueType="url"',
            'valuetype="url"',
            'ValueType="url"',
            'vAlUeTyPe="url"',
            'valueType="json"',
            'valueType="text" valuetype="url"',
        ):
            config = CONFIG.replace('<Text ', f'<Text {attribute} ')
            for row in (self.first, self.second):

                def remote(snapshot):
                    snapshot['project']['label_config'] = config
                    snapshot['task']['data']['text'] = 'https://synthetic.invalid/mutable.txt'

                self.set_answer(row, 'Positive', remote)
            initial = self.read().data
            replay = self.read(frozen=initial['frozen']).data
            for report in (initial, replay):
                assert report['reason'] == 'unsupported_config'
                assert report['agreement'] is None and report['locations'] == []
            assert replay['manifest'] == initial['manifest']
        for attribute in ('valueType="text"', 'valuetype="text"'):
            config = CONFIG.replace('<Text ', f'<Text {attribute} ')
            for row in (self.first, self.second):
                self.set_answer(row, 'Positive', lambda snapshot: snapshot['project'].update(label_config=config))
            assert self.read().data['agreement'] == 1

    def test_two_controls_report_per_control_locations_and_whole_answer_agreement(self):
        config = CONFIG.replace(
            '</View>',
            '<Choices name="second" toName="text"><Choice value="Positive"/>'
            '<Choice value="Negative"/></Choices></View>',
        )
        for row in (self.first, self.second):
            snapshot = copy.deepcopy(row.result_snapshot)
            snapshot['project']['label_config'] = config
            snapshot['annotation']['result'].append(
                {
                    'from_name': 'second',
                    'to_name': 'text',
                    'type': 'choices',
                    'value': {'choices': ['Positive' if row.pk == self.first.pk else 'Negative']},
                }
            )
            Submission.objects.filter(pk=row.pk).update(result_snapshot=snapshot, result_hash=_snapshot_hash(snapshot))
        data = self.read().data
        assert data['agreement'] == 0
        assert {row['from_name']: row['agreement'] for row in data['locations']} == {'second': 0, 'sentiment': 1}

    def test_hash_corruption_and_frozen_source_deletion_do_not_fallback(self):
        frozen = self.read().data['frozen']
        self.second.delete()
        data = self.read(frozen=frozen).data
        assert data['reason'] == 'source_unavailable' and data['locations'] == []
        Submission.objects.filter(pk=self.first.pk).update(result_hash='0' * 64)
        assert self.read([self.first]).data['status'] == 'unavailable'

    def test_annotator_reviewer_self_source_and_revocation_denied(self):
        self.client.force_authenticate(self.worker)
        assert self.read().status_code in (403, 404)
        self.client.force_authenticate(self.reviewer)
        assert self.read().status_code == 404
        sources = self.client.get(self.url + 'sources/').data['sources']
        assert [row['submission_id'] for row in sources] == [self.first.pk]
        self.review_member.delete()
        assert self.read([self.first]).status_code in (403, 404)

    def test_token_is_not_authority_and_cannot_change_project_or_actor(self):
        token = self.read().data['frozen']
        assert self.client.get(self.url, {'frozen': token + 'forged'}).status_code == 400
        self.client.force_authenticate(self.reviewer)
        assert self.read(frozen=token).status_code == 404
        self.client.force_authenticate(self.manager)
        assert (
            self.client.get(self.url, {'submissions': str(self.first.pk), 'actor': self.reviewer.pk}).status_code
            == 400
        )
        assert self.client.post(self.url, {}, format='json').status_code == 405
        assert self.client.get(
            self.url.replace(f'projects/{self.project.pk}', f'projects/{self.other.pk}'), {'frozen': token}
        ).status_code in (403, 404)

    def test_response_time_source_mutation_and_permission_withdrawal_fail_closed(self):
        calculate = service.calculate

        def revoke(rows, project_id):
            data = calculate(rows, project_id)
            type(self.manager).objects.filter(pk=self.manager.pk).update(is_active=False)
            return data

        with patch.object(service, 'calculate', revoke):
            response = self.read()
        assert response.status_code in (401, 403, 404)
        assert 'locations' not in response.data

    def test_core_only_disabled_and_read_policy_boundary(self):
        with override_settings(ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED=False):
            assert self.read().status_code == 404
        with override_settings(
            ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES={f'{self.org.pk}:{self.project.pk}': 'core_only_internal'}
        ):
            assert self.read().status_code == 404
        with override_settings(
            ANNOTATION_PROJECT_OPERATION_PROVIDER='review_batches.tests.test_batches.ReadOnlyPolicy'
        ):
            assert self.read().status_code == 200
        with override_settings(
            ANNOTATION_PROJECT_OPERATION_PROVIDER='review_batches.tests.test_batches.ReviewWithoutReadPolicy'
        ):
            assert self.read().status_code == 403
