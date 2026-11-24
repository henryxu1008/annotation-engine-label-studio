"""Manager-only access to frozen blind classification scores."""

from access_control.project_operations import ORGANIZE_CLASSIFICATION_GOLD, lock_project_operation
from django.db import models, transaction
from rest_framework.exceptions import NotFound
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from review_batches.service import digest, fail

from .blind_scoring import SCHEMA
from .gold_api import GoldAPI, MAX_BYTES, bounded_query, guard
from .gold_scoring import RULE
from .models import BlindGoldScore, BlindQualityAssignment


class BlindScoresAPI(GoldAPI):
    """Expose bounded score evidence only while the manager remains authorized."""

    http_method_names = ['get', 'head', 'options']

    @transaction.atomic
    def get(self, request, pk, score_id=None):
        principal, actor, project = guard(request, pk)
        params = request.query_params
        if (set(params) - {'before'} or (score_id is not None and params)
                or ('before' in params and (len(params.getlist('before')) != 1
                    or not params['before'].isascii() or not params['before'].isdigit()
                    or len(params['before']) > 19 or not 0 < int(params['before']) < 2**63))):
            fail('GOLD_INVALID_INPUT', 400)
        project, locked_actor = lock_project_operation(project.pk, principal, ORGANIZE_CLASSIFICATION_GOLD)
        current = guard(request, pk)
        if current[0] != principal or locked_actor.pk != actor.pk or current[1].pk != actor.pk:
            raise NotFound()
        rows = BlindGoldScore.objects.filter(submission__assignment__project=project,
                                             submission__assignment__task__project=project)
        if score_id is None:
            if 'before' in params:
                rows = rows.filter(pk__lt=int(params['before']))
            # No reference answers or scoring details in the collection.
            items = list(rows.order_by('-pk').values('id', 'submission_id', 'created_at', 'manifest_hash')[:101])
            page, has_more = items[:100], len(items) > 100
            return Response({'items': page, 'has_more': has_more,
                             'next_before': page[-1]['id'] if has_more else None})

        query = bounded_query(rows, 'manifest').annotate(
            _bounded_manifest=models.Case(
                models.When(_gold_bytes__gte=0, _gold_bytes__lte=MAX_BYTES, then=models.F('manifest')),
                default=models.Value(None), output_field=models.JSONField(),
            ),
        ).select_related('submission__assignment')
        score = get_object_or_404(query, pk=score_id)
        manifest, submission = score._bounded_manifest, score.submission
        assignment = submission.assignment
        marker = BlindQualityAssignment.objects.filter(assignment_id=assignment.pk, task_id=assignment.task_id,
            project_id=project.pk, assignee_id=assignment.assignee_id).first()
        if (type(score._gold_bytes) is not int or not 0 <= score._gold_bytes <= MAX_BYTES
                or not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA
                or set(manifest) != {'schema', 'project_id', 'task_id', 'assignment_id', 'assignee_id',
                                     'submission_id', 'revision', 'submission_hash', 'reference_id',
                                     'reference_hash', 'rule', 'result'}
                or manifest.get('rule') != RULE or not isinstance(manifest.get('result'), dict)
                or manifest.get('project_id') != project.pk or manifest.get('task_id') != assignment.task_id
                or manifest.get('assignment_id') != assignment.pk or manifest.get('assignee_id') != assignment.assignee_id
                or manifest.get('submission_id') != submission.pk or manifest.get('revision') != submission.revision
                or manifest.get('submission_hash') != submission.result_hash
                or submission.submitted_by_id != assignment.assignee_id or marker is None
                or type(marker.reference_id) is not int or marker.reference_id <= 0
                or not isinstance(marker.reference_hash, str) or len(marker.reference_hash) != 64
                or manifest.get('reference_id') != marker.reference_id
                or manifest.get('reference_hash') != marker.reference_hash or digest(manifest) != score.manifest_hash):
            fail('GOLD_EVIDENCE_INVALID', 409)
        return Response({'id': score.pk, 'manifest': manifest, 'manifest_hash': score.manifest_hash})
