param(
    [Parameter(Mandatory = $true)][string]$DependencyVenv,
    [Parameter(Mandatory = $true)][string]$MinioClient,
    [string]$Subnet = '10.252.86.0/28',
    [switch]$TestS3Unknown,
    [switch]$TestMixedUpgrade
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPath = (Resolve-Path -LiteralPath $DependencyVenv).Path
$mcPath = (Resolve-Path -LiteralPath $MinioClient).Path
$baseline = '2a9bfbcbf0a844b999de97e601d16050a893f5fb'
$tag = 'issue86-mixed-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$net = "$tag-net"
$server = "$tag-s3"
$db = "$tag-db"
$redis = "$tag-redis"
$runner = "$tag-runner"
$created = @()
$networkCreated = $false

if (-not (Test-Path -LiteralPath (Join-Path $venvPath 'bin/python'))) {
    throw 'The supplied dependency path must be a Linux virtual environment.'
}
if (-not (Test-Path -LiteralPath $mcPath -PathType Leaf)) {
    throw 'The supplied MinIO client must be a Linux executable file.'
}
foreach ($image in @('listingkit/object-storage:retained-20250422', 'postgres:16-alpine',
                      'redis:7-alpine', 'python:3.11-slim-bookworm')) {
    docker image inspect $image --format '{{.Id}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Required local image is unavailable: $image" }
}
git -C $repoRoot cat-file -e "$baseline^{commit}"
if ($LASTEXITCODE -ne 0) { throw 'The pinned legacy baseline commit is unavailable locally.' }

try {
    docker network create --internal --subnet $Subnet $net | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated test network.' }
    $networkCreated = $true
    docker run -d --pull never --name $server --network $net -e MINIO_ROOT_USER=issue86fixture `
        -e MINIO_ROOT_PASSWORD=issue86fixturepassword listingkit/object-storage:retained-20250422 server /data | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the disposable object store.' }
    $created += $server
    docker run -d --pull never --name $db --network "container:$server" -e POSTGRES_PASSWORD=postgres `
        -e POSTGRES_DB=postgres postgres:16-alpine | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable PostgreSQL.' }
    $created += $db
    docker run -d --pull never --name $redis --network "container:$server" redis:7-alpine --save '' --appendonly no | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable Redis.' }
    $created += $redis
    docker run -d --pull never --name $runner --network "container:$server" `
        -v "${repoRoot}:/work:ro" -v "${venvPath}:/opt/venv:ro" -v "${mcPath}:/fixture/mc:ro" `
        -w /work/label_studio -e PYTHONPATH=/work:/work/label_studio `
        -e DJANGO_SETTINGS_MODULE=core.settings.label_studio -e DJANGO_DB=postgresql `
        -e POSTGRE_HOST=127.0.0.1 -e POSTGRE_NAME=postgres -e POSTGRE_USER=postgres -e POSTGRE_PASSWORD=postgres `
        -e LATEST_VERSION_CHECK=false -e COLLECT_ANALYTICS=false -e SECRET_KEY=synthetic-issue86-secret `
        -e PYTHONDONTWRITEBYTECODE=1 -e LOG_DIR=/tmp/issue86-test-logs `
        -e ISSUE86_DISPOSABLE_S3_TEST=1 -e ISSUE86_DISPOSABLE_REDIS_TEST=1 -e ISSUE86_LEGACY_APP_ROOT=/legacy `
        python:3.11-slim-bookworm sleep 1800 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the disposable test runner.' }
    $created += $runner
    docker exec $runner mkdir -p /legacy
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the legacy source directory.' }
    git -C $repoRoot archive --format=tar $baseline label_studio pyproject.toml | docker exec -i $runner tar -xf - -C /legacy
    if ($LASTEXITCODE -ne 0) { throw 'Could not extract the pinned legacy source.' }
    docker exec $runner test -f /legacy/label_studio/io_storages/s3/models.py
    if ($LASTEXITCODE -ne 0) { throw 'The extracted legacy source is incomplete.' }
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        docker exec $db pg_isready -U postgres 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Disposable PostgreSQL did not become ready.' }
    if ($TestS3Unknown -and $TestMixedUpgrade) { throw 'Select only one test group.' }
    if ($TestMixedUpgrade) {
        docker exec -e ISSUE86_OLD_LABEL_STUDIO_DIR=/legacy/label_studio -e ISSUE86_OLD_WORKER_DIR=/legacy/label_studio `
            -e ISSUE86_TEST_REDIS_URL=redis://127.0.0.1:6379/0 $runner /opt/venv/bin/python -m pytest -q -p no:cacheprovider `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_old_sync_worker_blocks_new_drain_without_new_ledger `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_real_old_process_queue_update_is_not_cleared_by_new_cancellation `
            io_storages/tests/test_webhook_outbox.py::TaskCreatedWebhookOutboxTests::test_label_created_real_old_worker_rejected_then_new_worker_delivered
        if ($LASTEXITCODE -ne 0) { throw 'The mixed-upgrade regression failed.' }
        docker exec -e ISSUE86_OLD_LABEL_STUDIO_DIR=/legacy/label_studio -e ISSUE86_OLD_WORKER_CRASH=1 `
            $runner /opt/venv/bin/python -m pytest -q -p no:cacheprovider `
            io_storages/tests/test_storage_admission.py::StorageAdmissionTests::test_old_sync_worker_blocks_new_drain_without_new_ledger
    } elseif ($TestS3Unknown) {
        docker exec $runner /opt/venv/bin/python -m pytest -q -p no:cacheprovider io_storages/tests/test_s3_export_unknown_isolated.py
    } else {
        docker exec $runner /opt/venv/bin/python -m pytest -q -p no:cacheprovider io_storages/tests/test_s3_legacy_worker_isolated.py
    }
    if ($LASTEXITCODE -ne 0) { throw 'The isolated S3 regression failed.' }
} finally {
    if ($created.Count -gt 0) {
        [array]::Reverse($created)
        docker rm -fv $created 2>$null | Out-Null
    }
    if ($networkCreated) { docker network rm $net 2>$null | Out-Null }
}
