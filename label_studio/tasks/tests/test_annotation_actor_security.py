from types import SimpleNamespace
from unittest.mock import patch

from access_control.identity import Principal
from organizations.tests.factories import OrganizationFactory
from projects.models import ProjectMember
from projects.tests.factories import ProjectFactory
from rest_framework.test import APITestCase

from data_export.models import Export
from data_import.api import ReImportAPI
from data_import.functions import async_reimport_background
from projects.models import ProjectReimport
from tasks.models import Annotation, AnnotationDraft, TaskAssignment
from tasks.tests.factories import AnnotationFactory, TaskFactory
from users.tests.factories import UserFactory


class TestAnnotationActorSecurity(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = OrganizationFactory()
        cls.project = ProjectFactory(organization=cls.organization)
        cls.actor = cls.organization.created_by
        cls.other_user = UserFactory(active_organization=cls.organization)
        cls.organization.add_user(cls.other_user)

    def setUp(self):
        self.client.force_authenticate(user=self.actor)

    def test_create_annotation_ignores_spoofed_completed_by(self):
        task = TaskFactory(project=self.project)

        assignment = TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=self.actor,
            assigned_by=self.actor,
        )

        response = self.client.post(
            f"/api/tasks/{task.id}/annotations/",
            data={
                "result": [],
                "completed_by": self.other_user.id,
                "updated_by": self.other_user.id,
                "assignment_id": assignment.id,
                "assignment_version": assignment.version,
            },
            format="json",
        )

        assert response.status_code == 201
        annotation = Annotation.objects.get(pk=response.json()["id"])
        assert annotation.completed_by_id == self.actor.id
        assert annotation.updated_by_id == self.actor.id

    def test_update_annotation_cannot_change_actor_fields(self):
        task = TaskFactory(project=self.project)
        annotation = AnnotationFactory(
            task=task,
            project=self.project,
            completed_by=self.actor,
            updated_by=self.actor,
            result=[],
        )
        assignment = TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=self.actor,
            assigned_by=self.actor,
            annotation=annotation,
            status=TaskAssignment.Status.IN_PROGRESS,
        )

        response = self.client.patch(
            f"/api/annotations/{annotation.id}/",
            data={
                "result": [],
                "completed_by": self.other_user.id,
                "updated_by": self.other_user.id,
                "assignment_id": assignment.id,
                "assignment_version": assignment.version,
            },
            format="json",
        )

        assert response.status_code == 200
        annotation.refresh_from_db()
        assert annotation.completed_by_id == self.actor.id
        assert annotation.updated_by_id == self.actor.id


    def test_bulk_import_ignores_spoofed_completed_by(self):
        response = self.client.post(
            f"/api/projects/{self.project.id}/import",
            data=[
                {
                    "data": {"text": "trusted import actor"},
                    "annotations": [
                        {
                            "result": [],
                            "completed_by": self.other_user.id,
                        }
                    ],
                }
            ],
            format="json",
        )

        assert response.status_code == 201, response.json()
        annotation = Annotation.objects.filter(project=self.project).latest("id")
        assert annotation.completed_by_id == self.actor.id
        assert annotation.completed_by_id != self.other_user.id

    def test_custom_identity_provider_mapped_actor_owns_annotation_audit_fields(self):
        mapped_actor = UserFactory(active_organization=self.organization)
        self.organization.add_user(mapped_actor)
        ProjectMember.objects.create(
            project=self.project,
            user=mapped_actor,
            role=ProjectMember.Role.ANNOTATOR,
        )
        task = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=mapped_actor,
            assigned_by=self.actor,
        )

        class MappedProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{mapped_actor.id}",
                    source="test-mapped",
                    username=mapped_actor.email,
                    local_user_id=mapped_actor.id,
                )

        with patch("access_control.identity.get_identity_provider", return_value=MappedProvider()):
            create_response = self.client.post(
                f"/api/tasks/{task.id}/annotations/",
                data={
                    "result": [],
                    "completed_by": self.actor.id,
                    "updated_by": self.actor.id,
                    "assignment_id": assignment.id,
                    "assignment_version": assignment.version,
                },
                format="json",
            )

            assert create_response.status_code == 201, create_response.json()
            annotation = Annotation.objects.get(pk=create_response.json()["id"])
            assert annotation.completed_by_id == mapped_actor.id
            assert annotation.updated_by_id == mapped_actor.id
            task.refresh_from_db()
            assert task.updated_by_id == mapped_actor.id

            update_response = self.client.patch(
                f"/api/annotations/{annotation.id}/",
                data={
                    "result": [],
                    "updated_by": self.actor.id,
                    "assignment_id": assignment.id,
                    "assignment_version": assignment.version,
                },
                format="json",
            )

        assert update_response.status_code == 200, update_response.json()
        assert update_response.json()["updated_by"] == mapped_actor.id
        annotation.refresh_from_db()
        task.refresh_from_db()
        assert annotation.completed_by_id == mapped_actor.id
        assert annotation.updated_by_id == mapped_actor.id
        assert task.updated_by_id == mapped_actor.id

    def test_custom_identity_provider_mapped_actor_updates_task_audit_on_delete(self):
        mapped_actor = UserFactory(active_organization=self.organization)
        self.organization.add_user(mapped_actor)
        ProjectMember.objects.create(
            project=self.project,
            user=mapped_actor,
            role=ProjectMember.Role.ANNOTATOR,
        )
        task = TaskFactory(project=self.project)
        annotation = AnnotationFactory(
            task=task,
            project=self.project,
            completed_by=mapped_actor,
            updated_by=mapped_actor,
            result=[],
        )
        assignment = TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=mapped_actor,
            assigned_by=self.actor,
            annotation=annotation,
            status=TaskAssignment.Status.IN_PROGRESS,
        )
        draft = AnnotationDraft.objects.create(
            task=task,
            annotation=annotation,
            user=mapped_actor,
            assignment=assignment,
            result=[],
        )

        class MappedProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{mapped_actor.id}",
                    source="test-mapped",
                    username=mapped_actor.email,
                    local_user_id=mapped_actor.id,
                )

        with patch("access_control.identity.get_identity_provider", return_value=MappedProvider()):
            response = self.client.delete(
                f"/api/annotations/{annotation.id}/",
                data={
                    "draft_id": draft.id,
                    "draft_revision": draft.revision,
                    "assignment_id": assignment.id,
                    "assignment_version": assignment.version,
                },
                format="json",
            )

        assert response.status_code == 204
        task.refresh_from_db()
        assert task.updated_by_id == mapped_actor.id


    def test_data_manager_serializes_mapped_principal_assignment_and_annotations(self):
        mapped_actor = UserFactory(active_organization=self.organization)
        self.organization.add_user(mapped_actor)
        ProjectMember.objects.create(
            project=self.project,
            user=mapped_actor,
            role=ProjectMember.Role.ANNOTATOR,
        )
        task = TaskFactory(project=self.project)
        annotation = AnnotationFactory(
            task=task,
            project=self.project,
            completed_by=mapped_actor,
            updated_by=mapped_actor,
            result=[],
        )
        assignment = TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=mapped_actor,
            assigned_by=self.actor,
            annotation=annotation,
            status=TaskAssignment.Status.IN_PROGRESS,
        )
        draft = AnnotationDraft.objects.create(
            task=task,
            annotation=annotation,
            user=mapped_actor,
            assignment=assignment,
            result=[],
        )

        class MappedProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{mapped_actor.id}",
                    source="test-mapped",
                    username=mapped_actor.email,
                    local_user_id=mapped_actor.id,
                )

        with patch("access_control.identity.get_identity_provider", return_value=MappedProvider()):
            response = self.client.get(
                f"/api/tasks/?project={self.project.id}&page=1&page_size=100&fields=all"
            )

        assert response.status_code == 200, response.json()
        payload = response.json()
        item = next(task_item for task_item in payload["tasks"] if task_item["id"] == task.id)
        assert payload["total_annotations"] == 1
        assert item["total_annotations"] == 1
        assert item["assignment_id"] == assignment.id
        assert item["assignment_version"] == assignment.version
        assert {entry["id"] for entry in item["annotations"]} == {annotation.id}
        assert {entry["id"] for entry in item["drafts"]} == {draft.id}

    def test_convert_to_draft_uses_mapped_actor_as_owner(self):
        mapped_actor = UserFactory(active_organization=self.organization)
        self.organization.add_user(mapped_actor)
        ProjectMember.objects.create(
            project=self.project,
            user=mapped_actor,
            role=ProjectMember.Role.ANNOTATOR,
        )
        task = TaskFactory(project=self.project)
        annotation = AnnotationFactory(
            task=task,
            project=self.project,
            completed_by=mapped_actor,
            updated_by=mapped_actor,
            result=[],
        )
        assignment = TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=mapped_actor,
            assigned_by=self.actor,
            annotation=annotation,
            status=TaskAssignment.Status.IN_PROGRESS,
        )

        class MappedProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{mapped_actor.id}",
                    source="test-mapped",
                    username=mapped_actor.email,
                    local_user_id=mapped_actor.id,
                )

        with patch("access_control.identity.get_identity_provider", return_value=MappedProvider()):
            response = self.client.post(
                f"/api/annotations/{annotation.id}/convert-to-draft",
                data={},
                format="json",
            )

        assert response.status_code == 201, response.json()
        draft = AnnotationDraft.objects.get(pk=response.json()["id"])
        assert draft.user_id == mapped_actor.id
        assert draft.assignment_id == assignment.id
        assert not Annotation.objects.filter(pk=annotation.id).exists()

        with patch("access_control.identity.get_identity_provider", return_value=MappedProvider()):
            submit_response = self.client.post(
                f"/api/tasks/{task.id}/annotations/",
                data={
                    "result": [],
                    "draft_id": draft.id,
                    "draft_revision": draft.revision,
                    "assignment_id": assignment.id,
                    "assignment_version": assignment.version,
                },
                format="json",
            )

        assert submit_response.status_code == 201, submit_response.json()
        submitted = Annotation.objects.get(pk=submit_response.json()["id"])
        assert submitted.completed_by_id == mapped_actor.id
        assert not AnnotationDraft.objects.filter(pk=draft.id).exists()


    def test_data_manager_task_collection_uses_mapped_principal(self):
        mapped_actor = UserFactory(active_organization=self.organization)
        self.organization.add_user(mapped_actor)
        ProjectMember.objects.create(
            project=self.project,
            user=mapped_actor,
            role=ProjectMember.Role.ANNOTATOR,
        )
        assigned_task = TaskFactory(project=self.project)
        unassigned_task = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(
            task=assigned_task,
            project=self.project,
            assignee=mapped_actor,
            assigned_by=self.actor,
        )

        class MappedProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{mapped_actor.id}",
                    source="test-mapped",
                    username=mapped_actor.email,
                    local_user_id=mapped_actor.id,
                )

        with patch("access_control.identity.get_identity_provider", return_value=MappedProvider()):
            response = self.client.get(f"/api/tasks/?project={self.project.id}&page=1&page_size=100")

        assert response.status_code == 200, response.json()
        ids = {item["id"] for item in response.json()["tasks"]}
        assert assigned_task.id in ids
        assert unassigned_task.id not in ids
        assert response.json()["tasks"][0]["assignment_id"] == assignment.id

    def test_direct_draft_creation_uses_mapped_actor_owner(self):
        mapped_actor = UserFactory(active_organization=self.organization)
        self.organization.add_user(mapped_actor)
        ProjectMember.objects.create(
            project=self.project,
            user=mapped_actor,
            role=ProjectMember.Role.ANNOTATOR,
        )
        task = TaskFactory(project=self.project)
        assignment = TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=mapped_actor,
            assigned_by=self.actor,
        )

        class MappedProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{mapped_actor.id}",
                    source="test-mapped",
                    username=mapped_actor.email,
                    local_user_id=mapped_actor.id,
                )

        with patch("access_control.identity.get_identity_provider", return_value=MappedProvider()):
            response = self.client.post(
                f"/api/tasks/{task.id}/drafts",
                data={
                    "result": [],
                    "lead_time": 0,
                    "assignment_id": assignment.id,
                    "assignment_version": assignment.version,
                },
                format="json",
            )

        assert response.status_code == 201, response.json()
        draft = AnnotationDraft.objects.get(pk=response.json()["id"])
        assert draft.user_id == mapped_actor.id
        assert draft.assignment_id == assignment.id

    def test_export_snapshot_created_by_uses_mapped_manager_actor(self):
        mapped_manager = UserFactory(active_organization=self.organization)
        self.organization.add_user(mapped_manager)
        ProjectMember.objects.create(
            project=self.project,
            user=mapped_manager,
            role=ProjectMember.Role.MANAGER,
        )

        class MappedProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{mapped_manager.id}",
                    source="test-mapped",
                    username=mapped_manager.email,
                    local_user_id=mapped_manager.id,
                )

        with (
            patch("access_control.identity.get_identity_provider", return_value=MappedProvider()),
            patch.object(Export, "run_file_exporting"),
        ):
            response = self.client.post(
                f"/api/projects/{self.project.id}/exports/",
                data={},
                format="json",
            )

        assert response.status_code == 201, response.json()
        snapshot = Export.objects.get(pk=response.json()["id"])
        assert snapshot.created_by_id == mapped_manager.id

    def test_async_import_rejects_mapped_actor_outside_project_scope_before_queueing(self):
        foreign_actor = UserFactory()
        foreign_org = OrganizationFactory()
        foreign_actor.active_organization = foreign_org
        foreign_actor.save(update_fields=["active_organization"])

        class ForeignProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{foreign_actor.id}",
                    source="test-mapped",
                    username=foreign_actor.email,
                    local_user_id=foreign_actor.id,
                )

        with (
            patch("access_control.identity.get_identity_provider", return_value=ForeignProvider()),
            patch("data_import.api.settings.VERSION_EDITION", "Enterprise"),
            patch("data_import.api.start_job_async_or_sync") as start_job,
        ):
            response = self.client.post(
                f"/api/projects/{self.project.id}/import",
                data=[{"data": {"text": "out-of-scope actor"}}],
                format="json",
            )

        assert response.status_code == 403
        start_job.assert_not_called()

    def test_async_import_keeps_session_scope_and_passes_mapped_actor_separately(self):
        mapped_actor = UserFactory(active_organization=self.organization)
        self.organization.add_user(mapped_actor)
        ProjectMember.objects.create(
            project=self.project,
            user=mapped_actor,
            role=ProjectMember.Role.MANAGER,
        )

        class MappedProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{mapped_actor.id}",
                    source="test-mapped",
                    username=mapped_actor.email,
                    local_user_id=mapped_actor.id,
                )

        with (
            patch("access_control.identity.get_identity_provider", return_value=MappedProvider()),
            patch("data_import.api.settings.VERSION_EDITION", "Enterprise"),
            patch("data_import.api.start_job_async_or_sync") as start_job,
        ):
            response = self.client.post(
                f"/api/projects/{self.project.id}/import",
                data=[{"data": {"text": "async mapped actor"}}],
                format="json",
            )

        assert response.status_code == 201, response.json()
        args, kwargs = start_job.call_args
        assert args[2] == self.actor.id
        assert kwargs["actor_id"] == mapped_actor.id
        assert kwargs["organization_id"] == self.project.organization_id

    def test_async_reimport_passes_mapped_actor_without_replacing_session_user(self):
        mapped_actor = UserFactory(active_organization=self.organization)
        self.organization.add_user(mapped_actor)
        ProjectMember.objects.create(
            project=self.project,
            user=mapped_actor,
            role=ProjectMember.Role.MANAGER,
        )

        class MappedProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{mapped_actor.id}",
                    source="test-mapped",
                    username=mapped_actor.email,
                    local_user_id=mapped_actor.id,
                )

        view = ReImportAPI()
        view.request = SimpleNamespace(user=self.actor)

        with (
            patch("access_control.identity.get_identity_provider", return_value=MappedProvider()),
            patch("data_import.api.start_job_async_or_sync") as start_job,
        ):
            response = view.async_reimport(
                self.project,
                [999999],
                True,
                self.project.organization_id,
            )

        assert response.status_code == 201
        args, kwargs = start_job.call_args
        assert args[2] == self.project.organization_id
        assert args[3] == self.actor.id
        assert kwargs["actor_id"] == mapped_actor.id


    def test_async_reimport_accepts_legacy_queued_user_object(self):
        project_reimport = ProjectReimport.objects.create(
            project=self.project,
            file_upload_ids=[],
            files_as_tasks_list=True,
        )

        with patch("data_import.functions.FileUpload.load_tasks_from_uploaded_files", return_value=([], {}, [])):
            async_reimport_background(
                project_reimport.id,
                self.project.organization_id,
                self.actor,
            )

        project_reimport.refresh_from_db()
        assert project_reimport.status == ProjectReimport.Status.COMPLETED


    def test_sync_import_rejects_out_of_scope_mapped_actor_before_parsing_even_without_commit(self):
        foreign_actor = UserFactory()
        foreign_org = OrganizationFactory()
        foreign_actor.active_organization = foreign_org
        foreign_actor.save(update_fields=["active_organization"])

        class ForeignProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{foreign_actor.id}",
                    source="test-mapped",
                    username=foreign_actor.email,
                    local_user_id=foreign_actor.id,
                )

        with (
            patch("access_control.identity.get_identity_provider", return_value=ForeignProvider()),
            patch("data_import.api.settings.VERSION_EDITION", "Community"),
            patch("data_import.api.load_tasks") as load_tasks,
        ):
            response = self.client.post(
                f"/api/projects/{self.project.id}/import?commit_to_project=false",
                data=[{"data": {"text": "must not parse"}}],
                format="json",
            )

        assert response.status_code == 403
        load_tasks.assert_not_called()


    def test_sync_reimport_rejects_out_of_scope_mapped_actor_before_mutation(self):
        foreign_actor = UserFactory()
        foreign_org = OrganizationFactory()
        foreign_actor.active_organization = foreign_org
        foreign_actor.save(update_fields=["active_organization"])

        class ForeignProvider:
            def resolve(self, request):
                return Principal(
                    principal_id=f"mapped:{foreign_actor.id}",
                    source="test-mapped",
                    username=foreign_actor.email,
                    local_user_id=foreign_actor.id,
                )

        with (
            patch("access_control.identity.get_identity_provider", return_value=ForeignProvider()),
            patch("data_import.api.settings.VERSION_EDITION", "Community"),
            patch("data_import.api.FileUpload.load_tasks_from_uploaded_files") as load_tasks,
        ):
            response = self.client.post(
                f"/api/projects/{self.project.id}/reimport",
                data={"file_upload_ids": [999999], "files_as_tasks_list": True},
                format="json",
            )

        assert response.status_code == 403
        load_tasks.assert_not_called()


    def test_malformed_assignment_id_returns_conflict_instead_of_server_error(self):
        task = TaskFactory(project=self.project)
        annotation = AnnotationFactory(
            task=task,
            project=self.project,
            completed_by=self.actor,
            updated_by=self.actor,
            result=[],
        )
        TaskAssignment.objects.create(
            task=task,
            project=self.project,
            assignee=self.actor,
            assigned_by=self.actor,
            annotation=annotation,
            status=TaskAssignment.Status.IN_PROGRESS,
        )

        response = self.client.patch(
            f"/api/annotations/{annotation.id}/",
            data={
                "result": [],
                "assignment_id": "stale",
                "assignment_version": 1,
            },
            format="json",
        )

        assert response.status_code == 409
