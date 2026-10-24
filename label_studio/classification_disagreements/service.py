"""Versioned, read-only classification comparison over explicitly pinned revisions."""

import hashlib
import itertools

import defusedxml.ElementTree as etree
from access_control.authorization import authorization
from access_control.project_operations import READ_WORK, require_project_operation
from defusedxml.common import DefusedXmlException
from django.conf import settings
from projects.models import Project
from rest_framework.exceptions import NotFound
from rest_framework.generics import get_object_or_404
from review_batches.service import digest, identity, valid_source
from tasks.models import Submission, Task
from tasks.my_work import work_projects

RULE = 'classification-exact-sets/v1'
SALT = 'classification-disagreements/v1'
MAX_INPUTS = 20


def guard(project_id, principal, actor):
    project = get_object_or_404(
        Project.objects.filter(pk__in=work_projects(actor, ['manager', 'reviewer']), deleted_at__isnull=True),
        pk=project_id,
    )
    if not settings.ANNOTATION_CLASSIFICATION_DISAGREEMENTS_ENABLED:
        raise NotFound()
    modes = settings.ANNOTATION_CLASSIFICATION_DISAGREEMENT_MODES
    if not isinstance(modes, dict) or modes.get(f'{project.organization_id}:{project.pk}') != 'fork_review':
        raise NotFound()
    role = authorization.project_role(principal, project)
    if role not in ('manager', 'reviewer'):
        raise NotFound()
    require_project_operation(project, READ_WORK)
    return project, role


def task_scope(project, task_id):
    return get_object_or_404(Task, pk=task_id, project=project)


def sources(project, task_id, actor, role):
    rows = Submission.objects.filter(
        assignment__project=project, assignment__task_id=task_id, assignment__task__project=project
    ).select_related('assignment__task', 'review')
    return rows.exclude(submitted_by=actor) if role == 'reviewer' else rows


def pin(row):
    snapshot = row.result_snapshot if isinstance(row.result_snapshot, dict) else {}
    project = snapshot.get('project') if isinstance(snapshot.get('project'), dict) else {}
    return {**identity(row), 'config_hash': hashlib.sha256(str(project.get('label_config', '')).encode()).hexdigest()}


class Incomparable(Exception):
    def __init__(self, reason):
        self.reason = reason


def controls(xml):
    if not isinstance(xml, str) or len(xml.encode()) > 65536:
        raise Incomparable('unsupported_config')
    try:
        root = etree.fromstring(xml)
    except (etree.ParseError, ValueError, DefusedXmlException):
        raise Incomparable('unsupported_config')
    nodes = list(root.iter())
    if any(node.tag not in ('View', 'Text', 'Choices', 'Choice') for node in nodes):
        raise Incomparable('unsupported_config')
    if any(
        any(key in node.attrib for key in ('visibleWhen', 'whenTagName', 'whenLabelValue', 'whenChoiceValue'))
        for node in nodes
    ):
        raise Incomparable('unsupported_config')
    texts = {node.get('name'): node.get('value') for node in nodes if node.tag == 'Text'}
    names = [node.get('name') for node in nodes if node.tag in ('Text', 'Choices')]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise Incomparable('unsupported_config')
    result = {}
    for node in nodes:
        if node.tag != 'Choices':
            continue
        name, target = node.get('name'), node.get('toName')
        if (
            not name
            or name in result
            or target not in texts
            or not texts[target]
            or not texts[target].startswith('$')
            or node.get('perRegion', 'false') != 'false'
            or node.get('perItem', 'false') != 'false'
            or node.get('visibleWhen')
        ):
            raise Incomparable('unsupported_config')
        labels = [choice.get('value') for choice in node]
        if (
            not labels
            or len(labels) > 100
            or any(choice.tag != 'Choice' or choice.get('alias') or list(choice) for choice in node)
            or any(not value or '$' in value for value in labels)
            or len(labels) != len(set(labels))
        ):
            raise Incomparable('unsupported_config')
        mode = node.get('choice', 'single')
        if mode not in ('single', 'single-radio', 'multiple'):
            raise Incomparable('unsupported_config')
        result[name] = {'target': target, 'field': texts[target][1:], 'labels': labels, 'multiple': mode == 'multiple'}
    if not result or len(result) > 20:
        raise Incomparable('unsupported_config')
    return result


def answers(row, schema):
    snapshot = row.result_snapshot
    annotation = snapshot.get('annotation')
    if not isinstance(annotation, dict) or annotation.get('was_cancelled') is not False:
        raise Incomparable('cancelled_or_missing')
    values = annotation.get('result')
    if not isinstance(values, list) or len(values) != len(schema):
        raise Incomparable('missing_or_malformed')
    data = snapshot.get('task', {}).get('data')
    if not isinstance(data, dict) or any(not isinstance(data.get(c['field']), str) for c in schema.values()):
        raise Incomparable('missing_or_malformed')
    result = {}
    for value in values:
        if not isinstance(value, dict):
            raise Incomparable('missing_or_malformed')
        name = value.get('from_name')
        control = schema.get(name) if isinstance(name, str) else None
        choices = value.get('value', {}).get('choices') if isinstance(value.get('value'), dict) else None
        if (
            not control
            or name in result
            or value.get('to_name') != control['target']
            or value.get('type') != 'choices'
            or not isinstance(choices, list)
            or not choices
            or len(choices) > len(control['labels'])
            or set(value['value']) != {'choices'}
            or any(not isinstance(choice, str) or choice not in control['labels'] for choice in choices)
            or len(choices) != len(set(choices))
            or (not control['multiple'] and len(choices) != 1)
            or any(key in value for key in ('start', 'end', 'parentID'))
        ):
            raise Incomparable('missing_or_malformed')
        result[name] = sorted(choices)
    return result


def calculate(rows, project_id):
    try:
        if len(rows) < 2:
            raise Incomparable('insufficient_inputs')
        if any(row.submitted_by_id is None for row in rows):
            raise Incomparable('missing_submitter')
        if len({row.submitted_by_id for row in rows}) != len(rows):
            raise Incomparable('duplicate_submitter')
        if any(not valid_source(row, project_id) for row in rows):
            raise Incomparable('invalid_source')
        configs = [row.result_snapshot['project'].get('label_config') for row in rows]
        if any(config != configs[0] for config in configs):
            raise Incomparable('config_mismatch')
        schema = controls(configs[0])
        task_data = [digest(row.result_snapshot['task'].get('data')) for row in rows]
        if len(set(task_data)) != 1:
            raise Incomparable('task_data_mismatch')
        normalized = [answers(row, schema) for row in rows]
        pairs = list(itertools.combinations(range(len(rows)), 2))
        matching = sum(normalized[a] == normalized[b] for a, b in pairs)
        locations = []
        for name, control in sorted(schema.items()):
            matches = sum(normalized[a][name] == normalized[b][name] for a, b in pairs)
            locations.append(
                {
                    'from_name': name,
                    'to_name': control['target'],
                    'matching_pairs': matches,
                    'total_pairs': len(pairs),
                    'agreement': matches / len(pairs),
                    'disagrees': matches != len(pairs),
                    'values': [
                        {'submission_id': row.pk, 'choices': normalized[i][name]} for i, row in enumerate(rows)
                    ],
                }
            )
        return {
            'status': 'comparable',
            'reason': None,
            'matching_pairs': matching,
            'total_pairs': len(pairs),
            'agreement': matching / len(pairs),
            'locations': locations,
        }
    except (Incomparable, TypeError, ValueError, KeyError, AttributeError) as exc:
        return unavailable(exc.reason if isinstance(exc, Incomparable) else 'missing_or_malformed')


def unavailable(reason):
    return {
        'status': 'unavailable',
        'reason': reason,
        'matching_pairs': None,
        'total_pairs': None,
        'agreement': None,
        'locations': [],
    }
