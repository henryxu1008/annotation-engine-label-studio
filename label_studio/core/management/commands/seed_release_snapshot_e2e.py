"""Fresh synthetic #80 cohort in an explicitly disposable database."""

import base64
import hashlib
import json
import os
from pathlib import Path

from data_import.models import FileUpload
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from organizations.models import Organization
from projects.models import Project, ProjectMember
from tasks.models import Annotation, Task, TaskAssignment
from tasks.submissions import create_submission, review_submission
from users.models import User


class Command(BaseCommand):
    help = "Seed a new isolated #80 cohort; never reuses a retained runtime."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True)
        parser.add_argument("--policy-output", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        if os.environ.get("TEST_ENVIRONMENT") != "1":
            raise CommandError("Requires an isolated TEST_ENVIRONMENT=1 database")
        # Reuse the existing fixture builder in this NEW database, without touching #79 resources.
        call_command("seed_approved_export_e2e", output=options["output"], policy_output=options["policy_output"])
        fixture = json.loads(Path(options["output"]).read_text())
        policy = json.loads(Path(options["policy_output"]).read_text())
        policy["source_namespace"] = "00000000-0000-4000-8000-000000000080"
        organization = Organization.objects.get(pk=fixture["organization_id"])
        users = {key: User.objects.get(pk=row["id"]) for key, row in fixture["users"].items()}
        media = Project.objects.create(
            organization=organization,
            created_by=users["owner"],
            title="Issue80 managed media snapshots",
            is_published=True,
            label_config='<View><Image name="image" value="$image"/><Choices name="class" toName="image"><Choice value="A"/></Choices></View>',
        )
        for role in ("manager", "annotator", "reviewer"):
            ProjectMember.objects.create(project=media, user=users[role], role=role)
        blob = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jm3sAAAAASUVORK5CYII="
        )
        upload = FileUpload.objects.create(
            project=media, user=users["manager"], file=ContentFile(blob, name="issue80-synthetic.png")
        )
        sources = []
        for reference in (upload.url, "/data/upload/999999/missing.png"):
            task = Task.objects.create(project=media, data={"image": reference}, meta={"synthetic": True})
            annotation = Annotation.objects.create(
                project=media, task=task, completed_by=users["annotator"], result=[]
            )
            assignment = TaskAssignment.objects.create(
                project=media,
                task=task,
                assignee=users["annotator"],
                assigned_by=users["manager"],
                annotation=annotation,
            )
            source = create_submission(assignment=assignment, annotation=annotation, actor=users["annotator"])
            review_submission(submission=source, reviewer=users["reviewer"], decision="approved")
            sources.append(source.pk)
        policy["projects"][f"{organization.pk}:{media.pk}"] = {
            "mode": "fork_approved",
            "version": "issue80-synthetic/v1",
        }
        fixture["media"] = {
            "project_id": media.pk,
            "valid_submission_id": sources[0],
            "missing_submission_id": sources[1],
            "sha256": hashlib.sha256(blob).hexdigest(),
        }
        Path(options["output"]).write_text(json.dumps(fixture, indent=2), encoding="utf-8")
        Path(options["policy_output"]).write_text(json.dumps(policy, indent=2), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS("#80 synthetic text/media cohort ready; no Host or Core calls"))
