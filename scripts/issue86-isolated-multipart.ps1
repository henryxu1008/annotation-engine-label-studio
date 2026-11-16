param([Parameter(Mandatory = $true)][string]$DependencyVenv)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPath = (Resolve-Path -LiteralPath $DependencyVenv).Path
if (-not (Test-Path -LiteralPath (Join-Path $venvPath 'bin/python'))) { throw 'A Linux virtual environment is required.' }
$tag = 'issue86-multipart-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$server = "$tag-s3"
$runner = "$tag-runner"
$created = @()
try {
    docker run -d --pull never --name $server --network none -e MINIO_ROOT_USER=issue86fixture `
        -e MINIO_ROOT_PASSWORD=issue86fixturepassword listingkit/object-storage:retained-20250422 server /data | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable object storage.' }
    $created += $server
    docker run -d --pull never --name $runner --network "container:$server" `
        -v "${repoRoot}:/work:ro" -v "${venvPath}:/opt/venv:ro" -w /work/label_studio `
        -e PYTHONPATH=/work:/work/label_studio -e DJANGO_SETTINGS_MODULE=core.settings.label_studio `
        -e DJANGO_DB=sqlite -e LATEST_VERSION_CHECK=false -e COLLECT_ANALYTICS=false `
        -e SECRET_KEY=synthetic-issue86-secret -e PYTHONDONTWRITEBYTECODE=1 `
        -e LOG_DIR=/tmp/issue86-test-logs -e ISSUE86_DISPOSABLE_S3_TEST=1 `
        python:3.11-slim-bookworm sleep 600 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable test runner.' }
    $created += $runner
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        docker exec $runner /opt/venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/minio/health/ready', timeout=1)" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Disposable object storage did not become ready.' }
    docker exec $runner /opt/venv/bin/python -m pytest -q -p no:cacheprovider `
        io_storages/tests/test_s3_multipart_inventory.py io_storages/tests/test_s3_multipart_isolated.py
    if ($LASTEXITCODE -ne 0) { throw 'The isolated multipart inventory regressions failed.' }
} finally {
    if ($created.Count -gt 0) {
        [array]::Reverse($created)
        docker rm -fv $created 2>$null | Out-Null
    }
}
