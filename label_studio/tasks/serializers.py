"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license."""

import logging
from collections.abc import MutableMapping

import ujson as json
from access_control.authorization import authorization
from access_control.identity import resolve_principal
from core.current_request import CurrentContext, get_current_request
from core.feature_flags import flag_set
from core.label_config import replace_task_data_undefined_with_config_field
from core.utils.common import load_func, retry_database_locked
from core.utils.db import fast_first
from core.utils.exceptions import extract_message
from django.conf import settings
from django.db import IntegrityError, transaction
from drf_spectacular.utils import extend_schema_field
from fsm.serializer_fields import FSMStateField
from fsm.state_inference import get_or_infer_state
from fsm.utils import get_or_initialize_state, is_fsm_enabled
from label_studio_sdk.label_interface import LabelInterface
from organizations.models import OrganizationMember
from projects.models import Project, ProjectMember
from rest_flex_fields import FlexFieldsModelSerializer
from rest_framework import generics, serializers
from rest_framework.exceptions import ValidationError
from rest_framework.fields import SkipField
from rest_framework.serializers import ModelSerializer
from rest_framework.settings import api_settings
from tasks.exceptions import AnnotationDuplicateError
from tasks.models import Annotation, AnnotationDraft, Prediction, PredictionMeta, ReviewDecision, Submission, Task, TaskAssignment
from tasks.validation import TaskValidator
from users.models import User
from users.serializers import UserSerializer, UserSimpleSerializer

logger = logging.getLogger(__name__)


def sanitize_prediction_import_payload(prediction):
    """Drop only FSM `state` from prediction import payloads."""
    if not isinstance(prediction, MutableMapping):
        return prediction
    prediction.pop('state', None)
    return prediction


class PredictionQuerySerializer(serializers.Serializer):
    task = serializers.IntegerField(required=False, help_text='Task ID to filter predictions')
    project = serializers.IntegerField(required=False, help_text='Project ID to filter predictions')


@extend_schema_field(
    {
        'type': 'array',
        'title': 'Prediction result list',
        'description': 'List of prediction results for the task',
        'items': {
            'type': 'object',
            'title': 'Prediction result items (regions)',
            'description': 'List of predicted regions for the task',
        },
    }
)
class PredictionResultField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        'type': 'array',
        'title': 'Annotation result list',
        'description': 'List of annotation results for the task',
        'items': {
            'type': 'object',
            'title': 'Annotation result items (regions)',
            'description': 'List of annotated regions for the task',
        },
    }
)
class AnnotationResultField(serializers.JSONField):
    pass


class PredictionSerializer(ModelSerializer):
    result = PredictionResultField()
    model_version = serializers.CharField(
        allow_blank=True,
        required=False,
        help_text='Model version - tag for predictions that can be used to filter tasks in Data Manager, as well as '
        'select specific model version for showing preannotations in the labeling interface',
    )
    created_ago = serializers.CharField(default='', read_only=True, help_text='Delta time from creation time')

    def validate(self, data):
        """Validate prediction using LabelInterface against project configuration"""
        project = None
        if 'task' in data:
            project = data['task'].project
        elif 'project' in data:
            project = data['project']
        ff_user = project.organization.created_by if project else 'auto'

        if not flag_set('fflag_feat_utc_210_prediction_validation_15082025', user=ff_user):
            # Skip validation if feature flag is not set
            logger.info(f'Skipping prediction validation in PredictionSerializer for user {ff_user}')
            return super().validate(data)

        # Only validate if we're updating the result field
        if 'result' not in data:
            return data

        if not project:
            raise ValidationError('Project is required for prediction validation')

        # Validate prediction using LabelInterface
        li = LabelInterface(project.label_config)
        validation_errors = li.validate_prediction(data, return_errors=True)

        if validation_errors:
            raise ValidationError(f'Error validating prediction: {validation_errors}')

        return data

    class Meta:
        model = Prediction
        fields = '__all__'


class TaskAssignmentSerializer(serializers.ModelSerializer):
    assignee = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    assignee_identity = UserSimpleSerializer(source='assignee', read_only=True)

    class Meta:
        model = TaskAssignment
        fields = [
            'id',
            'task',
            'project',
            'assignee',
            'assignee_identity',
            'assigned_by',
            'annotation',
            'status',
            'version',
            'assigned_at',
            'updated_at',
        ]
        read_only_fields = [
            'project',
            'assignee_identity',
            'assigned_by',
            'annotation',
            'status',
            'version',
            'assigned_at',
            'updated_at',
        ]

    def validate(self, attrs):
        task = attrs['task']
        assignee = attrs['assignee']

        if task.project_id is None:
            raise ValidationError({'task': 'Task must belong to a project.'})

        is_creator = task.project.created_by_id == assignee.id
        has_active_org_membership = (
            assignee.is_active
            and OrganizationMember.objects.filter(
                organization=task.project.organization,
                user=assignee,
                deleted_at__isnull=True,
            ).exists()
        )
        has_role = ProjectMember.objects.filter(
            project=task.project,
            user=assignee,
            enabled=True,
            role__in=[ProjectMember.Role.ANNOTATOR, ProjectMember.Role.MANAGER],
        ).exists()

        if not has_active_org_membership or (not is_creator and not has_role):
            raise ValidationError({'assignee': 'Assignee must be an active annotator or manager in this project.'})

        if TaskAssignment.objects.filter(
            task=task,
            assignee=assignee,
            status__in=TaskAssignment.ACTIVE_STATUSES,
        ).exists():
            raise ValidationError({'assignee': 'This user already has an active assignment for the task.'})

        return attrs


class ReviewDecisionSerializer(serializers.ModelSerializer):
    reviewer = UserSerializer(read_only=True)

    class Meta:
        model = ReviewDecision
        fields = ['id', 'decision', 'reason', 'reviewer', 'created_at']
        read_only_fields = fields


class SubmissionSerializer(serializers.ModelSerializer):
    submitted_by = UserSerializer(read_only=True)
    review = ReviewDecisionSerializer(read_only=True)

    class Meta:
        model = Submission
        fields = [
            'id',
            'assignment',
            'annotation',
            'revision',
            'result_snapshot',
            'result_hash',
            'submitted_by',
            'submitted_at',
            'status',
            'review',
        ]
        read_only_fields = fields


class ReviewSubmissionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=ReviewDecision.Decision.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, attrs):
        if attrs['decision'] == ReviewDecision.Decision.REJECTED and not attrs.get('reason', '').strip():
            raise ValidationError({'reason': 'A rejection reason is required.'})
        return attrs


class ListAnnotationSerializer(serializers.ListSerializer):
    pass


class CompletedByDMSerializer(UserSerializer):
    class Meta:
        model = User
        fields = ['id', 'first_name', 'last_name', 'avatar', 'email', 'initials']


class AnnotationSerializer(FlexFieldsModelSerializer):
    """
    Annotation Serializer with FSM state support.

    Note: The 'state' field will be populated from the queryset annotation
    if present, preventing N+1 queries. Use .with_state() on your queryset.
    """

    state = FSMStateField(read_only=True)  # FSM state - automatically uses annotation if present
    """"""

    result = AnnotationResultField(required=False)
    created_username = serializers.SerializerMethodField(default='', read_only=True, help_text='Username string')
    created_ago = serializers.CharField(default='', read_only=True, help_text='Time delta from creation time')
    completed_by = serializers.PrimaryKeyRelatedField(required=False, queryset=User.objects.all())
    unique_id = serializers.CharField(required=False, write_only=True)
    assignment_id = serializers.IntegerField(required=False, write_only=True)
    assignment_version = serializers.IntegerField(required=False, write_only=True)
    submit_for_review = serializers.BooleanField(required=False, default=False, write_only=True)

    def create(self, validated_data):
        validated_data.pop('assignment_id', None)
        validated_data.pop('assignment_version', None)
        validated_data.pop('submit_for_review', None)
        try:
            return super().create(validated_data)
        except IntegrityError as e:
            errors = [
                'UNIQUE constraint failed: task_completion.unique_id',
                'duplicate key value violates unique constraint "task_completion_unique_id_key"',
            ]
            if any([error in str(e) for error in errors]):
                raise AnnotationDuplicateError()
            raise

    def update(self, instance, validated_data):
        # Annotation authorship is immutable through the generic update API.
        # updated_by is controlled by the authenticated server-side actor in
        # AnnotationAPI.update.
        validated_data.pop('completed_by', None)
        validated_data.pop('updated_by', None)
        validated_data.pop('assignment_id', None)
        validated_data.pop('assignment_version', None)
        validated_data.pop('submit_for_review', None)
        return super().update(instance, validated_data)

    def validate_result(self, value):
        data = value
        # convert from str to json if need
        if isinstance(value, str):
            try:
                data = json.loads(value)
            except:  # noqa: E722
                raise ValueError('annotation "result" can\'t be parse from str to JSON')

        # check result is list
        if not isinstance(data, list):
            raise ValidationError('annotation "result" field in annotation must be list')

        return data

    def get_created_username(self, annotation) -> str:
        user = annotation.completed_by
        if not user:
            return ''

        name = user.first_name
        if len(user.last_name):
            name = name + ' ' + user.last_name

        name += f' {user.email}, {user.id}'
        return name

    def to_representation(self, obj):
        """Remove state field if feature flags are disabled"""
        ret = super().to_representation(obj)
        user = CurrentContext.get_user()
        if not (
            flag_set('fflag_feat_fit_568_finite_state_management', user=user)
            and flag_set('fflag_feat_fit_710_fsm_state_fields', user=user)
        ):
            ret.pop('state', None)
        return ret

    class Meta:
        model = Annotation
        exclude = ['prediction', 'result_count']
        expandable_fields = {'completed_by': (CompletedByDMSerializer,)}


class AnnotationStubSerializer(FlexFieldsModelSerializer):
    """
    Lightweight Annotation Serializer for lazy loading.

    Returns only minimal metadata needed for annotation list display.
    Used when fflag_fix_all_fit_720_lazy_load_annotations is enabled
    to improve performance for tasks with many annotations.

    Fields included:
    - id: for selection and hydration
    - created_username: for display in annotation list
    - created_ago: for display in annotation list (relative time string)
    - created_at: for TimeAgo component (actual timestamp)
    - completed_by: user id for avatar lookup
    - ground_truth: for showing star indicator
    - was_cancelled: for skip queue / cancel-skip button display
    - is_stub: signals frontend to fetch full data on selection
    """

    created_username = serializers.SerializerMethodField(default='', read_only=True, help_text='Username string')
    created_ago = serializers.CharField(default='', read_only=True, help_text='Time delta from creation time')
    completed_by = serializers.PrimaryKeyRelatedField(required=False, queryset=User.objects.all())
    # Mark this as a stub so frontend knows to fetch full data on selection
    is_stub = serializers.SerializerMethodField(read_only=True)

    def get_created_username(self, annotation) -> str:
        user = annotation.completed_by
        if not user:
            return ''

        name = user.first_name
        if len(user.last_name):
            name = name + ' ' + user.last_name

        name += f' {user.email}, {user.id}'
        return name

    def get_is_stub(self, annotation) -> bool:
        return True

    class Meta:
        model = Annotation
        # Minimal fields for annotation list display only
        # ground_truth, created_at, and was_cancelled are simple model fields (no extra query)
        fields = [
            'id',
            'created_username',
            'created_ago',
            'created_at',
            'completed_by',
            'ground_truth',
            'was_cancelled',
            'is_stub',
        ]


class TaskSimpleSerializer(FlexFieldsModelSerializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['annotations'] = AnnotationSerializer(many=True, default=[], context=self.context, read_only=True)
        self.fields['predictions'] = PredictionSerializer(many=True, default=[], context=self.context, read_only=True)

    def to_representation(self, instance):
        project = instance.project
        if project:
            # resolve $undefined$ key in task data
            data = instance.data
            replace_task_data_undefined_with_config_field(data, project)

        return super().to_representation(instance)

    class Meta:
        model = Task
        exclude = ('precomputed_agreement', 'allow_skip')


class BaseTaskSerializer(FlexFieldsModelSerializer):
    """Task Serializer with project scheme configs validation"""

    def project(self, task=None):
        """Take the project from context"""
        if 'project' in self.context:
            project = self.context['project']
        elif 'view' in self.context and 'project_id' in self.context['view'].kwargs:
            kwargs = self.context['view'].kwargs
            project = generics.get_object_or_404(Project, kwargs['project_id'])
        elif task:
            project = task.project
        else:
            project = None
        return project

    def validate(self, task):
        instance = self.instance if hasattr(self, 'instance') else None

        project = self.project(task=instance)

        current_request = get_current_request()
        if current_request and current_request.method == 'POST' and not project:
            # raise ValidationError for the project field with standard DRF message
            try:
                self.fields['project'].fail('required')
            except ValidationError as exc:
                raise ValidationError(
                    {
                        'project': exc.detail,
                    }
                )

        validator = TaskValidator(
            project,
            instance=instance if 'data' not in task else None,
        )
        return validator.validate(task)

    def to_representation(self, instance):
        project = self.project(instance)
        if project:
            # resolve uri for storage (s3/gcs/etc)
            if self.context.get('resolve_uri', False):
                instance.data = instance.resolve_uri(instance.data, project)

            # resolve $undefined$ key in task data
            data = instance.data
            replace_task_data_undefined_with_config_field(data, project)

        return super().to_representation(instance)

    class Meta:
        model = Task
        exclude = ('precomputed_agreement', 'allow_skip')


class BaseTaskSerializerBulk(serializers.ListSerializer):
    """Serialize task with annotation from source json data"""

    annotations = AnnotationSerializer(many=True, default=[], read_only=True)
    predictions = PredictionSerializer(many=True, default=[], read_only=True)

    @property
    def project(self):
        return self.context.get('project')

    @staticmethod
    def format_error(i, detail, item):
        if len(detail) == 1:
            code = f' {detail[0].code}' if detail[0].code != 'invalid' else ''
            return f'Error{code} at item {i}: {detail[0]} :: {item}'
        else:
            errors = ', '.join(detail)
            codes = [d.code for d in detail]
            return f'Errors {codes} at item {i}: {errors} :: {item}'

    def to_internal_value(self, data):
        """Body of run_validation for all data items"""
        if data is None:
            raise ValidationError('All tasks are empty (None)')

        if not isinstance(data, list):
            raise ValidationError({api_settings.NON_FIELD_ERRORS_KEY: 'not a list'}, code='not_a_list')

        if not self.allow_empty and len(data) == 0:
            if self.parent and self.partial:
                raise SkipField()
            raise ValidationError({api_settings.NON_FIELD_ERRORS_KEY: 'empty'}, code='empty')

        ret, errors = [], []
        self.annotation_count, self.prediction_count = 0, 0
        for i, item in enumerate(data):
            try:
                validated = self.child.validate(item)
            except ValidationError as exc:
                error = self.format_error(i, exc.detail, item)
                errors.append(error)
                # do not print to user too many errors
                if len(errors) >= 100:
                    errors[99] = '...'
                    break
            else:
                ret.append(validated)
                errors.append({})

                if 'annotations' in item:
                    self.annotation_count += len(item['annotations'])
                if 'predictions' in item:
                    self.prediction_count += len(item['predictions'])

        if any(errors):
            logger.warning("Can't deserialize tasks due to " + str(errors))
            raise ValidationError(errors)

        return ret

    @staticmethod
    def _insert_valid_completed_by(annotations, members_email_to_id, members_ids, default_user):
        """Bind imported annotation authorship to the trusted importing actor.

        Ordinary project imports are write operations, not a privileged audit-log
        restoration channel. Nested completed_by values are therefore ignored
        rather than allowed to impersonate another organization member.
        """
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            annotation['completed_by_id'] = default_user.id
            annotation.pop('completed_by', None)

    @staticmethod
    def _insert_valid_user_reviews(dicts, members_email_to_id, default_user):
        """Insert correct user id by email from snapshot

        :param dicts: draft or review dicts from snapshot
        :param members_email_to_id: mapping from emails to current LS instance user IDs
        :param default_user: if email is not found in membr_email_to_id, this user will be used
        :return:
        """
        for obj in dicts:
            created_by = obj.get('created_by', {})
            email = created_by.get('email') if isinstance(created_by, dict) else None

            # user default user
            if email not in members_email_to_id:
                obj['created_by_id'] = default_user.id
                logger.warning('Email not found in members_email_to_id, default user used instead')

            # resolve annotators by email
            else:
                obj['created_by_id'] = members_email_to_id[email]

            obj.pop('created_by', None)

    @staticmethod
    def _insert_valid_user_drafts(dicts, members_email_to_id, default_user):
        """Insert correct user id by email from snapshot

        :param dicts: draft or review dicts from snapshot
        :param members_email_to_id: mapping from emails to current LS instance user IDs
        :param default_user: if email is not found in membr_email_to_id, this user will be used
        :return:
        """
        for obj in dicts:
            email = obj.get('user')

            # user default user
            if email not in members_email_to_id:
                obj['user_id'] = default_user.id
                logger.warning('Email not found in members_email_to_id, default user used instead')

            # resolve annotators by email
            else:
                obj['user_id'] = members_email_to_id[email]

            obj.pop('user', None)

    @retry_database_locked()
    def create(self, validated_data):
        """Create Tasks, Annotations, etc in bulk"""
        validated_tasks = validated_data
        logging.info(f'Try to serialize tasks with annotations, data len = {len(validated_data)}')
        user = self.context.get('user', None)
        default_user = user or self.project.created_by
        ff_user = self.project.organization.created_by

        # get members from project, we need them to restore annotation.completed_by etc
        organization = self.project.organization
        members_email_to_id = dict(organization.members.values_list('user__email', 'user__id'))
        members_ids = set(members_email_to_id.values())
        logger.debug(f'{len(members_email_to_id)} members found in organization {organization}')

        # to be sure we add tasks with annotations at the same time
        with transaction.atomic():
            # extract annotations, predictions, drafts, reviews, etc
            # all these lists will be grouped by tasks, e.g.:
            # task_annotations = [ [a1, a2], [a3, a4, a5], ... ]
            task_annotations, task_predictions = [], []
            task_drafts, task_reviews = [], []
            for task in validated_tasks:
                # extract annotations from snapshot
                annotations = task.pop('annotations', [])
                self._insert_valid_completed_by(annotations, members_email_to_id, members_ids, default_user)
                task_annotations.append(annotations)

                # extract predictions from snapshot
                predictions = task.pop('predictions', [])
                task_predictions.append(predictions)

                if flag_set('fflag_feat_back_lsdv_5307_import_reviews_drafts_29062023_short', user=ff_user):
                    # extract drafts from snapshot
                    drafts = task.pop('drafts', [])
                    self._insert_valid_user_drafts(drafts, members_email_to_id, default_user)
                    task_drafts.append(drafts)

                    # extract reviews from snapshot annotations
                    for annotation in annotations:
                        reviews = annotation.get('reviews', [])
                        self._insert_valid_user_reviews(reviews, members_email_to_id, default_user)
                        task_reviews.append(reviews)

            db_tasks = self.add_tasks(task_annotations, task_predictions, validated_tasks)
            db_annotations = self.add_annotations(task_annotations, user)
            prediction_errors = self.add_predictions(task_predictions)

            raise_prediction_errors = True
            if not flag_set('fflag_feat_utc_210_prediction_validation_15082025', user=ff_user):
                raise_prediction_errors = False

            # If there are prediction validation errors, raise them
            if prediction_errors and raise_prediction_errors:
                raise ValidationError({'predictions': prediction_errors})

        self.post_process_annotations(user, db_annotations, 'imported')
        self.post_process_tasks(self.project.id, [t.id for t in self.db_tasks])
        self.post_process_custom_callback(self.project.id, user)

        if flag_set('fflag_feat_back_lsdv_5307_import_reviews_drafts_29062023_short', user=ff_user):
            with transaction.atomic():
                # build mapping between new and old ids in annotations,
                # we need it because annotation ids will be known only after saving to db
                annotation_mapping = {v.import_id: v.id for v in db_annotations}
                annotation_mapping[None] = None
                # the sequence of add_ functions is very important because of references to ids
                self.add_drafts(task_drafts, db_tasks, annotation_mapping, self.project)
                self.add_reviews(task_reviews, annotation_mapping, self.project)

        # Backfill FSM states for bulk-created tasks
        # bulk_create() bypasses save() so FSM transitions don't fire automatically
        # Do this after all child entities states(annotations, drafts, reviews) have been backfilled
        self._backfill_fsm_states(self.db_tasks)

        return db_tasks

    def add_predictions(self, task_predictions):
        """Save predictions to DB and set the latest model version in the project"""
        db_predictions = []
        validation_errors = []

        should_validate = self.project.label_config_is_not_default and flag_set(
            'fflag_feat_utc_210_prediction_validation_15082025', user=self.project.organization.created_by
        )

        # add predictions
        last_model_version = None
        for i, predictions in enumerate(task_predictions):
            for j, prediction in enumerate(predictions):
                if not isinstance(prediction, dict):
                    validation_errors.append(f'Task {i}, prediction {j}: Prediction must be a dictionary')
                    continue

                # Strip FSM state field from predictions before validation
                # Exported data may include 'state' which is not a valid prediction field
                ff_user = self.project.organization.created_by
                if flag_set('fflag_feat_fit_568_finite_state_management', user=ff_user) and flag_set(
                    'fflag_feat_fit_710_fsm_state_fields', user=ff_user
                ):
                    prediction.pop('state', None)

                # Validate prediction only when project label config is not default
                if should_validate:
                    try:
                        prediction = sanitize_prediction_import_payload(prediction)
                        li = LabelInterface(self.project.label_config) if should_validate else None
                        validation_errors_list = li.validate_prediction(prediction, return_errors=True)

                        if validation_errors_list:
                            # Format errors for better readability
                            for error in validation_errors_list:
                                validation_errors.append(f'Task {i}, prediction {j}: {error}')
                            continue

                    except Exception as e:
                        validation_errors.append(
                            f'Task {i}, prediction {j}: Error validating prediction - {extract_message(e)}'
                        )
                        continue

                try:
                    # we need to call result normalizer here since "bulk_create" doesn't call save() method
                    result = Prediction.prepare_prediction_result(prediction['result'], self.project)
                    prediction_score = prediction.get('score')
                    if prediction_score is not None:
                        try:
                            prediction_score = float(prediction_score)
                        except ValueError:
                            logger.error(
                                "Can't upload prediction score: should be in float format.Fallback to score=None"
                            )
                            prediction_score = None

                    last_model_version = prediction.get('model_version', 'undefined')
                    db_predictions.append(
                        Prediction(
                            task=self.db_tasks[i],
                            project=self.db_tasks[i].project,
                            result=result,
                            score=prediction_score,
                            model_version=last_model_version,
                        )
                    )
                except Exception as e:
                    validation_errors.append(
                        f'Task {i}, prediction {j}: Failed to create prediction - {extract_message(e)}'
                    )
                    continue

        # Return validation errors if they exist
        if validation_errors:
            return validation_errors

        # predictions: DB bulk create
        self.db_predictions = Prediction.objects.bulk_create(db_predictions, batch_size=settings.BATCH_SIZE)
        logging.info(f'Predictions serialization success, len = {len(self.db_predictions)}')

        # renew project model version if it's empty
        if not self.project.model_version and last_model_version is not None:
            self.project.model_version = last_model_version
            self.project.save()

        return None  # No errors

    def add_reviews(self, task_reviews, annotation_mapping, project):
        """Save task reviews to DB"""
        return []

    def add_drafts(self, task_drafts, db_tasks, annotation_mapping, project):
        """Save task drafts to DB"""
        db_drafts = []

        # add drafts
        for i, drafts in enumerate(task_drafts):
            for draft in drafts:
                if not isinstance(draft, dict):
                    continue

                draft.update(
                    {
                        'task_id': db_tasks[i].id,
                        'annotation_id': annotation_mapping[draft.get('annotation')],
                        'project': self.project,
                        'import_id': draft.get('id'),
                    }
                )
                # remove redundant fields
                [
                    draft.pop(field, None)
                    for field in ['id', 'task', 'annotation', 'project', 'created_username', 'created_ago']
                ]
                db_drafts.append(AnnotationDraft(**draft))

        self.db_drafts = AnnotationDraft.objects.bulk_create(db_drafts, batch_size=settings.BATCH_SIZE)
        logging.info(f'drafts serialization success, len = {len(self.db_drafts)}')

        # Backfill FSM states for bulk-created drafts
        # bulk_create() bypasses save() so FSM transitions don't fire automatically
        self._backfill_fsm_states(self.db_drafts)

        return self.db_drafts

    def add_annotations(self, task_annotations, user):
        """Save task annotations to DB"""
        db_annotations = []

        # add annotations
        for i, annotations in enumerate(task_annotations):
            for annotation in annotations:
                if not isinstance(annotation, dict):
                    continue

                ground_truth = annotation.pop('ground_truth', True)
                was_cancelled = annotation.pop('was_cancelled', False)
                lead_time = annotation.pop('lead_time', None)

                body = {
                    'task': self.db_tasks[i],
                    'project': self.project,
                    'ground_truth': ground_truth,
                    'was_cancelled': was_cancelled,
                    'completed_by_id': annotation['completed_by_id'],
                    'result': annotation['result'],
                    'lead_time': lead_time,
                    'import_id': annotation.get('id'),
                }
                db_annotations.append(Annotation(**body))

        # annotations: DB bulk create
        if settings.DJANGO_DB == settings.DJANGO_DB_SQLITE:
            self.db_annotations = []
            try:
                last_annotation = Annotation.objects.latest('id')
                current_id = last_annotation.id + 1
            except Annotation.DoesNotExist:
                current_id = 1

            for annotation in db_annotations:
                annotation.id = current_id
                current_id += 1
            self.db_annotations = Annotation.objects.bulk_create(db_annotations, batch_size=settings.BATCH_SIZE)
        else:
            self.db_annotations = Annotation.objects.bulk_create(db_annotations, batch_size=settings.BATCH_SIZE)
        logging.info(f'Annotations serialization success, len = {len(self.db_annotations)}')

        # Backfill FSM states for bulk-created annotations
        # bulk_create() bypasses save() so FSM transitions don't fire automatically
        self._backfill_fsm_states(self.db_annotations)

        return self.db_annotations

    def add_tasks(self, task_annotations, task_predictions, validated_tasks):
        """Extract tasks from validated_tasks and store them in DB"""
        db_tasks = []
        max_overlap = self.project.maximum_annotations

        # Acquire a lock on the project to ensure atomicity when calculating inner_id
        project = Project.objects.select_for_update().get(id=self.project.id)

        last_task = fast_first(Task.objects.filter(project=project).order_by('-inner_id'))
        prev_inner_id = last_task.inner_id if last_task else 0
        max_inner_id = (prev_inner_id + 1) if prev_inner_id else 1

        for i, task in enumerate(validated_tasks):
            cancelled_annotations = len([ann for ann in task_annotations[i] if ann.get('was_cancelled', False)])
            total_annotations = len(task_annotations[i]) - cancelled_annotations
            current_overlap = len(set([ann.get('completed_by_id') for ann in task_annotations[i]]))
            t = Task(
                project=self.project,
                data=task['data'],
                meta=task.get('meta', {}),
                overlap=max_overlap,
                is_labeled=current_overlap >= max_overlap,
                file_upload_id=task.get('file_upload_id'),
                inner_id=None if prev_inner_id is None else max_inner_id + i,
                total_predictions=len(task_predictions[i]),
                total_annotations=total_annotations,
                cancelled_annotations=cancelled_annotations,
                allow_skip=task.get('allow_skip', True),
            )
            db_tasks.append(t)

        # get task ids
        if settings.DJANGO_DB == settings.DJANGO_DB_SQLITE:
            self.db_tasks = []
            try:
                last_task = Task.objects.latest('id')
                current_id = last_task.id + 1
            except Task.DoesNotExist:
                current_id = 1

            for task in db_tasks:
                task.id = current_id
                current_id += 1
            self.db_tasks = Task.objects.bulk_create(db_tasks, batch_size=settings.BATCH_SIZE)
        else:
            self.db_tasks = Task.objects.bulk_create(db_tasks, batch_size=settings.BATCH_SIZE)

        logging.info(f'Tasks serialization success, len = {len(self.db_tasks)}')

        return db_tasks

    def _backfill_fsm_states(self, entities: list, overwrite_state=False):
        """
        Backfill FSM states for entities created via bulk_create().

        bulk_create() bypasses the model's save() method, so FSM transitions
        don't fire automatically. This sets initial state for newly imported entities.
        """
        user = CurrentContext.get_user()
        if not entities or not is_fsm_enabled(user):
            return

        for entity in entities:
            inferred_state = get_or_infer_state(entity)
            get_or_initialize_state(entity, user=user, inferred_state=inferred_state, overwrite_state=overwrite_state)

    @staticmethod
    def post_process_annotations(user, db_annotations, action):
        pass

    @staticmethod
    def post_process_tasks(user, db_tasks):
        pass

    @staticmethod
    def add_annotation_fields(body, user, action):
        return body

    @staticmethod
    def post_process_custom_callback(project_id, user):
        pass

    class Meta:
        model = Task
        fields = '__all__'


TaskSerializer = load_func(settings.TASK_SERIALIZER)


class TaskWithAnnotationsSerializer(TaskSerializer):
    """ """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['annotations'] = AnnotationSerializer(many=True, default=[], context=self.context)

    class Meta:
        model = Task
        list_serializer_class = load_func(settings.TASK_SERIALIZER_BULK)

        exclude = ()


class AnnotationDraftSerializer(ModelSerializer):
    """
    AnnotationDraft Serializer with FSM state support.

    Note: The 'state' field will be populated from the queryset annotation
    if present, preventing N+1 queries. Use .with_state() on your queryset.
    """

    state = FSMStateField(read_only=True)  # FSM state - automatically uses annotation if present
    user = serializers.CharField(read_only=True)
    assignment_id = serializers.IntegerField(required=False, write_only=True)
    assignment_version = serializers.IntegerField(required=False, write_only=True)
    created_username = serializers.SerializerMethodField(default='', read_only=True, help_text='User name string')
    created_ago = serializers.CharField(default='', read_only=True, help_text='Delta time from creation time')

    def create(self, validated_data):
        validated_data.pop('assignment_id', None)
        validated_data.pop('assignment_version', None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop('assignment_id', None)
        validated_data.pop('assignment_version', None)
        return super().update(instance, validated_data)

    def validate_annotation(self, annotation):
        # Upstream unskip detaches a draft before removing the skipped
        # annotation. Rebinding it to a different annotation is never allowed.
        if self.instance and annotation is not None and annotation.pk != self.instance.annotation_id:
            raise serializers.ValidationError('A draft cannot be moved to another annotation.')
        return annotation

    def get_created_username(self, draft):
        user = draft.user
        if not user:
            return ''

        name = user.first_name
        last_name = user.last_name
        if len(last_name):
            name = name + ' ' + last_name
        name += (' ' if name else '') + f'{user.email}, {user.id}'
        return name

    def to_representation(self, obj):
        """Remove state field if feature flags are disabled"""
        ret = super().to_representation(obj)
        user = CurrentContext.get_user()
        if not (
            flag_set('fflag_feat_fit_568_finite_state_management', user=user)
            and flag_set('fflag_feat_fit_710_fsm_state_fields', user=user)
        ):
            ret.pop('state', None)
        return ret

    class Meta:
        model = AnnotationDraft
        fields = '__all__'
        read_only_fields = ('task', 'assignment', 'revision')


class TaskWithAnnotationsAndPredictionsAndDraftsSerializer(TaskSerializer):
    can_label = serializers.SerializerMethodField(read_only=True)
    predictions = serializers.SerializerMethodField(default=[], read_only=True)
    assignment_id = serializers.SerializerMethodField(read_only=True)
    assignment_version = serializers.SerializerMethodField(read_only=True)
    annotations = serializers.SerializerMethodField(default=[], read_only=True)
    drafts = serializers.SerializerMethodField(default=[], read_only=True)
    updated_by = serializers.SerializerMethodField(default=[], read_only=True)

    def get_updated_by(self, task):
        return [{'user_id': task.updated_by_id}] if task.updated_by_id else []

    def get_can_label(self, task):
        request = self.context.get('request')
        return request is not None and authorization.can_label_task(resolve_principal(request), task)

    def _get_assignment(self, task):
        user = self._get_user()
        if user is None:
            return None
        return (
            TaskAssignment.objects.filter(
                task=task,
                assignee=user,
                status__in=TaskAssignment.ACTIVE_STATUSES,
            )
            .order_by('-assigned_at', '-id')
            .first()
        )

    def get_assignment_id(self, task):
        assignment = self._get_assignment(task)
        return assignment.id if assignment else None

    def get_assignment_version(self, task):
        assignment = self._get_assignment(task)
        return assignment.version if assignment else None

    def _get_user(self):
        if 'request' in self.context and hasattr(self.context['request'], 'user'):
            return self.context['request'].user

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if ret.get('assignment_id') is None:
            ret.pop('assignment_id', None)
            ret.pop('assignment_version', None)
        return ret

    def get_predictions(self, task):
        predictions = task.predictions
        user = self._get_user()
        if flag_set('ff_front_dev_1682_model_version_dropdown_070622_short', user=user or 'auto'):
            active_ml_backends = task.project.get_active_ml_backends()
            model_versions = active_ml_backends.values_list('model_version', flat=True)
            logger.debug(f'Selecting predictions from active ML backend model versions: {model_versions}')
            predictions = predictions.filter(model_version__in=model_versions)
        elif task.project.model_version:
            predictions = predictions.filter(model_version=task.project.model_version)
        return PredictionSerializer(predictions, many=True, read_only=True, default=[], context=self.context).data

    def get_annotations(self, task):
        """Return annotations only for the current user"""
        annotations = task.annotations

        user = self._get_user()
        if user and user.is_annotator:
            annotations = annotations.filter(completed_by=user)

        return AnnotationSerializer(annotations, many=True, read_only=True, default=[], context=self.context).data

    def get_drafts(self, task):
        """Return drafts only for the current user"""
        # it's for swagger documentation
        if not isinstance(task, Task):
            return AnnotationDraftSerializer(many=True)

        assignment = self._get_assignment(task)
        drafts = task.drafts.filter(user=self._get_user(), assignment=assignment) if assignment else task.drafts.none()

        return AnnotationDraftSerializer(drafts, many=True, read_only=True, default=[], context=self.context).data


class NextTaskSerializer(TaskWithAnnotationsAndPredictionsAndDraftsSerializer):
    unique_lock_id = serializers.SerializerMethodField()

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        assignment = self._get_assignment(instance)
        annotation = assignment.annotation if assignment and assignment.annotation_id else None
        if (
            annotation is not None
            and annotation.was_cancelled
            and annotation.task_id == instance.id
            and annotation.completed_by_id == assignment.assignee_id
        ):
            # Resume the current owner's skipped annotation through the editor's
            # existing selection/Unskip path, rather than create a second one.
            ret['annotations'] = [AnnotationSerializer(annotation, context=self.context).data]
            ret['default_selected_annotation'] = annotation.id
        return ret

    def get_unique_lock_id(self, task):
        user = self.context['request'].user
        lock = task.locks.filter(user=user).first()
        if lock:
            return lock.unique_id

    def get_predictions(self, task):
        predictions = task.get_predictions_for_prelabeling()
        return PredictionSerializer(predictions, many=True, read_only=True, default=[], context=self.context).data

    def get_annotations(self, task):
        result = []
        if self.context.get('annotations', False):
            # Support lazy loading of annotations (FIT-720)
            # When annotations_stub is True, return lightweight stubs without result field
            use_stub = self.context.get('annotations_stub', False)
            user = self.context['request'].user

            if use_stub:
                # Get annotations queryset and filter by user
                annotations = task.annotations
                if user.is_annotator:
                    annotations = annotations.filter(completed_by=user)
                else:
                    annotations = annotations.filter(completed_by=user)
                return AnnotationStubSerializer(annotations, many=True, read_only=True, context=self.context).data
            else:
                annotations = super().get_annotations(task)
                for annotation in annotations:
                    if annotation.get('completed_by') == user.id:
                        result.append(annotation)
        return result


class TaskIDWithAnnotationsAndPredictionsSerializer(ModelSerializer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['annotations'] = AnnotationSerializer(many=True, default=[], context=self.context)
        self.fields['predictions'] = PredictionSerializer(many=True, default=[], context=self.context)

    class Meta:
        model = Task
        fields = ['id', 'annotations', 'predictions']


class TaskIDOnlySerializer(ModelSerializer):
    class Meta:
        model = Task
        fields = ['id']


class PredictionMetaSerializer(ModelSerializer):
    """Serializer for PredictionMeta model"""

    class Meta:
        model = PredictionMeta
        fields = '__all__'
        read_only_fields = ['prediction', 'failed_prediction']


# LSE inherits this serializer
TaskSerializerBulk = load_func(settings.TASK_SERIALIZER_BULK)
