"""One future seed transaction; reuse guarded supervision seeder and product services.

Imported only after E0. No audit capture, queue mocks, synchronous generate or model.
"""
from pathlib import Path
from uuid import uuid4

import ls84_contract as c


def seed(m):
    from classification_disagreements import batches
    from classification_disagreements.models import ClassificationBatch
    from core.management.commands.seed_second_review_e2e import XML
    from django.core.management import call_command
    from django.db import transaction
    from django.test import override_settings
    from django.utils import timezone
    from projects.models import Project, ProjectMember
    from rest_framework.test import APIClient
    from review_batches import confidence_contract as confidence
    from review_batches.service import digest
    from tasks.models import Annotation, Task, TaskAssignment
    from tasks.submissions import create_submission
    from users.models import User

    root = Path(m['run_root'])
    source = root / 'reports/supervision-private.json'
    target = root / 'reports/fixture-private.json'
    c.require(not source.exists() and not target.exists(), 'FRESH_FIXTURE_OUTPUT_REQUIRED')
    # The existing seeder refuses nonempty business scope and validates the native cluster proof.
    with transaction.atomic():
        call_command('seed_second_review_e2e', output=str(source))
        supervision = c.decode(c.read_file(source))
        manager = User.objects.get(pk=supervision['users']['manager']['id'])
        org = manager.active_organization
        actors = {}
        for name in ('followup', 'auditor', 'producer', 'outsider'):
            user = User.objects.create_user(name + '@' + c.DOMAIN, supervision['password'],
                                            username='acceptance-' + name, active_organization=org)
            org.add_user(user)
            actors[name] = {'id': user.pk, 'email': user.email}
        value = {'schema': 'ls84-combined-fixture/v1', 'run_id': m['run_id'], 'source_digest': m['source_digest'],
                 'instance_id': m['instance_id'], 'supervision': supervision, 'actors': actors, 'scopes': [],
                 'confidence': {'profiles': [], 'grants': []}, 'pending': {}, 'classification': {}}
        namespace, profile_id = str(uuid4()), str(uuid4())
        descriptor = {
            'schema': confidence.PROFILE, 'profile_id': profile_id, 'producer_namespace': namespace,
            'label': 'Synthetic protocol only', 'version': 'synthetic-v1', 'evaluator_id': 'synthetic-offline',
            'evaluator_version': '1', 'model_id': 'synthetic-no-model', 'model_artifact_sha256': 'a' * 64,
            'score_definition': confidence.SCORE_DEFINITION, 'calibration_version': 'synthetic-v1',
            'integer_rule': confidence.INTEGER_RULE, 'input_contract': confidence.INPUT_CONTRACT,
        }
        for locale, info in supervision['projects'].items():
            project = Project.objects.get(pk=info['id'])
            c.require(project.label_config == XML, 'STATIC_CLASSIFICATION_ONLY')
            value['scopes'].append({'project_id': project.pk, 'organization_id': org.pk, 'creator_id': manager.pk,
                                    'creator_email': manager.email, 'title': project.title})
            for name in ('followup', 'auditor', 'producer'):
                ProjectMember.objects.create(project=project, user_id=actors[name]['id'], role='manager')
            # outsider is a real org member with no project membership; no synthetic grant bypass.
            value['confidence']['profiles'].append({'organization_id': org.pk, 'project_id': project.pk,
                                                   'descriptor': descriptor, 'digest': confidence.digest(descriptor),
                                                   'fresh': True})
            value['confidence']['grants'].append({
                'organization_id': org.pk, 'project_id': project.pk,
                'principal_id': f'label_studio_local:{actors["producer"]["id"]}', 'source': 'label_studio_local',
                'actor_id': actors['producer']['id'], 'producer_namespace': namespace, 'profile_ids': [profile_id],
            })
            pending = []
            for kind in ('agreement', 'conflict'):
                task = Task.objects.create(project=project, data={'text': 'Synthetic pending ' + kind})
                for index, name in enumerate(('a', 'b')):
                    owner = User.objects.get(pk=supervision['users'][name]['id'])
                    assignment = TaskAssignment.objects.create(project=project, task=task, assignee=owner,
                                                               assigned_by=manager)
                    annotation = Annotation.objects.create(
                        project=project, task=task, completed_by=owner, lead_time=1.0,
                        result=[{'id': 'synthetic-' + str(assignment.pk), 'from_name': 'label', 'to_name': 'text',
                                 'type': 'choices', 'value': {'choices': ['B' if kind == 'conflict' and index else 'A']}}])
                    assignment.annotation = annotation
                    assignment.save(update_fields=['annotation'])
                    submission = create_submission(assignment=assignment, annotation=annotation, actor=owner)
                    pending.append({'id': submission.pk, 'assignment_id': assignment.pk, 'task_id': task.pk,
                                    'annotation_id': annotation.pk, 'owner': name, 'kind': kind})
            value['pending'][locale] = pending
            value['classification'][locale] = None
            project.summary.update_data_columns(list(Task.objects.filter(project=project)))
        configured = c.fixture_settings(value, m, building=True)
        with override_settings(**configured):
            producer = User.objects.get(pk=actors['producer']['id'])
            client = APIClient()
            client.force_authenticate(producer)
            for locale, info in supervision['projects'].items():
                project = Project.objects.get(pk=info['id'])
                pending = value['pending'][locale]
                for index, source_row in enumerate(pending):
                    base = f'/api/projects/{project.pk}/'
                    response = client.get(base + f'review-confidence-inputs/{source_row["id"]}/',
                                          {'profile_id': profile_id, 'profile_digest': confidence.digest(descriptor)})
                    c.require(response.status_code == 200, 'SYNTHETIC_CONFIDENCE_INPUT_REJECTED')
                    body = {'submission_id': source_row['id'], 'profile_id': profile_id,
                            'profile_digest': confidence.digest(descriptor),
                            'evaluated_input_hash': response.data['input_hash'], 'score_units': index * 100,
                            'evaluated_at': timezone.now().isoformat()}
                    response = client.post(base + 'review-confidence-observations/', body, format='json',
                                           HTTP_IDEMPOTENCY_KEY=str(uuid4()))
                    c.require(response.status_code == 201, 'SYNTHETIC_CONFIDENCE_OBSERVATION_REJECTED')
                now = timezone.now().isoformat()
                manifest = {**batches.build_plan(project, sorted({s['task_id'] for s in pending})),
                            'schema': batches.SCHEMA, 'project_id': project.pk, 'organization_id': org.pk,
                            'creator_principal': f'label_studio_local:{manager.pk}', 'actor_id_frozen': manager.pk,
                            'name': 'Synthetic frozen classification', 'scope_enumerated_at': now, 'calculated_at': now}
                row = ClassificationBatch.objects.create(
                    project=project, created_by=manager, creator_principal=manifest['creator_principal'],
                    actor_id_frozen=manager.pk, name=manifest['name'], idempotency_key=uuid4(),
                    request_hash=digest({'synthetic_run': m['run_id'], 'project_id': project.pk}),
                    manifest=manifest, manifest_hash=digest(manifest))
                value['classification'][locale] = {'id': row.pk, 'manifest_hash': row.manifest_hash}
        # External writes cannot roll back with PostgreSQL. Failure leaves an occupied output;
        # never retry seed against it or silently repair/erase an uncertain transaction.
        c.fixture_settings(value, m)
        c.write_new(target, value)
    return value
