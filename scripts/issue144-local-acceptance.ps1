param(
    [Parameter(Mandatory = $true)][string]$DependencyVenv,
    [Parameter(Mandatory = $true)][string]$MinioClient,
    [string]$Subnet = '10.252.86.0/28'
)

$ErrorActionPreference = 'Stop'
$venvPath = (Resolve-Path -LiteralPath $DependencyVenv).Path
$mcPath = (Resolve-Path -LiteralPath $MinioClient).Path
if (-not (Test-Path -LiteralPath (Join-Path $venvPath 'bin/python') -PathType Leaf)) {
    throw 'A Linux virtual environment is required.'
}
if (-not (Test-Path -LiteralPath $mcPath -PathType Leaf)) {
    throw 'A Linux MinIO client is required.'
}

# These existing fixtures create their own uniquely named disposable containers,
# do not publish host ports, and clean up only the resources they create. Run
# sequentially because the two network fixtures use the same isolated subnet.
# A passing local run is never evidence of real-cloud isolation or request drain.
$stages = @(
    @{ Name = 'legacy queue inventory'; Script = 'issue86-isolated-queue-inventory.ps1'; Args = @{ DependencyVenv = $venvPath } },
    @{ Name = 'multipart inventory'; Script = 'issue86-isolated-multipart.ps1'; Args = @{ DependencyVenv = $venvPath } },
    @{ Name = 'network fence and buffered request'; Script = 'issue86-isolated-network-fence.ps1'; Args = @{ DependencyVenv = $venvPath; MinioClient = $mcPath; Subnet = $Subnet } },
    @{ Name = 'legacy worker and credential fence'; Script = 'issue86-isolated-legacy-worker.ps1'; Args = @{ DependencyVenv = $venvPath; MinioClient = $mcPath; Subnet = $Subnet } },
    @{ Name = 'mixed upgrade and old-worker death'; Script = 'issue86-isolated-legacy-worker.ps1'; Args = @{ DependencyVenv = $venvPath; MinioClient = $mcPath; Subnet = $Subnet; TestMixedUpgrade = $true } },
    @{ Name = 'unknown S3 outcomes'; Script = 'issue86-isolated-legacy-worker.ps1'; Args = @{ DependencyVenv = $venvPath; MinioClient = $mcPath; Subnet = $Subnet; TestS3Unknown = $true } },
    @{ Name = 'database and broker isolation and migration'; Script = 'issue86-isolated-postgres-recovery.ps1'; Args = @{ DependencyVenv = $venvPath } },
    @{ Name = 'recovery and migration'; Script = 'issue86-isolated-postgres-recovery.ps1'; Args = @{ DependencyVenv = $venvPath; TestWebhookRecovery = $true } },
    @{ Name = 'previous durable workers'; Script = 'issue86-isolated-postgres-recovery.ps1'; Args = @{ DependencyVenv = $venvPath; TestWebhookMixed = $true } }
)

$passed = @()
foreach ($stage in $stages) {
    Write-Host "LOCAL START: $($stage.Name)"
    try {
        $stageArgs = $stage.Args
        # A script may use `exit` or return a failed native command without
        # throwing. Do not let a stale exit code or a nonzero result become a
        # successful acceptance stage.
        $global:LASTEXITCODE = 0
        & (Join-Path $PSScriptRoot $stage.Script) @stageArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Fixture exited with code $LASTEXITCODE."
        }
    } catch {
        Write-Host "LOCAL FAIL: $($stage.Name); later stages NOT_RUN."
        throw
    }
    $passed += $stage.Name
    Write-Host "LOCAL PASS: $($stage.Name)"
}
Write-Host "Local fixture stages passed: $($passed.Count)/$($stages.Count)."
Write-Host 'Real cloud identity/network isolation, request drain and cloud acceptance: NOT_RUN/UNKNOWN.'
