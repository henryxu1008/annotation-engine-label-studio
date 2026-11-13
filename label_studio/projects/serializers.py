"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""
import bleach
from constants import SAFE_HTML_ATTRIBUTES, SAFE_HTML_TAGS
from django.db.models import Q
from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from fsm.serializer_fields import FSMStateField
from label_studio_sdk.label_interface import LabelInterface
from label_studio_sdk.label_interface.control_tags import (
    BrushLabelsTag,
    BrushTag,
    ChoicesTag,
    DateTimeTag,
    EllipseLabelsTag,
    EllipseTag,
    HyperTextLabelsTag,
    KeyPointLabelsTag,
    KeyPointTag,
    LabelsTag,
    NumberTag,
    ParagraphLabelsTag,
    PolygonLabelsTag,
    PolygonTag,
    RatingTag,
    RectangleLabelsTag,
    RectangleTag,
    TaxonomyTag,
    TextAreaTag,
    TimeSeriesLabelsTag,
    VideoRectangleTag,
)
from organizations.models import OrganizationMember
from projects.models import Project, ProjectImport, ProjectMember, ProjectOnboarding, ProjectReimport, ProjectSummary
from rest_flex_fields import FlexFieldsModelSerializer
from rest_framework import serializers
from rest_framework.serializers import SerializerMethodField
from tasks.models import Task
from users.models import User
from users.serializers import UserSimpleSerializer


@extend_schema_field({'type': 'object', 'additionalProperties': True})
class OpenApiObjectJSONField(serializers.JSONField):
    """
    A JSON field that is always rendered as a generic OpenAPI object.

    drf-spectacular may otherwise produce a schema with only metadata (e.g. nullable/readOnly/description)
    and omit `type`/`$ref`, which breaks some OpenAPI doc renderers.
    """


class ProjectMemberSerializer(serializers.ModelSerializer):
    user = UserSimpleSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        source='user',
        write_only=True,
    )

    class Meta:
        model = ProjectMember
        fields = ['id', 'project', 'user', 'user_id', 'role', 'enabled', 'created_at', 'updated_at']
        read_only_fields = ['id', 'project', 'created_at', 'updated_at']

    def validate(self, attrs):
        if self.instance is not None and 'user' in attrs:
            if attrs['user'].pk != self.instance.user_id:
                raise serializers.ValidationError({'user_id': 'Project member identity cannot be changed.'})
            attrs.pop('user')

        project = self.context.get('project')
        user = attrs.get('user') or getattr(self.instance, 'user', None)
        if project is None or user is None:
            return attrs

        belongs_to_org = OrganizationMember.objects.filter(
            organization=project.organization,
            user=user,
            deleted_at__isnull=True,
        ).exists()
        if not belongs_to_org:
            raise serializers.ValidationError({'user_id': 'User must be an active member of the project organization.'})

        duplicate = ProjectMember.objects.filter(project=project, user=user)
        if self.instance is not None:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError({'user_id': 'User is already a member of this project.'})

        if project.created_by_id == user.id:
            requested_role = attrs.get('role', getattr(self.instance, 'role', ProjectMember.Role.MANAGER))
            requested_enabled = attrs.get('enabled', getattr(self.instance, 'enabled', True))
            if requested_role != ProjectMember.Role.MANAGER or not requested_enabled:
                raise serializers.ValidationError({'user_id': 'Project creator must remain an enabled manager.'})

        return attrs


class CreatedByFromContext:
    requires_context = True

    def __call__(self, serializer_field):
        return serializer_field.context.get('created_by')


@extend_schema_serializer(deprecate_fields=['show_ground_truth_first'])
class ProjectSerializer(FlexFieldsModelSerializer):
    """Serializer get numbers from project queryset annotation,
    make sure, that you use correct one(Project.objects.with_counts())
    """

    task_number = serializers.IntegerField(default=None, read_only=True, help_text='Total task number in project')
    total_annotations_number = serializers.IntegerField(
        default=None,
        read_only=True,
        help_text='Total annotations number in project including '
        'skipped_annotations_number and ground_truth_number.',
    )
    total_predictions_number = serializers.IntegerField(
        default=None,
        read_only=True,
        help_text='Total predictions number in project including '
        'skipped_annotations_number, ground_truth_number, and '
        'useful_annotation_number.',
    )
    useful_annotation_number = serializers.IntegerField(
        default=None,
        read_only=True,
        help_text='Useful annotation number in project not including '
        'skipped_annotations_number and ground_truth_number. '
        'Total annotations = annotation_number + '
        'skipped_annotations_number + ground_truth_number',
    )
    ground_truth_number = serializers.IntegerField(
        default=None, read_only=True, help_text='Honeypot annotation number in project'
    )
    skipped_annotations_number = serializers.IntegerField(
        default=None, read_only=True, help_text='Skipped by collaborators annotation number in project'
    )
    num_tasks_with_annotations = serializers.IntegerField(
        default=None, read_only=True, help_text='Tasks with annotations count'
    )

    created_by = UserSimpleSerializer(default=CreatedByFromContext(), help_text='Project owner')

    control_weights = OpenApiObjectJSONField(
        required=False, allow_null=True, help_text='Dict of weights for each control tag in metric calculation.'
    )
    parsed_label_config = OpenApiObjectJSONField(
        default=None, read_only=True, help_text='JSON-formatted labeling configuration'
    )
    start_training_on_annotation_update = SerializerMethodField(
        default=None, read_only=False, help_text='Start model training after any annotations are submitted or updated'
    )
    config_has_control_tags = SerializerMethodField(
        default=None, read_only=True, help_text='Flag to detect is project ready for labeling'
    )
    config_suitable_for_bulk_annotation = serializers.SerializerMethodField(
        default=None, read_only=True, help_text='Flag to detect is project ready for bulk annotation'
    )
    finished_task_number = serializers.IntegerField(default=None, read_only=True, help_text='Finished tasks')

    queue_total = serializers.SerializerMethodField()
    queue_done = serializers.SerializerMethodField()
    state = FSMStateField(read_only=True)  # FSM state - automatically uses annotation if present

    def _participant_projection(self, instance):
        from classification_disagreements.blind_participant_projection import participant_counts, participant_for_project

        cache = getattr(self, '_blind_projection_cache', None)
        if cache is None:
            cache = self._blind_projection_cache = {}
        if instance.pk not in cache:
            actor = participant_for_project(self.context.get('request'), instance)
            cache[instance.pk] = participant_counts(instance, actor) if actor is not None else None
        return cache[instance.pk]

    def to_representation(self, instance):
        projected = self._participant_projection(instance)
        result = super().to_representation(instance)
        request = self.context.get('request')
        if request is not None:
            from access_control.authorization import authorization
            from access_control.identity import resolve_principal

            user = getattr(request, 'user', None)
            if (not getattr(user, 'is_authenticated', False)
                    or not authorization.can_manage_project(resolve_principal(request), instance)):
                result.pop('task_data_login', None)
                result.pop('task_data_password', None)
        if projected is not None:
            for field, value in projected.items():
                if field in result:
                    result[field] = value
        return result

    @property
    def user_id(self):
        try:
            return self.context['request'].user.id
        except KeyError:
            return next(iter(self.context['user_cache']))

    @staticmethod
    def get_config_has_control_tags(project) -> bool:
        return len(project.get_parsed_config()) > 0

    @staticmethod
    def get_config_suitable_for_bulk_annotation(project) -> bool:
        li = LabelInterface(project.label_config)

        # List of tags that should not be present
        disallowed_tags = [
            LabelsTag,
            BrushTag,
            BrushLabelsTag,
            EllipseTag,
            EllipseLabelsTag,
            KeyPointTag,
            KeyPointLabelsTag,
            PolygonTag,
            PolygonLabelsTag,
            RectangleTag,
            RectangleLabelsTag,
            HyperTextLabelsTag,
            ParagraphLabelsTag,
            TimeSeriesLabelsTag,
            VideoRectangleTag,
        ]

        # Return False if any disallowed tag is present
        for tag_class in disallowed_tags:
            if li.find_tags_by_class(tag_class):
                return False

        # Check perRegion/perItem for expanded list of tags, plus value="no" for Choices/Taxonomy
        allowed_tags_for_checks = [ChoicesTag, TaxonomyTag, DateTimeTag, NumberTag, RatingTag, TextAreaTag]
        for tag_class in allowed_tags_for_checks:
            tags = li.find_tags_by_class(tag_class)
            for tag in tags:
                per_region = tag.attr.get('perRegion', 'false').lower() == 'true'
                per_item = tag.attr.get('perItem', 'false').lower() == 'true'
                if per_region or per_item:
                    return False
                # For ChoicesTag and TaxonomyTag, the value attribute must not be set at all
                if tag_class in [ChoicesTag, TaxonomyTag]:
                    if 'value' in tag.attr:
                        return False

        # For TaxonomyTag, check labeling and apiUrl
        taxonomy_tags = li.find_tags_by_class(TaxonomyTag)
        for tag in taxonomy_tags:
            labeling = tag.attr.get('labeling', 'false').lower() == 'true'
            if labeling:
                return False
            api_url = tag.attr.get('apiUrl', None)
            if api_url is not None:
                return False

        # If all checks pass, return True
        return True

    @staticmethod
    def get_parsed_label_config(project):
        return project.get_parsed_config()

    def get_start_training_on_annotation_update(self, instance) -> bool:
        # FIXME: remake this logic with start_training_on_annotation_update
        return True if instance.min_annotations_to_start_training else False

    def to_internal_value(self, data):
        # FIXME: remake this logic with start_training_on_annotation_update
        initial_data = data
        data = super().to_internal_value(data)

        if 'start_training_on_annotation_update' in initial_data:
            data['min_annotations_to_start_training'] = int(initial_data['start_training_on_annotation_update'])

        if 'expert_instruction' in initial_data:
            data['expert_instruction'] = bleach.clean(
                initial_data['expert_instruction'], tags=SAFE_HTML_TAGS, attributes=SAFE_HTML_ATTRIBUTES
            )

        return data

    def validate_color(self, value):
        # color : "#FF4C25"
        if value.startswith('#') and len(value) == 7:
            try:
                int(value[1:], 16)
                return value
            except ValueError:
                pass
        raise serializers.ValidationError('Color must be in "#RRGGBB" format')

    class Meta:
        model = Project
        extra_kwargs = {
            'memberships': {'required': False},
            'title': {'required': False},
            'created_by': {'required': False},
        }
        fields = [
            'id',
            'title',
            'description',
            'label_config',
            'expert_instruction',
            'show_instruction',
            'show_skip_button',
            'enable_empty_annotation',
            'show_annotation_history',
            'organization',
            'color',
            'maximum_annotations',
            'is_published',
            'model_version',
            'is_draft',
            'created_by',
            'created_at',
            'min_annotations_to_start_training',
            'start_training_on_annotation_update',
            'show_collab_predictions',
            'num_tasks_with_annotations',
            'task_number',
            'useful_annotation_number',
            'ground_truth_number',
            'skipped_annotations_number',
            'total_annotations_number',
            'total_predictions_number',
            'sampling',
            'show_ground_truth_first',
            'annotator_evaluation_enabled',
            'show_overlap_first',
            'overlap_cohort_percentage',
            'task_data_login',
            'task_data_password',
            'control_weights',
            'parsed_label_config',
            'evaluate_predictions_automatically',
            'config_has_control_tags',
            'skip_queue',
            'reveal_preannotations_interactively',
            'pinned_at',
            'finished_task_number',
            'queue_total',
            'queue_done',
            'config_suitable_for_bulk_annotation',
            'state',
        ]

    def validate_label_config(self, value):
        if self.instance is None:
            # No project created yet
            Project.validate_label_config(value)
        else:
            # Existing project is updated
            self.instance.validate_config(value)
        return value

    def validate_model_version(self, value):
        """Custom model_version validation"""
        p = self.instance

        # Only run the validation if model_version is about to change
        # and it contains a string
        if p is not None and p.model_version != value and value != '':
            # that model_version should either match live ml backend
            # or match version in predictions

            if p.ml_backends.filter(title=value).union(p.predictions.filter(project=p, model_version=value)).exists():
                return value
            else:
                raise serializers.ValidationError(
                    "Model version doesn't exist either as live model or as static predictions."
                )

        return value

    def update(self, instance, validated_data):
        if validated_data.get('show_collab_predictions') is False:
            instance.model_version = ''

        return super().update(instance, validated_data)

    def get_queue_total(self, project) -> int:
        projected = self._participant_projection(project)
        if projected is not None:
            return projected['queue_total']
        from projects.functions import quality_task_ids

        remain = project.tasks.filter(
            Q(is_labeled=False) & ~Q(annotations__completed_by_id=self.user_id)
            | Q(annotations__completed_by_id=self.user_id)
        ).exclude(pk__in=quality_task_ids()).distinct()
        return remain.count()

    def get_queue_done(self, project) -> int:
        projected = self._participant_projection(project)
        if projected is not None:
            return projected['queue_done']
        from projects.functions import quality_task_ids

        tasks_filter = {
            'project': project,
            'annotations__completed_by_id': self.user_id,
        }

        if project.skip_queue == project.SkipQueue.REQUEUE_FOR_ME:
            tasks_filter['annotations__was_cancelled'] = False

        already_done_tasks = Task.objects.filter(**tasks_filter).exclude(pk__in=quality_task_ids())
        result = already_done_tasks.distinct().count()

        return result


class ProjectCountsSerializer(ProjectSerializer):
    class Meta:
        model = Project
        fields = [
            'id',
            'task_number',
            'finished_task_number',
            'total_predictions_number',
            'total_annotations_number',
            'num_tasks_with_annotations',
            'useful_annotation_number',
            'ground_truth_number',
            'skipped_annotations_number',
        ]


class ProjectOnboardingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectOnboarding
        fields = '__all__'


class ProjectLabelConfigSerializer(serializers.Serializer):
    label_config = serializers.CharField(help_text=Project.label_config.field.help_text)

    def validate_label_config(self, config):
        Project.validate_label_config(config)
        return config


class ProjectSummarySerializer(serializers.ModelSerializer):
    def to_representation(self, instance):
        result = super().to_representation(instance)
        from classification_disagreements.blind_participant_projection import participant_for_project, participant_summary

        if participant_for_project(self.context.get('request'), instance.project) is not None:
            result.update(participant_summary(instance.project))
        return result

    class Meta:
        model = ProjectSummary
        fields = '__all__'


class ProjectImportSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectImport
        fields = [
            'id',
            'project',
            'preannotated_from_fields',
            'commit_to_project',
            'return_task_ids',
            'status',
            'url',
            'error',
            'created_at',
            'updated_at',
            'finished_at',
            'task_count',
            'annotation_count',
            'prediction_count',
            'duration',
            'file_upload_ids',
            'could_be_tasks_list',
            'found_formats',
            'data_columns',
            'tasks',
            'task_ids',
        ]


class ProjectReimportSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectReimport
        fields = [
            'id',
            'project',
            'status',
            'error',
            'task_count',
            'annotation_count',
            'prediction_count',
            'duration',
            'file_upload_ids',
            'files_as_tasks_list',
            'found_formats',
            'data_columns',
        ]


class ProjectModelVersionExtendedSerializer(serializers.Serializer):
    model_version = serializers.CharField()
    count = serializers.IntegerField()
    latest = serializers.DateTimeField()


class ProjectModelVersionParamsSerializer(serializers.Serializer):
    extended = serializers.BooleanField(required=False, default=False)
    include_live_models = serializers.BooleanField(required=False, default=False)
    limit = serializers.IntegerField(required=False, default=None)


class GetFieldsSerializer(serializers.Serializer):
    include = serializers.CharField(
        required=False,
        help_text=(
            'Comma-separated list of count fields to include in the response to optimize performance. '
            'Available fields: task_number, finished_task_number, total_predictions_number, '
            'total_annotations_number, num_tasks_with_annotations, useful_annotation_number, '
            'ground_truth_number, skipped_annotations_number. If not specified, all count fields are included.'
        ),
    )
    filter = serializers.CharField(
        required=False,
        default='all',
        help_text=(
            "Filter projects by pinned status. Use 'pinned_only' to return only pinned projects, "
            "'exclude_pinned' to return only non-pinned projects, or 'all' to return all projects."
        ),
    )
    search = serializers.CharField(
        required=False, default=None, help_text='Search term for project title and description'
    )

    def validate_include(self, value):
        if value is not None:
            value = value.split(',')
        return value

    def validate_filter(self, value):
        if value in ['all', 'pinned_only', 'exclude_pinned']:
            return value
