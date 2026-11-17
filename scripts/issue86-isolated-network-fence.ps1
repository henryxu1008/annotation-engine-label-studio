param(
    [Parameter(Mandatory = $true)][string]$DependencyVenv,
    [Parameter(Mandatory = $true)][string]$MinioClient,
    [string]$Subnet = '10.252.86.0/28'
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venvPath = (Resolve-Path -LiteralPath $DependencyVenv).Path
$mcPath = (Resolve-Path -LiteralPath $MinioClient).Path
$baseline = '2a9bfbcbf0a844b999de97e601d16050a893f5fb'
$tag = 'issue86-net-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$net = "$tag-net"
$server = "$tag-s3"
$observer = "$tag-observer"
$legacy = "$tag-legacy"
$created = @()
$networkCreated = $false

if (-not (Test-Path -LiteralPath (Join-Path $venvPath 'bin/python'))) { throw 'A Linux virtual environment is required.' }
if (-not (Test-Path -LiteralPath $mcPath -PathType Leaf)) { throw 'A Linux MinIO client is required.' }
foreach ($image in @('listingkit/object-storage:retained-20250422', 'python:3.11-slim-bookworm')) {
    docker image inspect $image --format '{{.Id}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Required local image is unavailable: $image" }
}
git -C $repoRoot cat-file -e "$baseline^{commit}"
if ($LASTEXITCODE -ne 0) { throw 'The pinned legacy baseline commit is unavailable locally.' }

try {
    docker network create --internal --subnet $Subnet $net | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated test network.' }
    $networkCreated = $true
    docker run -d --pull never --name $server --network $net --network-alias objects `
        -e MINIO_ROOT_USER=issue86fixture -e MINIO_ROOT_PASSWORD=issue86fixturepassword `
        listingkit/object-storage:retained-20250422 server /data | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the disposable object store.' }
    $created += $server
    $serverAddress = docker inspect $server --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
    if ($LASTEXITCODE -ne 0 -or -not $serverAddress) { throw 'Could not determine the fixture address.' }
    docker run -d --pull never --name $observer --network "container:$server" `
        -v "${mcPath}:/fixture/mc:ro" -v "${repoRoot}:/work:ro" -v "${venvPath}:/opt/venv:ro" `
        -w /work/label_studio -e PYTHONPATH=/work:/work/label_studio `
        -e DJANGO_SETTINGS_MODULE=core.settings.label_studio -e DJANGO_DB=sqlite `
        -e LATEST_VERSION_CHECK=false -e COLLECT_ANALYTICS=false -e SECRET_KEY=synthetic-issue86-secret `
        -e PYTHONDONTWRITEBYTECODE=1 -e LOG_DIR=/tmp/issue86-test-logs -e ISSUE86_DISPOSABLE_S3_TEST=1 `
        -e ISSUE86_DISPOSABLE_NETWORK_TEST=1 `
        python:3.11-slim-bookworm sleep 600 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the observer.' }
    $created += $observer
    docker run -d --pull never --name $legacy --network $net -v "${repoRoot}:/work:ro" `
        -v "${venvPath}:/opt/venv:ro" -w /legacy/label_studio -e PYTHONPATH=/legacy:/legacy/label_studio `
        -e DJANGO_SETTINGS_MODULE=core.settings.label_studio -e DJANGO_DB=sqlite `
        -e AWS_MAX_ATTEMPTS=1 -e LATEST_VERSION_CHECK=false -e COLLECT_ANALYTICS=false `
        -e SECRET_KEY=synthetic-issue86-secret -e PYTHONDONTWRITEBYTECODE=1 `
        -e ISSUE86_DISPOSABLE_NETWORK_TEST=1 -e "ISSUE86_OBJECT_HOST=$serverAddress" `
        python:3.11-slim-bookworm sleep 600 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the legacy container.' }
    $created += $legacy
    git -C $repoRoot archive --format=tar $baseline label_studio pyproject.toml | docker exec -i $legacy tar -xf - -C /legacy
    if ($LASTEXITCODE -ne 0) { throw 'Could not extract the pinned legacy source.' }
    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        docker exec $observer /fixture/mc alias set fixture http://127.0.0.1:9000 issue86fixture issue86fixturepassword 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Disposable object storage did not become ready.' }
    docker exec $observer /fixture/mc mb fixture/issue86-network-fixture | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the synthetic bucket.' }
    docker exec $observer /fixture/mc version enable fixture/issue86-network-fixture | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not enable fixture bucket versioning.' }
    docker exec $observer /fixture/mc admin user add fixture issue86oldworker issue86oldworkerpassword | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the synthetic old identity.' }
    docker exec $observer /fixture/mc admin policy attach fixture readwrite --user issue86oldworker | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not grant the synthetic old identity.' }
    docker exec $observer /fixture/mc alias set oldidentity http://127.0.0.1:9000 issue86oldworker issue86oldworkerpassword | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not configure the independent old-identity observer.' }
    docker exec $observer /fixture/mc stat oldidentity/issue86-network-fixture | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'The old identity cannot access the fixture before isolation.' }
    docker exec -d $observer /opt/venv/bin/python /work/scripts/issue86_buffered_put_proxy.py
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the synthetic intermediary.' }
    $listening = $false
    for ($i = 0; $i -lt 30; $i++) {
        docker exec $observer test -f /tmp/issue86-proxy-listening 2>$null
        if ($LASTEXITCODE -eq 0) { $listening = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $listening) { throw 'The synthetic intermediary did not become ready.' }
    docker exec -d $legacy /opt/venv/bin/python /work/scripts/issue86_legacy_network_probe.py
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the legacy probe.' }
    $ready = $false
    for ($i = 0; $i -lt 120; $i++) {
        docker exec $legacy test -f /tmp/issue86-ready 2>$null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        docker exec $legacy test -f /tmp/issue86-result 2>$null
        if ($LASTEXITCODE -eq 0) { throw 'Legacy probe failed before isolation.' }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Legacy probe did not reach the provider before isolation.' }
    $captured = $false
    for ($i = 0; $i -lt 30; $i++) {
        docker exec $observer test -f /tmp/issue86-proxy-captured 2>$null
        if ($LASTEXITCODE -eq 0) { $captured = $true; break }
        docker exec $observer test -f /tmp/issue86-proxy-result 2>$null
        if ($LASTEXITCODE -eq 0) { throw 'The intermediary failed before capturing the PUT.' }
        Start-Sleep -Seconds 1
    }
    if (-not $captured) { throw 'The intermediary did not capture the legacy PUT.' }
    docker network disconnect --force $net $legacy
    if ($LASTEXITCODE -ne 0) { throw 'Could not disconnect the legacy container.' }
    $members = docker network inspect $net --format '{{range .Containers}}{{.Name}} {{end}}'
    if ($LASTEXITCODE -ne 0 -or $members -match [regex]::Escape($legacy)) { throw 'Legacy container remains attached.' }
    docker exec $observer /fixture/mc stat fixture/issue86-network-fixture/issue86/inflight 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { throw 'The synthetic PUT appeared before the intermediary was released.' }
    docker exec $observer touch /tmp/issue86-proxy-release
    if ($LASTEXITCODE -ne 0) { throw 'Could not release the synthetic intermediary.' }
    $proxyOutcome = ''
    for ($i = 0; $i -lt 30; $i++) {
        docker exec $observer test -f /tmp/issue86-proxy-result 2>$null
        if ($LASTEXITCODE -eq 0) { $proxyOutcome = docker exec $observer cat /tmp/issue86-proxy-result; break }
        Start-Sleep -Seconds 1
    }
    if ($proxyOutcome -ne 'PROVIDER_CONFIRMED') { throw "The provider did not confirm the buffered PUT: $proxyOutcome" }
    $body = docker exec $observer /fixture/mc cat fixture/issue86-network-fixture/issue86/inflight
    if ($LASTEXITCODE -ne 0 -or $body -ne 'synthetic-network-inflight') { throw 'The committed fixture object was not observed.' }
    $versionProbe = "import boto3; c=boto3.client('s3',endpoint_url='http://127.0.0.1:9000',region_name='us-east-1',aws_access_key_id='issue86fixture',aws_secret_access_key='issue86fixturepassword'); r=c.head_object(Bucket='issue86-network-fixture',Key='issue86/inflight'); print('VERSIONED' if r.get('VersionId') else 'UNVERSIONED')"
    $versionStatus = docker exec $observer /opt/venv/bin/python -c $versionProbe
    if ($LASTEXITCODE -ne 0 -or $versionStatus -ne 'VERSIONED') { throw 'The fixture PUT has no immutable version.' }
    Write-Output 'PASS: a legacy PUT buffered before network disconnect was committed as a version afterward.'
    docker exec $legacy touch /tmp/issue86-resume
    if ($LASTEXITCODE -ne 0) { throw 'Could not resume the legacy probe.' }
    $outcome = ''
    for ($i = 0; $i -lt 30; $i++) {
        docker exec $legacy test -f /tmp/issue86-result 2>$null
        if ($LASTEXITCODE -eq 0) { $outcome = docker exec $legacy cat /tmp/issue86-result; break }
        Start-Sleep -Seconds 1
    }
    if ($outcome -notmatch '^NEW_CONNECTION_BLOCKED;CLIENT_(UNKNOWN|BLOCKED_[A-Za-z]+)$') {
        throw "Legacy probe reported an unsafe or invalid outcome: $outcome"
    }
    docker exec $observer /fixture/mc stat fixture/issue86-network-fixture | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'The independent observer lost access.' }
    docker exec $observer /fixture/mc stat oldidentity/issue86-network-fixture | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'The old identity was not active in the independent observer.' }
    Write-Output 'PASS: new legacy-container connections were blocked while the observer remained connected.'
    Write-Output "Existing SDK client observation: $outcome"
    Write-Output 'UNKNOWN is not a drain proof. This local result does not establish AWS network enforcement.'
    docker exec $observer /opt/venv/bin/python -m pytest -q -p no:cacheprovider io_storages/tests/test_s3_proxy_drain_isolated.py
    if ($LASTEXITCODE -ne 0) { throw 'The buffered request regression failed.' }
} finally {
    if ($created.Count -gt 0) {
        [array]::Reverse($created)
        docker rm -fv $created 2>$null | Out-Null
    }
    if ($networkCreated) { docker network rm $net 2>$null | Out-Null }
}
