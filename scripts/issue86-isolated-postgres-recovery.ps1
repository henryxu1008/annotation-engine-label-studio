param([Parameter(Mandatory = $true)][string]$DependencyVenv, [switch]$TestS3Fence, [switch]$TestEngineering, [switch]$TestExportFence, [switch]$TestImportFence, [switch]$TestNextTaskFence)

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
    if (@($TestS3Fence, $TestEngineering, $TestExportFence, $TestImportFence, $TestNextTaskFence).Where({ $_ }).Count -gt 1) { throw 'Select only one test group.' }
    if ($TestEngineering) {
        docker exec $runner /opt/venv/bin/python -m pytest -q -x -p no:cacheprovider `
            projects/tests/test_lifecycle_task_fence.py projects/tests/test_lifecycle_background_import.py `
            projects/tests/test_lifecycle_migration.py io_storages/tests/test_project_operation_fence.py `
            io_storages/tests/test_storage_admission.py io_storages/tests/test_webhook_outbox.py `
            io_storages/tests/test_s3_delete_key_fence.py io_storages/tests/test_rq_sync_attempt_isolated.py `
            io_storages/tests/test_upgrade_isolation_postgres.py tasks/tests/test_project_operation_policy.py `
            data_export/tests/test_lifecycle_worker_fence.py
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
