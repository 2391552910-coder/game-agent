[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$skillRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$entryScript = Join-Path $PSScriptRoot 'Invoke-MyAgent2SgaiHttpE2E.ps1'
$simulationApp = Join-Path $PSScriptRoot 'simulation_myagent_app.py'
$simulationDriver = Join-Path $PSScriptRoot 'simulation_driver.py'
$skillDocument = Join-Path $skillRoot 'SKILL.md'
$skillGitIgnore = Join-Path $skillRoot '.gitignore'

$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $entryScript,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {
    throw "Entry script has PowerShell parse errors: $($parseErrors.Message -join '; ')"
}

$parameterNames = @($ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath })
if ($parameterNames -notcontains 'ContractVersion') {
    throw 'Entry script must expose a ContractVersion parameter.'
}
if ($parameterNames -notcontains 'GatewayMode') {
    throw 'Entry script must expose a GatewayMode parameter.'
}
if ($parameterNames -notcontains 'SkipDependencyManagement') {
    throw 'Entry script must expose SkipDependencyManagement for externally managed infrastructure.'
}
if ($parameterNames -notcontains 'KeepDependencies') {
    throw 'Entry script must expose KeepDependencies for local debugging.'
}

$entryText = Get-Content -Raw -LiteralPath $entryScript
if ($entryText -notmatch "ValidateSet\('v1',\s*'v2'\)") {
    throw 'ContractVersion must only accept v1 or v2.'
}
if ($entryText -notmatch "ValidateSet\('Simulation',\s*'Real'\)") {
    throw 'GatewayMode must only accept Simulation or Real.'
}
if ($entryText -match '(?im)^\s*\$pid\s*=') {
    throw 'Do not assign to $pid because PowerShell treats it as the read-only $PID variable.'
}
if ($entryText -notmatch '(?s)--timeout-seconds\s+\$DecisionTimeoutSeconds') {
    throw 'Simulation mode must pass DecisionTimeoutSeconds to the driver.'
}
foreach ($requiredPattern in @(
    '--contract-version\s+\$ContractVersion',
    'seed_gateway_v2_test_tenant\.py',
    'assert_gateway_v2_state\.py',
    'gateway_runtime_config_keys\.json',
    'LLM_GATEWAY_APP_GATEWAYS',
    'LLM_GATEWAY_EVENT_STREAM_KEY',
    'LLM_GATEWAY_EVENT_CONSUMER_GROUP',
    'SGAI_SIM_EVENT_APP_ID',
    'SGAI_SIM_DECISION_APP_ID',
    'SGAI_SIM_CONTROL_APP_ID'
)) {
    if ($entryText -notmatch $requiredPattern) {
        throw "Entry script is missing required v2 behavior: $requiredPattern"
    }
}
if ($entryText -notmatch 'llm-gateway:e2e:\$runId' -or $entryText -notmatch 'simulation-\$runId') {
    throw 'Each Simulation run must use an isolated v1 Redis stream and consumer group.'
}

$buildIndex = $entryText.IndexOf('if (-not $SkipBuild)', [System.StringComparison]::Ordinal)
$appAssertIndex = $entryText.IndexOf('Assert-PathExists -Path $appDll', [System.StringComparison]::Ordinal)
if ($buildIndex -lt 0 -or $appAssertIndex -lt 0 -or $buildIndex -gt $appAssertIndex) {
    throw 'Real mode must build SGAI before asserting that Bin/App.dll exists.'
}

foreach ($path in @($simulationApp, $simulationDriver)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Simulation resource is missing: $path"
    }
}

$myAgentRoot = (Resolve-Path -LiteralPath (Join-Path $skillRoot '..\..\..')).Path
$python = Join-Path $myAgentRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "myAgent2 Python is missing: $python"
}
& $python -m py_compile $simulationApp $simulationDriver
if ($LASTEXITCODE -ne 0) {
    throw "Simulation Python syntax check failed, exitCode=$LASTEXITCODE"
}

$skillText = Get-Content -Raw -LiteralPath $skillDocument
foreach ($requiredText in @(
    '-ContractVersion v1 -GatewayMode Simulation',
    '-ContractVersion v2 -GatewayMode Simulation',
    '-ContractVersion v2 -GatewayMode Real',
    'does not prove real SGAI'
)) {
    if (-not $skillText.Contains($requiredText)) {
        throw "SKILL.md must document: $requiredText"
    }
}

if (-not (Test-Path -LiteralPath $skillGitIgnore)) {
    throw "Skill-local .gitignore is missing: $skillGitIgnore"
}
$ignoreText = Get-Content -Raw -LiteralPath $skillGitIgnore
foreach ($requiredPattern in @('.run/', '__pycache__/')) {
    if (-not $ignoreText.Contains($requiredPattern)) {
        throw "Skill-local .gitignore must contain: $requiredPattern"
    }
}

[pscustomobject]@{
    success = $true
    entryScript = $entryScript
    simulationResources = @($simulationApp, $simulationDriver)
    documentedContractVersions = @('v1', 'v2')
    documentedGatewayModes = @('Simulation', 'Real')
} | ConvertTo-Json -Depth 4
