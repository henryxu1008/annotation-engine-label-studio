param(
    [Parameter(Mandatory = $true)][string]$DependencyVenv,
    [Parameter(Mandatory = $true)][string]$FrontendBuild,
    [Parameter(Mandatory = $true)][string]$FrontendNodeModules,
    [string]$Subnet = '10.252.93.0/28'
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPath = (Resolve-Path -LiteralPath $DependencyVenv).Path
$buildPath = (Resolve-Path -LiteralPath $FrontendBuild).Path
$modulesPath = (Resolve-Path -LiteralPath $FrontendNodeModules).Path
foreach ($required in @((Join-Path $venvPath 'bin/python'), (Join-Path $buildPath 'main.js'),
                        (Join-Path $modulesPath 'cypress/bin/cypress'))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Missing fixture dependency: $required" }
}
$tag = 'issue86-ui-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$net = "$tag-net"
$db = "$tag-db"
$redis = "$tag-redis"
$runner = "$tag-runner"
$relay = "$tag-relay"
$artifactRoot = Join-Path $env:TEMP $tag
New-Item -ItemType Directory -Path $artifactRoot | Out-Null
$created = @()
$networkCreated = $false
try {
    docker network create --internal --subnet $Subnet $net | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated fixture network.' }
    $networkCreated = $true
    docker run -d --pull never --name $db --network $net `
        -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=issue86_ui_test postgres:16-alpine | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable PostgreSQL.' }
    $created += $db
    $dbAddress = docker inspect $db --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
    if ($LASTEXITCODE -ne 0 -or $dbAddress -notmatch '^\d+\.\d+\.\d+\.\d+$') { throw 'Could not identify fixture address.' }
    $relayConfig = @"
global
  log stdout format raw local0
defaults
  mode tcp
  timeout connect 5s
  timeout client 60s
  timeout server 60s
frontend browser
  bind *:8080
  default_backend application
backend application
  server fixture ${dbAddress}:8080
"@
    $relayConfigPath = Join-Path $artifactRoot 'haproxy.cfg'
    Set-Content -LiteralPath $relayConfigPath -Value $relayConfig -Encoding ascii
    docker run -d --pull never --name $relay --network bridge -p 127.0.0.1::8080 `
        -v "${relayConfigPath}:/usr/local/etc/haproxy/haproxy.cfg:ro" haproxy:3.2-alpine | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the loopback browser relay.' }
    $created += $relay
    docker network connect $net $relay
    if ($LASTEXITCODE -ne 0) { throw 'Could not connect the relay to the isolated fixture.' }
    $portLine = docker port $relay 8080/tcp
    if ($LASTEXITCODE -ne 0 -or $portLine -notmatch '^127\.0\.0\.1:(\d+)$') { throw 'Expected a loopback-only browser port.' }
    $baseUrl = 'http://127.0.0.1:' + $Matches[1]
    docker run -d --pull never --name $redis --network "container:$db" redis:7-alpine --save '' --appendonly no | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable Redis.' }
    $created += $redis
    docker run -d --pull never --name $runner --network "container:$db" `
        -v "${repoRoot}:/work:ro" -v "${venvPath}:/opt/venv:ro" -v "${buildPath}:/work/web/dist/apps/labelstudio:ro" `
        -w /work/label_studio -e PYTHONPATH=/work:/work/label_studio -e DJANGO_SETTINGS_MODULE=core.settings.label_studio `
        -e DJANGO_DB=postgresql -e POSTGRE_HOST=127.0.0.1 -e POSTGRE_NAME=issue86_ui_test `
        -e POSTGRE_USER=postgres -e POSTGRE_PASSWORD=postgres -e LATEST_VERSION_CHECK=false -e COLLECT_ANALYTICS=false `
        -e SECRET_KEY=synthetic-issue86-secret -e PYTHONDONTWRITEBYTECODE=1 -e LOG_DIR=/tmp/issue86-test-logs `
        -e SENTRY_DSN= -e SENTRY_RATE=0 `
        -e LABEL_STUDIO_BASE_DATA_DIR=/tmp/issue86-data -e TEST_ENVIRONMENT=1 -e ISSUE86_DISPOSABLE_UI_FIXTURE=1 `
        -e DEBUG=true -e "HOST=$baseUrl" -e "CSRF_TRUSTED_ORIGINS=$baseUrl" `
        -e ANNOTATION_STORAGE_ADMISSION_ENABLED=true -e ANNOTATION_LIFECYCLE_TRANSITIONS_ENABLED=true `
        -e ANNOTATION_LIFECYCLE_PUBLIC_API_ENABLED=true `
        -e ANNOTATION_PROJECT_OPERATION_PROVIDER=access_control.project_operations.LifecycleProjectOperationProvider `
        python:3.11-slim-bookworm sleep 1800 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable app runner.' }
    $created += $runner
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        docker exec $db pg_isready -U postgres 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Disposable PostgreSQL did not become ready.' }
    docker exec $runner /opt/venv/bin/python manage.py migrate --noinput *> (Join-Path $artifactRoot 'migrate.log')
    if ($LASTEXITCODE -ne 0) { throw 'Fixture migration failed; see migrate.log.' }
    $seedOutput = docker exec $runner /opt/venv/bin/python /work/scripts/issue86_ui_fixture.py
    if ($LASTEXITCODE -ne 0) { throw 'Fixture seeding failed.' }
    $seedLine = @($seedOutput | Where-Object { $_ -like 'ISSUE86_FIXTURE=*' })
    if ($seedLine.Count -ne 1) { throw 'Fixture identity was not returned exactly once.' }
    $fixture = $seedLine[0].Substring('ISSUE86_FIXTURE='.Length) | ConvertFrom-Json
    if ($fixture.project_id -lt 1 -or $fixture.manager -ne 'issue86-manager@example.test') { throw 'Invalid fixture scope.' }
    docker exec -d $runner /opt/venv/bin/python manage.py runserver 0.0.0.0:8080 --noreload
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the local app.' }
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-WebRequest -Uri "$baseUrl/user/login/" -UseBasicParsing -TimeoutSec 2 | Out-Null; $ready = $true; break } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw 'The isolated app did not become ready.' }
    $env:NODE_PATH = $modulesPath
    $env:ISSUE86_E2E_URL = $baseUrl
    $env:CYPRESS_issue86_project_id = [string]$fixture.project_id
    $artifactConfig = 'screenshotsFolder=' + ($artifactRoot.Replace('\', '/') + '/screenshots') +
        ',videosFolder=' + ($artifactRoot.Replace('\', '/') + '/videos') +
        ',downloadsFolder=' + ($artifactRoot.Replace('\', '/') + '/downloads')
    Push-Location (Join-Path $repoRoot 'web/apps/labelstudio-e2e')
    try {
        & node (Join-Path $modulesPath 'cypress/bin/cypress') run --project . --config-file cypress.lifecycle.config.ts `
            --browser electron --headless --config $artifactConfig
        if ($LASTEXITCODE -ne 0) { throw "Lifecycle browser regression failed; artifacts: $artifactRoot" }
    } finally { Pop-Location }
} finally {
    if ($created.Count -gt 0) {
        [array]::Reverse($created)
        docker rm -fv $created 2>$null | Out-Null
    }
    if ($networkCreated) { docker network rm $net 2>$null | Out-Null }
}
