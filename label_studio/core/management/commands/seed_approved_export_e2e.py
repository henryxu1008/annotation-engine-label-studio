"""Synthetic #79 browser fixtures, restricted to disposable test environments."""

import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from organizations.models import Organization
from projects.models import Project, ProjectMember
from tasks.models import Annotation, Task, TaskAssignment
from tasks.submissions import create_submission, review_submission
from users.models import User


class Command(BaseCommand):
    help = "Create isolated approved-export browser fixtures (TEST_ENVIRONMENT=1 only)."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True)
        parser.add_argument("--policy-output", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        if os.environ.get("TEST_ENVIRONMENT") != "1":
            raise CommandError("Only allowed in an explicitly isolated TEST_ENVIRONMENT=1 database.")
        password = "Issue79Synthetic!12345"
        owner = User.objects.create_user(
            email="issue79-owner@example.test", password=password, username="issue79-owner"
        )
        organization = Organization.create_organization(created_by=owner, title="Issue79 synthetic organization")
        owner.active_organization = organization
        owner.save(update_fields=["active_organization"])
        users = {"owner": owner}
        from organizations.models import OrganizationMember

        for role in ("manager", "annotator", "reviewer"):
            user = User.objects.create_user(
                email=f"issue79-{role}@example.test",
                password=password,
                username=f"issue79-{role}",
                active_organization=organization,
            )
            OrganizationMember.objects.create(user=user, organization=organization)
            users[role] = user
        xml = '<View><Labels name="entity" toName="text"><Label value="Entity"/></Labels><Text name="text" value="$text"/></View>'
        project = Project.objects.create(
            title="Issue79 approved NER export",
            organization=organization,
            created_by=owner,
            label_config=xml,
            is_published=True,
        )
        members = {}
        for role in ("manager", "annotator", "reviewer"):
            members[role] = ProjectMember.objects.create(project=project, user=users[role], role=role)
        sources = []
        for i, text in enumerate(("Alpha sample", "Beta sample", "Gamma sample", "Pending sample")):
            task = Task.objects.create(project=project, data={"text": text}, meta={"synthetic": True})
            annotation = Annotation.objects.create(
                task=task,
                project=project,
                completed_by=users["annotator"],
                result=[
                    {
                        "id": f"entity-{i}",
                        "from_name": "entity",
                        "to_name": "text",
                        "type": "labels",
                        "value": {
                            "start": 0,
                            "end": len(text.split()[0]),
                            "text": text.split()[0],
                            "labels": ["Entity"],
                        },
                    }
                ],
            )
            assignment = TaskAssignment.objects.create(
                project=project, task=task, assignee=users["annotator"], assigned_by=owner, annotation=annotation
            )
            source = create_submission(assignment=assignment, annotation=annotation, actor=users["annotator"])
            if i < 3:
                review_submission(submission=source, reviewer=users["reviewer"], decision="approved")
            sources.append(
                {"submission_id": source.pk, "revision": source.revision, "result_hash": source.result_hash}
            )
        fixture = {
            "project_id": project.pk,
            "organization_id": organization.pk,
            "password": password,
            "users": {role: {"id": user.pk, "email": user.email} for role, user in users.items()},
            "manager_membership_id": members["manager"].pk,
            "approved": sources[:3],
            "pending": sources[3],
        }
        policy = {
            "source_namespace": "00000000-0000-4000-8000-000000000079",
            "projects": {
                f"{organization.pk}:{project.pk}": {"mode": "fork_approved", "version": "issue79-synthetic/v1"}
            },
        }
        Path(options["output"]).write_text(json.dumps(fixture, indent=2), encoding="utf-8")
        Path(options["policy_output"]).write_text(json.dumps(policy, indent=2), encoding="utf-8")
        self.stdout.write(
            self.style.SUCCESS(f"Seeded isolated #79 project {project.pk}; no external provider or Core use.")
        )
