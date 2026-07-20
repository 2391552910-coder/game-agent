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
if ($parameterNames -notcontains 'Mode') {
    throw 'Entry script must expose a Mode parameter.'
}
if ($parameterNames -notcontains 'SkipDependencyManagement') {
    throw 'Entry script must expose SkipDependencyManagement for externally managed infrastructure.'
}
if ($parameterNames -notcontains 'KeepDependencies') {
    throw 'Entry script must expose KeepDependencies for local debugging.'
}

$entryText = Get-Content -Raw -LiteralPath $entryScript
if ($entryText -notmatch "ValidateSet\('Simulation',\s*'Real'\)") {
    throw 'Mode must only accept Simulation or Real.'
}
if ($entryText -match '(?im)^\s*\$pid\s*=') {
    throw 'Do not assign to $pid because PowerShell treats it as the read-only $PID variable.'
}
if ($entryText -notmatch '(?s)--timeout-seconds\s+\$DecisionTimeoutSeconds') {
    throw 'Simulation mode must pass DecisionTimeoutSeconds to the driver.'
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
foreach ($requiredText in @('-Mode Simulation', '-Mode Real', 'does not prove real SGAI')) {
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
    documentedModes = @('Simulation', 'Real')
} | ConvertTo-Json -Depth 4
