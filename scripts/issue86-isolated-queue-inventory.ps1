param([Parameter(Mandatory = $true)][string]$DependencyVenv)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPath = (Resolve-Path -LiteralPath $DependencyVenv).Path
if (-not (Test-Path -LiteralPath (Join-Path $venvPath 'bin/python'))) { throw 'A Linux virtual environment is required.' }
$tag = 'issue86-queue-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$redis = "$tag-redis"
$runner = "$tag-runner"
$created = @()
try {
    docker run -d --pull never --name $redis --network none redis:7-alpine --save '' --appendonly no | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable Redis.' }
    $created += $redis
    docker run -d --pull never --name $runner --network "container:$redis" `
        -v "${repoRoot}:/work:ro" -v "${venvPath}:/opt/venv:ro" -w /work/label_studio `
        -e PYTHONPATH=/work:/work/label_studio -e DJANGO_SETTINGS_MODULE=core.settings.label_studio `
        -e DJANGO_DB=sqlite -e LATEST_VERSION_CHECK=false -e COLLECT_ANALYTICS=false `
        -e SECRET_KEY=synthetic-issue86-secret -e PYTHONDONTWRITEBYTECODE=1 `
        -e LOG_DIR=/tmp/issue86-test-logs -e ISSUE86_DISPOSABLE_REDIS_TEST=1 `
        python:3.11-slim-bookworm sleep 600 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable test runner.' }
    $created += $runner
    docker exec $runner /opt/venv/bin/python -m pytest -q -p no:cacheprovider `
        io_storages/tests/test_upgrade_queue_inventory.py io_storages/tests/test_upgrade_queue_inventory_isolated.py
    if ($LASTEXITCODE -ne 0) { throw 'The isolated queue inventory regressions failed.' }
} finally {
    if ($created.Count -gt 0) {
        [array]::Reverse($created)
        docker rm -fv $created 2>$null | Out-Null
    }
}
