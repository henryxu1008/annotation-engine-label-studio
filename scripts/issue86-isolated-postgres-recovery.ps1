param([Parameter(Mandatory = $true)][string]$DependencyVenv, [switch]$TestS3Fence, [switch]$TestVersionDelete, [switch]$TestEngineering, [switch]$TestExportFence, [switch]$TestImportFence, [switch]$TestNextTaskFence, [switch]$TestWebhookRecovery, [switch]$TestWebhookMixed, [switch]$TestRqRecovery, [switch]$TestPackageDispatch)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPath = (Resolve-Path -LiteralPath $DependencyVenv).Path
if (-not (Test-Path -LiteralPath (Join-Path $venvPath 'bin/python'))) { throw 'A Linux virtual environment is required.' }
$tag = 'issue86-recovery-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$db = "$tag-db"
$redis = "$tag-redis"
$runner = "$tag-runner"
$created = @()
try {
    docker run -d --pull never --name $db --network none -e POSTGRES_PASSWORD=postgres `
        -e POSTGRES_DB=postgres postgres:16-alpine -c max_prepared_transactions=20 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable PostgreSQL.' }
    $created += $db
    docker run -d --pull never --name $redis --network "container:$db" redis:7-alpine --save '' --appendonly no | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable Redis.' }
    $created += $redis
    docker run -d --pull never --name $runner --network "container:$db" `
        -v "${repoRoot}:/work:ro" -v "${venvPath}:/opt/venv:ro" -w /work/label_studio `
        -e PYTHONPATH=/work:/work/label_studio -e DJANGO_SETTINGS_MODULE=core.settings.label_studio `
        -e DJANGO_DB=postgresql -e POSTGRE_HOST=127.0.0.1 -e POSTGRE_NAME=postgres `
        -e POSTGRE_USER=postgres -e POSTGRE_PASSWORD=postgres -e LATEST_VERSION_CHECK=false `
        -e COLLECT_ANALYTICS=false -e SECRET_KEY=synthetic-issue86-secret -e PYTHONDONTWRITEBYTECODE=1 `
        -e LOG_DIR=/tmp/issue86-test-logs -e ISSUE86_POSTGRES_ISOLATION_TEST=1 `
        -e ISSUE86_REDIS_ISOLATION_TEST=1 python:3.11-slim-bookworm sleep 3600 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable test runner.' }
    $created += $runner
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        docker exec $db pg_isready -U postgres 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Disposable PostgreSQL did not become ready.' }
    if (@($TestS3Fence, $TestVersionDelete, $TestEngineering, $TestExportFence, $TestImportFence, $TestNextTaskFence, $TestWebhookRecovery, $TestWebhookMixed, $TestRqRecovery, $TestPackageDispatch).Where({ $_ }).Count -gt 1) { throw 'Select only one test group.' }
    if ($TestPackageDispatch) {
        docker exec $runner /opt/venv/bin/python -m pytest -q -x -p no:cacheprovider task_packages/tests/test_package_dispatch.py task_packages/tests/test_packages.py
    } elseif ($TestEngineering) {
        docker exec $runner /opt/venv/bin/python -m pytest -q -x -p no:cacheprovider `
            projects/tests/test_lifecycle_task_fence.py projects/tests/test_lifecycle_background_import.py `
            projects/tests/test_lifecycle_migration.py io_storages/tests/test_project_operation_fence.py `
            io_storages/tests/test_storage_admission.py io_storages/tests/test_webhook_outbox.py `
            io_storages/tests/test_s3_delete_key_fence.py io_storages/tests/test_rq_sync_attempt_isolated.py `
            io_storages/tests/test_upgrade_isolation_postgres.py tasks/tests/test_project_operation_policy.py `
            data_export/tests/test_lifecycle_worker_fence.py
    } elseif ($TestVersionDelete) {
        docker exec $runner /opt/venv/bin/python -m pytest -q -x -p no:cacheprovider `
            io_storages/tests/test_s3_version_delete.py
    } elseif ($TestRqRecovery) {
        docker exec $runner /opt/venv/bin/python -m pytest -q -x -p no:cacheprovider `
            io_storages/tests/test_rq_sync_attempt_isolated.py `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_failure_callback_cannot_overwrite_a_different_attempt_or_legacy_job `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_reserved_redispatch_fences_older_queued_worker_and_job_receipt
    } elseif ($TestWebhookMixed) {
        $oldWorker = '8c273179c778b1e99ee74d8c17ddb7516ab96b92'
        git -C $repoRoot cat-file -e "$oldWorker^{commit}"
        if ($LASTEXITCODE -ne 0) { throw 'The pinned previous worker commit is unavailable.' }
        docker exec $runner mkdir -p /legacy-worker
        git -C $repoRoot archive --format=tar $oldWorker label_studio pyproject.toml | docker exec -i $runner tar -xf - -C /legacy-worker
        if ($LASTEXITCODE -ne 0) { throw 'Could not extract the previous worker.' }
        docker exec -e ISSUE86_OLD_DURABLE_WORKER_DIR=/legacy-worker/label_studio `
            -e ISSUE86_TEST_REDIS_URL=redis://127.0.0.1:6379/0 $runner /opt/venv/bin/python -m pytest -q -x -p no:cacheprovider `
            io_storages/tests/test_webhook_outbox.py::TaskCreatedWebhookOutboxTests::test_recovered_delivery_can_be_claimed_by_real_old_webhook_worker `
            io_storages/tests/test_rq_sync_attempt_isolated.py::IsolatedRQSyncAttemptTests::test_previous_worker_attempt_cannot_claim_recovered_storage
    } elseif ($TestWebhookRecovery) {
        docker exec $runner /opt/venv/bin/python -m pytest -q -x -p no:cacheprovider `
            io_storages/tests/test_webhook_outbox.py projects/tests/test_lifecycle_migration.py `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_local_export_reloads_source_after_an_older_worker_was_delayed `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_local_export_does_not_write_after_annotation_is_deleted `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_non_s3_providers_serialize_current_annotation_after_project_lock `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_non_s3_providers_do_not_open_clients_for_deleted_annotations `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_storage_recovery_fences_old_claim_after_new_worker_reclaims `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_annotation_export_recovery_fences_old_claim_and_preserves_delivery `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_storage_recovery_refuses_legacy_started_wrong_owner_and_cross_project `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_public_storage_recovery_enforces_scope_actor_and_stale_receipt `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_killed_storage_worker_pre_effect_is_requeued_without_repeating_effect `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_storage_recovery_rejects_an_effect_already_started `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_stale_health_check_cannot_fail_a_recovered_storage_attempt `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_storage_recovery_clears_failed_old_receipt_after_health_check_wins `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_stalled_redis_observation_cannot_block_or_fail_recovered_reused_job_id `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_timer_new_attempt_invalidates_inflight_health_observation `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_timer_reservation_before_health_check_does_not_reuse_old_receipt
    } elseif ($TestNextTaskFence) {
        docker exec $runner /opt/venv/bin/python -m pytest -q -x -p no:cacheprovider `
            projects/tests/test_lifecycle_task_fence.py -k next_task
    } elseif ($TestImportFence) {
        docker exec $runner /opt/venv/bin/python -m pytest -q -x -p no:cacheprovider `
            projects/tests/test_lifecycle_background_import.py
    } elseif ($TestExportFence) {
        docker exec $runner /opt/venv/bin/python -m pytest -q -x -p no:cacheprovider `
            'data_export/tests/test_approved_exports.py::TestApprovedExports::test_lifecycle_admission_blocks_release_creation_and_retry' `
            'data_export/tests/test_release_snapshots.py::TestReleaseSnapshots::test_lifecycle_admission_blocks_snapshot_creation_and_retry'
    } elseif ($TestS3Fence) {
        docker exec $runner /opt/venv/bin/python -m pytest -q -p no:cacheprovider `
            io_storages/tests/test_s3_delete_key_fence.py `
            io_storages/tests/test_s3_deletion_plan.py `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_standalone_process_inventories_legacy_storage_without_loading_urls `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_s3_delete_records_exact_key_without_calling_provider `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_s3_put_version_is_bound_to_deletion_intent `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_blocked_s3_outbox_is_requeued_and_succeeds_after_blocker_resolves
    } else {
        docker exec $runner /opt/venv/bin/python -m pytest -q -p no:cacheprovider `
            projects/tests/test_lifecycle_migration.py io_storages/tests/test_upgrade_isolation_postgres.py
    }
    if ($LASTEXITCODE -ne 0) { throw 'The isolated PostgreSQL recovery regressions failed.' }
} finally {
    if ($created.Count -gt 0) {
        [array]::Reverse($created)
        docker rm -fv $created 2>$null | Out-Null
    }
}
