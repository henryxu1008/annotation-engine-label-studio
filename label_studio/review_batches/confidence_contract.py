"""Strict operator-owned confidence provenance, independent of ML predictions."""
import copy
import hashlib
import re
from uuid import UUID

from access_control.identity import resolve_actor
from access_control.project_operations import ORGANIZE_REVIEW, PROVIDE_REVIEW_CONFIDENCE, require_project_operation
from classification_disagreements import ner, snapshot_loading
from classification_disagreements import service as classification
from django.conf import settings
from django.db.models import F, OuterRef, Subquery
from rest_framework.exceptions import APIException
from tasks.models import Submission

from . import service
from .handoff_metadata import BoundedMetadata

KIND = 'revision_confidence'
ALGORITHM = 'revision-confidence-ranked/v1'
SCHEMA = 'review-confidence-priority/v1'
INPUT = 'review-confidence-input/v1'
FACT = 'review-confidence-observation/v1'
RECEIPT = 'review-confidence-observation-receipt/v1'
PROFILE = 'review-confidence-profile/v1'
INPUT_CONTRACT = 'static-text-choices-submission/v1'
INTEGER_RULE = 'millionths-int/v1'
SCORE_DEFINITION = 'revision-confidence-lower-uncertain/v1'
MIB = 1024 * 1024
MAX_METADATA, MAX_METADATA_STORED = 4 * MIB, 16 * MIB
MAX_PLAN, MAX_PLAN_STORED = 4 * MIB, 16 * MIB
MAX_OUTPUT, MAX_BODY = 4 * MIB, 16 * 1024
MAX_PROFILE, MAX_PROFILES, MAX_CAPABILITY = 4096, 20, 128 * 1024
MAX_FACT, MAX_FACT_STORED = 8192, 32768
MAX_SINGLE, MAX_SINGLE_STORED = 256 * 1024, 2 * MIB
MAX_SNAPSHOT, MAX_SNAPSHOT_STORED = 8 * MIB, 64 * MIB
HEX = re.compile(r'^[0-9a-f]{64}$')
PINS = (
    'submission_id',
    'assignment_id',
    'task_id',
    'revision',
    'assignee_id',
    'submitter_id',
    'result_hash',
    'task_data_hash',
    'label_config_sha256',
)
DESCRIPTOR_KEYS = {
    'schema',
    'profile_id',
    'producer_namespace',
    'label',
    'version',
    'evaluator_id',
    'evaluator_version',
    'model_id',
    'model_artifact_sha256',
    'score_definition',
    'calibration_version',
    'integer_rule',
    'input_contract',
}


def fail(code='PROOF_INVALID', status=409):
    service.fail('REVIEW_CONFIDENCE_' + code, status)


def integer(value, low=0, high=1_000_000):
    return type(value) is int and low <= value <= high


def positive(value):
    return integer(value, 1, 2**53 - 1)


def hexadecimal(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def uuid_string(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def text(value, maximum=160):
    try:
        return isinstance(value, str) and 0 < len(value.encode('utf-8')) <= maximum and '\x00' not in value
    except UnicodeError:
        return False


def bounded(value, maximum=MAX_METADATA):
    try:
        return ner.bounded(value, maximum)
    except ner.Error:
        fail('TOO_LARGE', 422)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail()


def digest(value):
    bounded(value)
    return service.digest(value)


def instance_id():
    value = settings.ANNOTATION_REVIEW_CONFIDENCE_INSTANCE_ID
    if not uuid_string(value):
        fail('UNAVAILABLE', 403)
    return value


def descriptor(value):
    bounded(value, MAX_PROFILE)
    if not isinstance(value, dict) or set(value) != DESCRIPTOR_KEYS:
        fail('UNAVAILABLE', 403)
    if (
        value['schema'] != PROFILE
        or not uuid_string(value['profile_id'])
        or not uuid_string(value['producer_namespace'])
        or not hexadecimal(value['model_artifact_sha256'])
        or value['input_contract'] != INPUT_CONTRACT
        or value['integer_rule'] != INTEGER_RULE
        or value['score_definition'] != SCORE_DEFINITION
        or any(not text(value[k]) for k in DESCRIPTOR_KEYS - {'profile_id', 'producer_namespace'})
    ):
        fail('UNAVAILABLE', 403)
    return copy.deepcopy(value)


def profiles(project):
    raw = settings.ANNOTATION_REVIEW_CONFIDENCE_PROFILES
    if not isinstance(raw, list):
        fail('UNAVAILABLE', 403)
    selected = []
    for entry in raw:
        if not isinstance(entry, dict):
            fail('UNAVAILABLE', 403)
        if entry.get('project_id') != project.pk or entry.get('organization_id') != project.organization_id:
            continue
        if (
            set(entry) != {'project_id', 'organization_id', 'descriptor', 'digest', 'fresh'}
            or not positive(entry['project_id'])
            or not positive(entry['organization_id'])
            or type(entry['fresh']) is not bool
        ):
            fail('UNAVAILABLE', 403)
        value = descriptor(entry['descriptor'])
        if not hexadecimal(entry['digest']) or digest(value) != entry['digest']:
            fail('UNAVAILABLE', 403)
        selected.append({'descriptor': value, 'digest': entry['digest'], 'fresh': entry['fresh']})
        if len(selected) > MAX_PROFILES:
            fail('UNAVAILABLE', 403)
    identities = [(p['descriptor']['profile_id'], p['digest']) for p in selected]
    if len(identities) != len(set(identities)):
        fail('UNAVAILABLE', 403)
    return sorted(selected, key=lambda p: (p['descriptor']['profile_id'], p['digest']))


def selected_profile(project, selection):
    found = [
        p
        for p in profiles(project)
        if p['descriptor']['profile_id'] == selection['profile_id']
        and p['digest'] == selection['profile_digest']
        and p['fresh']
    ]
    if len(found) != 1:
        fail('UNAVAILABLE', 403)
    return found[0]


def guard(pk, principal, actor, *, create=False, producer=False):
    if not positive(pk):
        fail('INVALID_INPUT', 400)
    if not text(principal.principal_id, 512) or not text(principal.source, 128):
        fail('FORBIDDEN', 403)
    project, role, _ = service.guard(pk, principal, actor, manager=create or producer)
    if not settings.ANNOTATION_REVIEW_CONFIDENCE_ENABLED:
        fail('UNAVAILABLE', 403)
    instance_id()
    if create:
        require_project_operation(project, ORGANIZE_REVIEW)
    if producer:
        require_project_operation(project, PROVIDE_REVIEW_CONFIDENCE)
    return principal, actor, project, role


def context(request, initial, *, create=False, producer=False):
    principal, actor = resolve_actor(request)
    current = guard(initial[2].pk, principal, actor, create=create, producer=producer)
    if (
        principal != initial[0]
        or actor.pk != initial[1].pk
        or current[2].organization_id != initial[2].organization_id
        or current[3] != initial[3]
    ):
        fail('AUTHORITY_CHANGED', 403)
    return current


def grant(current, profile_id):
    principal, actor, project, _ = current
    raw = settings.ANNOTATION_REVIEW_CONFIDENCE_GRANTS
    if not isinstance(raw, list):
        fail('UNAVAILABLE', 403)
    entries = [
        e
        for e in raw
        if isinstance(e, dict)
        and e.get('project_id') == project.pk
        and e.get('organization_id') == project.organization_id
        and e.get('principal_id') == principal.principal_id
        and e.get('source') == principal.source
        and e.get('actor_id') == actor.pk
    ]
    if len(entries) != 1:
        fail('FORBIDDEN', 403)
    entry = entries[0]
    if (
        set(entry)
        != {'project_id', 'organization_id', 'principal_id', 'source', 'actor_id', 'producer_namespace', 'profile_ids'}
        or not uuid_string(entry['producer_namespace'])
        or not isinstance(entry['profile_ids'], list)
        or not 1 <= len(entry['profile_ids']) <= MAX_PROFILES
        or any(not uuid_string(v) for v in entry['profile_ids'])
        or profile_id not in entry['profile_ids']
        or len(set(entry['profile_ids'])) != len(entry['profile_ids'])
        or not all(positive(entry[k]) for k in ('project_id', 'organization_id', 'actor_id'))
    ):
        fail('FORBIDDEN', 403)
    return copy.deepcopy(entry)


def owner_hash(current):
    p, a, project, role = current
    return digest(
        {
            'principal': p.principal_id,
            'source': p.source,
            'actor': a.pk,
            'project': project.pk,
            'organization': project.organization_id,
            'role': role,
        }
    )


def public_profile(value):
    return {k: v for k, v in value.items() if k != 'producer_namespace'}


def capability(current):
    initial = guard(current[2].pk, current[0], current[1])
    data = {'can_read_confidence_priority': True}
    if initial[3] != 'manager':
        return {**data, 'can_prioritize_confidence': False}
    try:
        guard(initial[2].pk, initial[0], initial[1], create=True)
    except APIException:
        return {**data, 'can_prioritize_confidence': False}
    data.update(can_recover_confidence=True, confidence_owner_hash=owner_hash(initial), confidence_profiles=[])
    try:
        values = profiles(initial[2])
        data['confidence_profiles'] = [
            dict(public_profile(p['descriptor']), digest=p['digest']) for p in values if p['fresh']
        ]
    except APIException:
        pass
    data['can_prioritize_confidence'] = bool(data['confidence_profiles'])
    bounded(data, MAX_CAPABILITY)
    return data


def metadata_budget():
    return BoundedMetadata(MAX_METADATA, MAX_METADATA_STORED, bounded, lambda code, status: fail('TOO_LARGE', status))


class SnapshotPolicy:
    MAX_REVIEW_REASON_BYTES = MAX_OUTPUT
    USER_FIELDS = ('id', 'first_name', 'last_name', 'email', 'avatar')
    MAX_STORED_BYTES = MAX_SNAPSHOT_STORED
    MAX_SNAPSHOT_BYTES = MAX_SNAPSHOT
    MAX_ROW_STORED_BYTES = MAX_SINGLE_STORED
    MAX_ROW_SNAPSHOT_BYTES = MAX_SINGLE
    Error = ner.Error
    TOO_LARGE = 'REVIEW_CONFIDENCE_TOO_LARGE'
    SOURCE_INTEGRITY_FAILED = 'REVIEW_CONFIDENCE_PROOF_INVALID'
    bounded = staticmethod(bounded)


class SingleSnapshotPolicy(SnapshotPolicy):
    MAX_STORED_BYTES = MAX_SINGLE_STORED
    MAX_SNAPSHOT_BYTES = MAX_SINGLE


def load(query, *, for_serialization=False, single=False):
    try:
        return snapshot_loading.load(
            query.select_related(None),
            policy=SingleSnapshotPolicy if single else SnapshotPolicy,
            validate=service.valid_source,
            projection='submission' if for_serialization else 'review',
        )
    except ner.Error as exc:
        service.fail(exc.code, exc.status)


def subject(row, current):
    snapshot = row.result_snapshot
    if not service.valid_source(row, current[2].pk):
        fail()
    xml = snapshot['project'].get('label_config')
    if not isinstance(xml, str):
        fail('INPUT_UNSUPPORTED', 400)
    pin = {
        **service.identity(row),
        'assignee_id': row.assignment.assignee_id,
        'task_data_hash': digest(snapshot['task'].get('data')),
        'label_config_sha256': hashlib.sha256(xml.encode('utf-8')).hexdigest(),
    }
    if any(not positive(pin[k]) for k in PINS[:6]):
        fail('INPUT_CHANGED')
    return pin


def supported(row):
    """Reuse the existing static classification parser; no metric is computed."""
    try:
        xml = row.result_snapshot['project']['label_config']
        schema = classification.controls(xml)
        # The existing comparator accepts decorative HTML/CSS. Such decoration
        # may fetch mutable media, which this complete-input V1 contract excludes.
        attributes = {
            'View': {'name'},
            'Text': {'name', 'value', 'valuetype'},
            'Choices': {'name', 'toname', 'choice', 'required', 'showinline', 'perregion', 'peritem'},
            'Choice': {'value', 'hotkey', 'selected', 'alias'},
        }
        for node in classification.etree.fromstring(xml).iter():
            if (
                set(key.lower() for key in node.attrib) - attributes[node.tag]
                or (node.text or '').strip()
                or (node.tail or '').strip()
            ):
                return False
        if any(not re.fullmatch(r'[A-Za-z0-9_]+', c['field']) or c['field'] == '__proto__' for c in schema.values()):
            return False
        classification.answers(row, schema)
        return all(
            len(row.result_snapshot['task']['data'][c['field']].encode('utf-8')) <= 65536 for c in schema.values()
        )
    except (classification.Incomparable, KeyError, TypeError, ValueError, UnicodeError):
        return False


def envelope(row, current):
    pins = subject(row, current)
    scope = {'instance_id': instance_id(), 'organization_id': current[2].organization_id, 'project_id': current[2].pk}
    value = {'schema': INPUT, **scope, 'subject': pins, 'result_snapshot': row.result_snapshot}
    return value, digest(value)


def source_query(project):
    return Submission.objects.filter(assignment__project=project, assignment__task__project=project)


def pending_query(project):
    newest = Submission.objects.filter(assignment_id=OuterRef('assignment_id')).order_by('-revision')
    return source_query(project).filter(
        pk=Subquery(newest.values('pk')[:1]),
        status='pending',
        review__isnull=True,
        submitted_by_id=F('assignment__assignee_id'),
    )
