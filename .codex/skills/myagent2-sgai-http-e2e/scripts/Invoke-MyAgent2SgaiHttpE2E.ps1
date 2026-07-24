[CmdletBinding()]
param(
    [ValidateSet('v1', 'v2')]
    [string]$ContractVersion = 'v1',
    [Alias('Mode')]
    [ValidateSet('Simulation', 'Real')]
    [string]$GatewayMode = 'Simulation',
    [string]$MyAgentRoot,
    [string]$SgaiRoot = 'D:\Projects\游戏场景数据\SGAI',
    [int]$MyAgentPort = 8000,
    [int]$GatewayPort = 19091,
    [int]$GatewayInnerPort = 20020,
    [string]$GatewayId = 'local-smoke-gateway',
    [Guid]$TestTenantId = '00000000-0000-0000-0000-000000000001',
    [string]$AppId = 'robot-gateway-smoke',
    [string]$AppSecret = 'robot-gateway-smoke-secret',
    [string]$SmokeAccount = 'AI1001',
    [string]$SmokePassword = '123456',
    [string]$LoginMode = 'account',
    [string]$LoginProfileKey = 'local-test-login',
    [string]$GameProfileKey = 'local-test-game',
    [string]$ServerId = '0',
    [string]$RoleId = '',
    [int]$StartupTimeoutSeconds = 60,
    [int]$RunningTimeoutSeconds = 60,
    [int]$DecisionTimeoutSeconds = 60,
    [switch]$SkipBuild,
    [switch]$SkipDependencyManagement,
    [switch]$KeepDependencies,
    [switch]$StopExistingPorts,
    [switch]$KeepServices
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-DefaultMyAgentRoot {
    $candidate = Join-Path $PSScriptRoot '..\..\..\..'
    return (Resolve-Path -LiteralPath $candidate).Path
}

function Assert-PathExists {
    param(
        [string]$Path,
        [string]$Message
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw $Message
    }
}

function Get-Sha256Hex {
    param([string]$Text)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        $hash = $sha.ComputeHash($bytes)
        return -join ($hash | ForEach-Object { $_.ToString('x2') })
    }
    finally {
        $sha.Dispose()
    }
}

function Get-HmacSha256Hex {
    param(
        [string]$Secret,
        [string]$Text
    )

    $hmac = [System.Security.Cryptography.HMACSHA256]::new([System.Text.Encoding]::UTF8.GetBytes($Secret))
    try {
        $hash = $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Text))
        return -join ($hash | ForEach-Object { $_.ToString('x2') })
    }
    finally {
        $hmac.Dispose()
    }
}

function ConvertTo-CanonicalJson {
    param([object]$Value)

    return $Value | ConvertTo-Json -Depth 32 -Compress
}

function Invoke-RobotGatewayRequest {
    param(
        [string]$BaseUrl,
        [string]$Path,
        [object]$Body,
        [string]$RequestId,
        [string]$AppId,
        [string]$Secret,
        [int]$ExpectedStatus,
        [string]$CaseName
    )

    $method = 'POST'
    $json = ConvertTo-CanonicalJson $Body
    $timestampMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds().ToString()
    $bodyHash = Get-Sha256Hex $json
    $signingText = "$method`n$Path`n$timestampMs`n$RequestId`n$bodyHash"
    $signature = Get-HmacSha256Hex -Secret $Secret -Text $signingText
    $headers = @{
        'X-RequestId' = $RequestId
        'X-AppId' = $AppId
        'X-TimestampMs' = $timestampMs
        'X-Signature' = $signature
    }

    $uri = "$BaseUrl$Path"
    $response = Invoke-WebRequest -Uri $uri -Method $method -Headers $headers -Body $json -ContentType 'application/json' -UseBasicParsing -SkipHttpErrorCheck
    $statusCode = [int]$response.StatusCode
    $raw = [string]$response.Content

    if ($statusCode -ne $ExpectedStatus) {
        throw "[$CaseName] HTTP 状态不符合预期，expected=$ExpectedStatus actual=$statusCode body=$raw"
    }

    $parsed = $null
    if (-not [string]::IsNullOrWhiteSpace($raw)) {
        $parsed = $raw | ConvertFrom-Json
    }

    [pscustomobject]@{
        CaseName = $CaseName
        StatusCode = $statusCode
        Body = $parsed
        RawBody = $raw
        RequestJson = $json
    }
}

function Test-PortOpen {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutMilliseconds = 500
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Wait-PortOpen {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutSeconds,
        [string]$Name
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-PortOpen -HostName $HostName -Port $Port) {
            return
        }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)

    throw "$Name 未在 $TimeoutSeconds 秒内监听 ${HostName}:$Port"
}

function Wait-MyAgentHealth {
    param(
        [string]$BaseUrl,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $health = Invoke-RestMethod -Uri "$BaseUrl/health" -Method GET -TimeoutSec 3
            if ($health.status -eq 'ok') {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $deadline)

    throw "myAgent2 /health 未在 $TimeoutSeconds 秒内返回 status=ok"
}

function Wait-MyAgentV2Ready {
    param(
        [string]$BaseUrl,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $ready = Invoke-RestMethod -Uri "$BaseUrl/ready" -Method GET -TimeoutSec 3
            if ($ready.status -eq 'ready') {
                $capabilities = Invoke-RestMethod -Uri "$BaseUrl/api/gateway/v2/capabilities" -Method GET -TimeoutSec 3
                if ($capabilities.contractVersion -ne 'llm-gateway-http-v2') {
                    throw 'v2 capabilities contractVersion 不匹配。'
                }
                if ($capabilities.receiveEventsPath -ne '/api/gateway/v2/events') {
                    throw 'v2 capabilities receiveEventsPath 不匹配。'
                }
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $deadline)

    throw "myAgent2 /ready 和 v2 capabilities 未在 $TimeoutSeconds 秒内就绪。"
}

function Stop-PortOwners {
    param([int[]]$Ports)

    foreach ($port in $Ports) {
        $connections = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
        foreach ($connection in $connections) {
            $ownerProcessId = [int]$connection.OwningProcess
            if ($ownerProcessId -gt 0) {
                Write-Host "Stop existing listener: port=$port pid=$ownerProcessId"
                Stop-Process -Id $ownerProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Assert-PortAvailable {
    param([int[]]$Ports)

    foreach ($port in $Ports) {
        if (Test-PortOpen -HostName '127.0.0.1' -Port $port) {
            throw "端口 $port 已被占用。确认不是目标服务后，可显式加 -StopExistingPorts。"
        }
    }
}

function Set-ProcessEnvironment {
    param([hashtable]$Values)

    $previous = @{}
    foreach ($key in $Values.Keys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, [string]$Values[$key], 'Process')
    }
    return $previous
}

function Restore-ProcessEnvironment {
    param([hashtable]$Previous)

    foreach ($key in $Previous.Keys) {
        [Environment]::SetEnvironmentVariable($key, $Previous[$key], 'Process')
    }
}

function Get-RequiredProcessEnvironment {
    param([string]$Name)

    $value = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "$Name is required."
    }
    return $value
}

function Start-LoggedProcess {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [string]$StdoutLog,
        [string]$StderrLog
    )

    Remove-Item -LiteralPath $StdoutLog, $StderrLog -Force -ErrorAction SilentlyContinue
    Start-Process -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog `
        -WindowStyle Hidden `
        -PassThru
}

function Invoke-DotNetBuild {
    param([string]$SgaiRoot)

    $solution = Join-Path $SgaiRoot 'DotNet\DotNet.sln'
    Assert-PathExists -Path $solution -Message "SGAI DotNet.sln 不存在：$solution"
    dotnet build $solution -c Debug --nologo
    if ($LASTEXITCODE -ne 0) {
        throw "SGAI DotNet.sln 构建失败，exitCode=$LASTEXITCODE"
    }
}

function Wait-HostingRunning {
    param(
        [string]$BaseUrl,
        [object]$StatusBody,
        [string]$AppId,
        [string]$Secret,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $index = 0
    do {
        $index++
        $status = Invoke-RobotGatewayRequest `
            -BaseUrl $BaseUrl `
            -Path '/api/v1/hosting/status' `
            -Body $StatusBody `
            -RequestId "myagent2-sgai-e2e-status-$index" `
            -AppId $AppId `
            -Secret $Secret `
            -ExpectedStatus 200 `
            -CaseName 'status-running'

        $state = [string]$status.Body.state
        if ($state -eq 'Running') {
            return $status
        }

        if (@('Failed', 'KickedByUser', 'TokenExpired', 'Stopped') -contains $state) {
            $raw = $status.RawBody
            throw "托管 session 进入终态，state=$state body=$raw"
        }

        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    throw "托管 session 未在 $TimeoutSeconds 秒内进入 Running。"
}

function Wait-LlmMetrics {
    param(
        [string]$BaseUrl,
        [string]$AppId,
        [string]$Secret,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $index = 0
    do {
        $index++
        $metrics = Invoke-RobotGatewayRequest `
            -BaseUrl $BaseUrl `
            -Path '/api/v1/hosting/metrics' `
            -Body ([ordered]@{}) `
            -RequestId "myagent2-sgai-e2e-metrics-$index" `
            -AppId $AppId `
            -Secret $Secret `
            -ExpectedStatus 200 `
            -CaseName 'metrics-llm'

        $llmEventsSent = [long]$metrics.Body.metrics.llmEventsSent
        $llmDecisionsAccepted = [long]$metrics.Body.metrics.llmDecisionsAccepted
        if ($llmEventsSent -gt 0 -and $llmDecisionsAccepted -gt 0) {
            return $metrics
        }

        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    throw "未在 $TimeoutSeconds 秒内观察到双向 LLM HTTP 闭环：llmEventsSent>0 且 llmDecisionsAccepted>0。"
}

function Test-DockerReady {
    $serverVersion = & docker info --format '{{.ServerVersion}}' 2>$null
    return $LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace([string]$serverVersion)
}

function Wait-DockerReady {
    param([int]$TimeoutSeconds)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (Test-DockerReady) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    throw "Docker Desktop 未在 $TimeoutSeconds 秒内就绪。"
}

function Test-DockerContainerRunning {
    param([string]$Name)

    $running = & docker inspect --format '{{.State.Running}}' $Name 2>$null
    return $LASTEXITCODE -eq 0 -and ([string]$running).Trim().ToLowerInvariant() -eq 'true'
}

function Wait-DockerContainerHealthy {
    param(
        [string]$Name,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $health = & docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $Name 2>$null
        if ($LASTEXITCODE -eq 0 -and ([string]$health).Trim() -eq 'healthy') {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    throw "Docker 容器 $Name 未在 $TimeoutSeconds 秒内达到 healthy。"
}

function Start-SimulationDependencies {
    param(
        [string]$MyAgentRoot,
        [int]$TimeoutSeconds,
        [switch]$SkipManagement,
        [hashtable]$State
    )

    if ($SkipManagement) {
        return
    }
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Simulation 模式需要 Docker CLI，或显式使用 -SkipDependencyManagement 并自行启动 PostgreSQL/Redis。'
    }

    $State.Managed = $true
    if (-not (Test-DockerReady)) {
        $State.DockerStarted = $true
        & docker desktop start | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Docker Desktop 启动失败，exitCode=$LASTEXITCODE"
        }
        Wait-DockerReady -TimeoutSeconds $TimeoutSeconds
    }

    $State.PostgresWasRunning = Test-DockerContainerRunning -Name 'myagent_dev_postgres'
    $State.RedisWasRunning = Test-DockerContainerRunning -Name 'myagent_dev_redis'
    $composeFile = Join-Path $MyAgentRoot 'docker-compose.dev.yml'
    Assert-PathExists -Path $composeFile -Message "myAgent2 docker-compose.dev.yml 不存在：$composeFile"

    & docker compose -f $composeFile up -d postgres redis | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL/Redis 启动失败，exitCode=$LASTEXITCODE"
    }
    Wait-DockerContainerHealthy -Name 'myagent_dev_postgres' -TimeoutSeconds $TimeoutSeconds
    Wait-DockerContainerHealthy -Name 'myagent_dev_redis' -TimeoutSeconds $TimeoutSeconds
}

function Stop-SimulationDependencies {
    param(
        [string]$MyAgentRoot,
        [hashtable]$State,
        [switch]$Keep
    )

    if ($Keep -or -not $State.Managed -or -not (Test-DockerReady)) {
        return
    }

    $composeFile = Join-Path $MyAgentRoot 'docker-compose.dev.yml'
    if (-not $State.PostgresWasRunning) {
        & docker compose -f $composeFile stop postgres | Out-Host
    }
    if (-not $State.RedisWasRunning) {
        & docker compose -f $composeFile stop redis | Out-Host
    }
    if ($State.DockerStarted) {
        & docker desktop stop | Out-Host
    }
}

function Invoke-SimulationMode {
    param(
        [string]$ContractVersion,
        [string]$MyAgentRoot,
        [int]$MyAgentPort,
        [int]$GatewayPort,
        [string]$GatewayId,
        [Guid]$TestTenantId,
        [string]$AppId,
        [string]$AppSecret,
        [int]$StartupTimeoutSeconds,
        [int]$DecisionTimeoutSeconds,
        [switch]$SkipDependencyManagement,
        [switch]$KeepDependencies,
        [switch]$StopExistingPorts,
        [switch]$KeepServices
    )

    if ($KeepServices) {
        throw 'Simulation 模式不支持 -KeepServices；Mock SGAI 随验证进程结束。基础设施可使用 -KeepDependencies 保留。'
    }

    $python = Join-Path $MyAgentRoot '.venv\Scripts\python.exe'
    $appMain = Join-Path $MyAgentRoot 'src\api\main.py'
    $simulationApp = Join-Path $PSScriptRoot 'simulation_myagent_app.py'
    $simulationDriver = Join-Path $PSScriptRoot 'simulation_driver.py'
    Assert-PathExists -Path $python -Message "myAgent2 Python 不存在：$python"
    Assert-PathExists -Path $appMain -Message "myAgent2 FastAPI 入口不存在：$appMain"
    Assert-PathExists -Path $simulationApp -Message "Simulation app 不存在：$simulationApp"
    Assert-PathExists -Path $simulationDriver -Message "Simulation driver 不存在：$simulationDriver"

    if ($StopExistingPorts) {
        Stop-PortOwners -Ports @($MyAgentPort, $GatewayPort)
        Start-Sleep -Milliseconds 500
    }
    else {
        Assert-PortAvailable -Ports @($MyAgentPort, $GatewayPort)
    }

    $runId = [Guid]::NewGuid().ToString('N').Substring(0, 12)
    $configuredRunRoot = [Environment]::GetEnvironmentVariable('MYAGENT2_E2E_RUN_ROOT', 'Process')
    $runRoot = if ([string]::IsNullOrWhiteSpace($configuredRunRoot)) {
        Join-Path $MyAgentRoot '.codex\skills\myagent2-sgai-http-e2e\.run'
    }
    else {
        $configuredRunRoot
    }
    $myAgentStdout = Join-Path $runRoot "simulation-myagent2-$runId.stdout.log"
    $myAgentStderr = Join-Path $runRoot "simulation-myagent2-$runId.stderr.log"
    $resultPath = Join-Path $runRoot "myagent2-sgai-http-simulation-result-$runId.json"
    $sessionEvidencePath = Join-Path $runRoot "myagent2-sgai-http-$ContractVersion-session-$runId.json"
    New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
    Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue

    $dependencyState = @{
        Managed = $false
        DockerStarted = $false
        PostgresWasRunning = $false
        RedisWasRunning = $false
    }
    $myAgentBaseUrl = "http://127.0.0.1:$MyAgentPort"
    $gatewayBaseUrl = "http://127.0.0.1:$GatewayPort"
    $eventAppId = $AppId
    $eventAppSecret = $AppSecret
    $decisionAppId = if ($ContractVersion -eq 'v2') { "simulation-decision-$runId" } else { $AppId }
    $decisionAppSecret = if ($ContractVersion -eq 'v2') { "simulation-decision-secret-$runId" } else { $AppSecret }
    $controlAppId = if ($ContractVersion -eq 'v2') { "simulation-control-$runId" } else { $AppId }
    $controlAppSecret = if ($ContractVersion -eq 'v2') { "simulation-control-secret-$runId" } else { $AppSecret }
    $appSecrets = @{}
    $appSecrets[$eventAppId] = $eventAppSecret
    $appGateways = @{}
    $appGateways[$eventAppId] = @($GatewayId)
    $appTenants = @{}
    $appTenants[$GatewayId] = if ($ContractVersion -eq 'v2') { $TestTenantId.ToString() } else { $GatewayId }
    $envValues = @{
        'LLM_GATEWAY_APP_SECRETS' = ($appSecrets | ConvertTo-Json -Compress)
        'LLM_GATEWAY_APP_GATEWAYS' = ($appGateways | ConvertTo-Json -Compress)
        'LLM_GATEWAY_APP_TENANTS' = ($appTenants | ConvertTo-Json -Compress)
        'LLM_GATEWAY_TIMESTAMP_TOLERANCE_MS' = '300000'
        'LLM_GATEWAY_IDEMPOTENCY_TTL_SECONDS' = '60'
        'LLM_GATEWAY_DECISION_URL' = "$gatewayBaseUrl/api/v1/hosting/llm/decision"
        'LLM_GATEWAY_DECISION_APP_ID' = $decisionAppId
        'LLM_GATEWAY_DECISION_APP_SECRET' = $decisionAppSecret
        'LLM_GATEWAY_DECISION_TIMEOUT_SECONDS' = '10'
        'LLM_GATEWAY_EVENT_STREAM_KEY' = "llm-gateway:e2e:$runId"
        'LLM_GATEWAY_EVENT_CONSUMER_GROUP' = "simulation-$runId"
        'LLM_GATEWAY_V1_ENABLED' = if ($ContractVersion -eq 'v1') { 'true' } else { 'false' }
        'LLM_GATEWAY_V2_ENABLED' = if ($ContractVersion -eq 'v2') { 'true' } else { 'false' }
        'LLM_GATEWAY_V2_POLL_MS' = '20'
        'EMBEDDING_ENABLED' = if ($ContractVersion -eq 'v2') { 'false' } else { 'true' }
        'RERANK_ENABLED' = if ($ContractVersion -eq 'v2') { 'false' } else { 'true' }
        'SGAI_SIM_APP_ID' = $eventAppId
        'SGAI_SIM_APP_SECRET' = $eventAppSecret
        'SGAI_SIM_EVENT_APP_ID' = $eventAppId
        'SGAI_SIM_EVENT_APP_SECRET' = $eventAppSecret
        'SGAI_SIM_DECISION_APP_ID' = $decisionAppId
        'SGAI_SIM_DECISION_APP_SECRET' = $decisionAppSecret
        'SGAI_SIM_CONTROL_APP_ID' = $controlAppId
        'SGAI_SIM_CONTROL_APP_SECRET' = $controlAppSecret
        'SGAI_SIM_GATEWAY_ID' = $GatewayId
    }
    if ($ContractVersion -eq 'v2') {
        $testPostgresDsn = [Environment]::GetEnvironmentVariable('TEST_POSTGRES_DSN', 'Process')
        if ([string]::IsNullOrWhiteSpace($testPostgresDsn)) {
            throw 'v2 Simulation 要求 TEST_POSTGRES_DSN 指向 myagent_test_* 测试库。'
        }
        $envValues['POSTGRES_DSN'] = $testPostgresDsn
    }

    $previousEnv = $null
    $myAgentProcess = $null
    try {
        Start-SimulationDependencies `
            -MyAgentRoot $MyAgentRoot `
            -TimeoutSeconds $StartupTimeoutSeconds `
            -SkipManagement:$SkipDependencyManagement `
            -State $dependencyState

        $previousEnv = Set-ProcessEnvironment -Values $envValues
        if ($ContractVersion -eq 'v2') {
            & $python (Join-Path $MyAgentRoot 'scripts\assert_gateway_v2_state.py') --preflight-test-database
            if ($LASTEXITCODE -ne 0) {
                throw 'TEST_POSTGRES_DSN 安全预检失败。'
            }
            & $python -m alembic upgrade head
            if ($LASTEXITCODE -ne 0) {
                throw 'v2 测试数据库迁移失败。'
            }
            & $python (Join-Path $MyAgentRoot 'scripts\seed_gateway_v2_test_tenant.py') `
                --tenant-id $TestTenantId.ToString() `
                --gateway-id $GatewayId
            if ($LASTEXITCODE -ne 0) {
                throw 'v2 测试 tenant seed 失败。'
            }
        }
        $myAgentProcess = Start-LoggedProcess `
            -FilePath $python `
            -ArgumentList @('-m', 'uvicorn', 'simulation_myagent_app:app', '--app-dir', $PSScriptRoot, '--host', '127.0.0.1', '--port', [string]$MyAgentPort) `
            -WorkingDirectory $MyAgentRoot `
            -StdoutLog $myAgentStdout `
            -StderrLog $myAgentStderr

        Wait-MyAgentHealth -BaseUrl $myAgentBaseUrl -TimeoutSeconds $StartupTimeoutSeconds
        if ($ContractVersion -eq 'v2') {
            Wait-MyAgentV2Ready -BaseUrl $myAgentBaseUrl -TimeoutSeconds $StartupTimeoutSeconds
        }

        $driverOutput = @(
            & $python $simulationDriver `
                --myagent-port $MyAgentPort `
                --gateway-port $GatewayPort `
                --run-id $runId `
                --contract-version $ContractVersion `
                --output $sessionEvidencePath `
                --timeout-seconds $DecisionTimeoutSeconds 2>&1
        )
        $driverExitCode = $LASTEXITCODE
        $driverText = $driverOutput -join [Environment]::NewLine
        if ($driverExitCode -ne 0) {
            throw "Simulation driver 失败，exitCode=$driverExitCode output=$driverText"
        }
        $driverResult = $driverText | ConvertFrom-Json
        if ($ContractVersion -eq 'v2') {
            if ([long]$driverResult.metricsAfter.llmEventsSent -ne 4 `
                -or [long]$driverResult.metricsAfter.llmEventsFailed -ne 0 `
                -or [long]$driverResult.metricsAfter.llmDecisionsAccepted -ne 2 `
                -or [long]$driverResult.metricsAfter.llmDecisionsRejected -ne 0) {
                throw "v2 Simulation 指标不符合预期：$driverText"
            }
            $databaseOutput = @(
                & $python (Join-Path $MyAgentRoot 'scripts\assert_gateway_v2_state.py') `
                    --session-file $sessionEvidencePath `
                    --expect-complete-cycle 2>&1
            )
            if ($LASTEXITCODE -ne 0) {
                throw "v2 数据库闭环断言失败：$($databaseOutput -join [Environment]::NewLine)"
            }
            $databaseResult = ($databaseOutput -join [Environment]::NewLine) | ConvertFrom-Json
        }
        elseif (-not $driverResult.success `
            -or [long]$driverResult.metrics.llmEventsSent -ne 2 `
            -or [long]$driverResult.metrics.llmEventsFailed -ne 0 `
            -or [long]$driverResult.metrics.llmDecisionsAccepted -ne 2 `
            -or [long]$driverResult.metrics.llmDecisionsRejected -ne 0) {
            throw "v1 Simulation 指标不符合预期：$driverText"
        }

        if ($ContractVersion -eq 'v2') {
            $summary = [ordered]@{
                success = $true
                contractVersion = 'llm-gateway-http-v2'
                gatewayMode = 'Simulation'
                provesRealSgai = $false
                evidence = $driverResult
                database = $databaseResult
                logs = [ordered]@{
                    myAgentStdout = $myAgentStdout
                    myAgentStderr = $myAgentStderr
                    sessionEvidence = $sessionEvidencePath
                    result = $resultPath
                }
            }
        }
        else {
            $summary = [ordered]@{
                success = $true
                contractVersion = 'llm-gateway-http-v1'
                gatewayMode = 'Simulation'
                provesRealSgai = $false
                runId = $runId
                myAgentBaseUrl = $myAgentBaseUrl
                gatewayBaseUrl = $gatewayBaseUrl
                accountLoginState = $driverResult.accountLoginState
                hostingState = $driverResult.hostingState
                eventResponse = $driverResult.eventResponse
                eventResponses = $driverResult.eventResponses
                llmMetrics = $driverResult.metrics
                decisions = $driverResult.decisions
                logs = [ordered]@{
                    myAgentStdout = $myAgentStdout
                    myAgentStderr = $myAgentStderr
                    result = $resultPath
                }
            }
        }
        $summary | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding UTF8
        $summary | ConvertTo-Json -Depth 10
    }
    finally {
        if ($myAgentProcess -ne $null -and -not $myAgentProcess.HasExited) {
            Stop-Process -Id $myAgentProcess.Id -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $myAgentProcess.Id -Timeout 5 -ErrorAction SilentlyContinue
        }
        if ($previousEnv -ne $null) {
            Restore-ProcessEnvironment -Previous $previousEnv
        }
        Stop-SimulationDependencies -MyAgentRoot $MyAgentRoot -State $dependencyState -Keep:$KeepDependencies
    }
}

function Invoke-RealV2Mode {
    param(
        [string]$MyAgentRoot,
        [string]$SgaiRoot,
        [int]$MyAgentPort,
        [int]$GatewayPort,
        [int]$GatewayInnerPort,
        [string]$GatewayId,
        [Guid]$TestTenantId,
        [int]$StartupTimeoutSeconds,
        [int]$DecisionTimeoutSeconds,
        [switch]$SkipBuild,
        [switch]$StopExistingPorts,
        [switch]$KeepServices
    )

    $runtimeFixturePath = Join-Path $MyAgentRoot 'tests\fixtures\llm_gateway_v2\gateway_runtime_config_keys.json'
    Assert-PathExists `
        -Path $runtimeFixturePath `
        -Message 'Real v2 要求 Gateway Task 0 导出的 gateway_runtime_config_keys.json；禁止猜测配置键。'
    $runtimeFixture = Get-Content -Raw -LiteralPath $runtimeFixturePath | ConvertFrom-Json
    $requiredRuntimeProperties = @(
        'enabledKey',
        'providerBaseUrlKey',
        'contractVersionKey',
        'capabilitiesPathKey',
        'eventsPathKey',
        'eventAppIdKey',
        'eventAppSecretKey',
        'gatewayIdKey',
        'decisionAppIdKey',
        'decisionAppSecretKey',
        'decisionPath'
    )
    foreach ($property in $requiredRuntimeProperties) {
        if ([string]::IsNullOrWhiteSpace([string]$runtimeFixture.$property)) {
            throw "Gateway runtime fixture missing $property."
        }
    }

    $eventAppId = Get-RequiredProcessEnvironment -Name 'E2E_EVENT_APP_ID'
    $eventAppSecret = Get-RequiredProcessEnvironment -Name 'E2E_EVENT_APP_SECRET'
    $decisionAppId = Get-RequiredProcessEnvironment -Name 'E2E_DECISION_APP_ID'
    $decisionAppSecret = Get-RequiredProcessEnvironment -Name 'E2E_DECISION_APP_SECRET'
    $controlAppId = Get-RequiredProcessEnvironment -Name 'E2E_GATEWAY_CONTROL_APP_ID'
    $controlAppSecret = Get-RequiredProcessEnvironment -Name 'E2E_GATEWAY_CONTROL_APP_SECRET'
    $testPostgresDsn = Get-RequiredProcessEnvironment -Name 'TEST_POSTGRES_DSN'
    if ($eventAppId -eq $decisionAppId -or $eventAppSecret -eq $decisionAppSecret) {
        throw 'Real v2 event 和 decision 身份必须不同。'
    }

    $SgaiRoot = (Resolve-Path -LiteralPath $SgaiRoot).Path
    $python = Join-Path $MyAgentRoot '.venv\Scripts\python.exe'
    $appDll = Join-Path $SgaiRoot 'Bin\App.dll'
    $gameConfigRoot = Join-Path $SgaiRoot 'Config\Excel\cs\GameConfig'
    Assert-PathExists -Path $python -Message "myAgent2 Python 不存在：$python"
    if (-not $SkipBuild) {
        Invoke-DotNetBuild -SgaiRoot $SgaiRoot
    }
    Assert-PathExists -Path $appDll -Message "SGAI App.dll 不存在：$appDll。请先构建 SGAI。"
    foreach ($configFile in @('robotgatewayllmconfigcategory.bytes', 'robotgatewayruntimeconfigcategory.bytes')) {
        Assert-PathExists `
            -Path (Join-Path $gameConfigRoot $configFile) `
            -Message "SGAI 配置缺失：Config/Excel/cs/GameConfig/$configFile。请先执行 Unity 导表。"
    }

    if ($StopExistingPorts) {
        Stop-PortOwners -Ports @($MyAgentPort, $GatewayPort, $GatewayInnerPort)
        Start-Sleep -Milliseconds 500
    }
    else {
        Assert-PortAvailable -Ports @($MyAgentPort, $GatewayPort, $GatewayInnerPort)
    }

    $myAgentBaseUrl = "http://127.0.0.1:$MyAgentPort"
    $gatewayBaseUrl = "http://127.0.0.1:$GatewayPort"
    $runRoot = Join-Path $MyAgentRoot '.codex\skills\myagent2-sgai-http-e2e\.run'
    $myAgentStdout = Join-Path $runRoot 'v2-real-myagent2.stdout.log'
    $myAgentStderr = Join-Path $runRoot 'v2-real-myagent2.stderr.log'
    $gatewayStdout = Join-Path $runRoot 'v2-real-sgai.stdout.log'
    $gatewayStderr = Join-Path $runRoot 'v2-real-sgai.stderr.log'
    $sessionEvidencePath = Join-Path $runRoot 'v2-real-session.json'
    $resultPath = Join-Path $runRoot 'v2-real-result.json'
    New-Item -ItemType Directory -Force -Path $runRoot | Out-Null

    $eventSecrets = @{}
    $eventSecrets[$eventAppId] = $eventAppSecret
    $eventGateways = @{}
    $eventGateways[$eventAppId] = @($GatewayId)
    $eventTenants = @{}
    $eventTenants[$GatewayId] = $TestTenantId.ToString()
    $envValues = @{
        'POSTGRES_DSN' = $testPostgresDsn
        'LLM_GATEWAY_V1_ENABLED' = 'true'
        'LLM_GATEWAY_V2_ENABLED' = 'true'
        'LLM_GATEWAY_APP_SECRETS' = ($eventSecrets | ConvertTo-Json -Compress)
        'LLM_GATEWAY_APP_GATEWAYS' = ($eventGateways | ConvertTo-Json -Compress)
        'LLM_GATEWAY_APP_TENANTS' = ($eventTenants | ConvertTo-Json -Compress)
        'LLM_GATEWAY_DECISION_URL' = "$gatewayBaseUrl$([string]$runtimeFixture.decisionPath)"
        'LLM_GATEWAY_DECISION_APP_ID' = $decisionAppId
        'LLM_GATEWAY_DECISION_APP_SECRET' = $decisionAppSecret
        'LLM_GATEWAY_DECISION_TIMEOUT_SECONDS' = '10'
        'EMBEDDING_ENABLED' = 'true'
        'RERANK_ENABLED' = 'true'
        'E2E_GATEWAY_CONTROL_APP_ID' = $controlAppId
        'E2E_GATEWAY_CONTROL_APP_SECRET' = $controlAppSecret
        'ROBOT_GATEWAY_PROCESS_INNER_PORT_20' = [string]$GatewayInnerPort
    }
    $gatewayRuntime = @{
        ([string]$runtimeFixture.enabledKey) = '1'
        ([string]$runtimeFixture.providerBaseUrlKey) = $myAgentBaseUrl
        ([string]$runtimeFixture.contractVersionKey) = 'llm-gateway-http-v2'
        ([string]$runtimeFixture.capabilitiesPathKey) = '/api/gateway/v2/capabilities'
        ([string]$runtimeFixture.eventsPathKey) = '/api/gateway/v2/events'
        ([string]$runtimeFixture.eventAppIdKey) = $eventAppId
        ([string]$runtimeFixture.eventAppSecretKey) = $eventAppSecret
        ([string]$runtimeFixture.gatewayIdKey) = $GatewayId
        ([string]$runtimeFixture.decisionAppIdKey) = $decisionAppId
        ([string]$runtimeFixture.decisionAppSecretKey) = $decisionAppSecret
    }
    foreach ($entry in $gatewayRuntime.GetEnumerator()) {
        $envValues[$entry.Key] = [string]$entry.Value
    }

    $previousEnv = $null
    $myAgentProcess = $null
    $gatewayProcess = $null
    try {
        $previousEnv = Set-ProcessEnvironment -Values $envValues
        & $python (Join-Path $MyAgentRoot 'scripts\assert_gateway_v2_state.py') --preflight-test-database
        if ($LASTEXITCODE -ne 0) {
            throw 'TEST_POSTGRES_DSN 安全预检失败。'
        }
        & $python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) {
            throw 'v2 测试数据库迁移失败。'
        }
        & $python (Join-Path $MyAgentRoot 'scripts\seed_gateway_v2_test_tenant.py') `
            --tenant-id $TestTenantId.ToString() `
            --gateway-id $GatewayId
        if ($LASTEXITCODE -ne 0) {
            throw 'v2 测试 tenant seed 失败。'
        }

        $myAgentProcess = Start-LoggedProcess `
            -FilePath $python `
            -ArgumentList @('-m', 'uvicorn', 'src.api.main:app', '--host', '127.0.0.1', '--port', [string]$MyAgentPort) `
            -WorkingDirectory $MyAgentRoot `
            -StdoutLog $myAgentStdout `
            -StderrLog $myAgentStderr
        Wait-MyAgentV2Ready -BaseUrl $myAgentBaseUrl -TimeoutSeconds $StartupTimeoutSeconds

        $gatewayProcess = Start-LoggedProcess `
            -FilePath 'dotnet' `
            -ArgumentList @($appDll, '--AppType=Server', '--Process=20', '--StartConfig=StartConfig/Localhost', '--CreateScenes=1', '--Console=0', '--Develop=1') `
            -WorkingDirectory (Join-Path $SgaiRoot 'Bin') `
            -StdoutLog $gatewayStdout `
            -StderrLog $gatewayStderr
        Wait-PortOpen `
            -HostName '127.0.0.1' `
            -Port $GatewayPort `
            -TimeoutSeconds $StartupTimeoutSeconds `
            -Name 'SGAI AiRobotGateway'

        & $python (Join-Path $MyAgentRoot 'scripts\invoke_gateway_v2_e2e.py') `
            --gateway-base-url $gatewayBaseUrl `
            --gateway-id $GatewayId `
            --output $sessionEvidencePath `
            --timeout-seconds $DecisionTimeoutSeconds
        if ($LASTEXITCODE -ne 0) {
            throw 'Gateway v2 Real driver 失败。'
        }
        $databaseOutput = @(
            & $python (Join-Path $MyAgentRoot 'scripts\assert_gateway_v2_state.py') `
                --session-file $sessionEvidencePath `
                --expect-complete-cycle 2>&1
        )
        if ($LASTEXITCODE -ne 0) {
            throw "Gateway v2 Real 数据库断言失败：$($databaseOutput -join [Environment]::NewLine)"
        }
        $summary = [ordered]@{
            success = $true
            contractVersion = 'llm-gateway-http-v2'
            gatewayMode = 'Real'
            provesRealSgai = $true
            evidence = Get-Content -Raw -LiteralPath $sessionEvidencePath | ConvertFrom-Json
            database = ($databaseOutput -join [Environment]::NewLine) | ConvertFrom-Json
            logs = [ordered]@{
                myAgentStdout = $myAgentStdout
                myAgentStderr = $myAgentStderr
                gatewayStdout = $gatewayStdout
                gatewayStderr = $gatewayStderr
                sessionEvidence = $sessionEvidencePath
                result = $resultPath
            }
        }
        $summary | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding UTF8
        $summary | ConvertTo-Json -Depth 10
    }
    finally {
        if (-not $KeepServices) {
            if ($gatewayProcess -ne $null -and -not $gatewayProcess.HasExited) {
                Stop-Process -Id $gatewayProcess.Id -Force -ErrorAction SilentlyContinue
                Wait-Process -Id $gatewayProcess.Id -Timeout 5 -ErrorAction SilentlyContinue
            }
            if ($myAgentProcess -ne $null -and -not $myAgentProcess.HasExited) {
                Stop-Process -Id $myAgentProcess.Id -Force -ErrorAction SilentlyContinue
                Wait-Process -Id $myAgentProcess.Id -Timeout 5 -ErrorAction SilentlyContinue
            }
        }
        if ($previousEnv -ne $null) {
            Restore-ProcessEnvironment -Previous $previousEnv
        }
    }
}

if ([string]::IsNullOrWhiteSpace($MyAgentRoot)) {
    $MyAgentRoot = Resolve-DefaultMyAgentRoot
}
else {
    $MyAgentRoot = (Resolve-Path -LiteralPath $MyAgentRoot).Path
}

if ($GatewayMode -eq 'Simulation') {
    Invoke-SimulationMode `
        -ContractVersion $ContractVersion `
        -MyAgentRoot $MyAgentRoot `
        -MyAgentPort $MyAgentPort `
        -GatewayPort $GatewayPort `
        -GatewayId $GatewayId `
        -TestTenantId $TestTenantId `
        -AppId $AppId `
        -AppSecret $AppSecret `
        -StartupTimeoutSeconds $StartupTimeoutSeconds `
        -DecisionTimeoutSeconds $DecisionTimeoutSeconds `
        -SkipDependencyManagement:$SkipDependencyManagement `
        -KeepDependencies:$KeepDependencies `
        -StopExistingPorts:$StopExistingPorts `
        -KeepServices:$KeepServices
    return
}

if ($SkipDependencyManagement -or $KeepDependencies) {
    throw '-SkipDependencyManagement 和 -KeepDependencies 仅适用于 Simulation 模式。'
}

if ($ContractVersion -eq 'v2') {
    Invoke-RealV2Mode `
        -MyAgentRoot $MyAgentRoot `
        -SgaiRoot $SgaiRoot `
        -MyAgentPort $MyAgentPort `
        -GatewayPort $GatewayPort `
        -GatewayInnerPort $GatewayInnerPort `
        -GatewayId $GatewayId `
        -TestTenantId $TestTenantId `
        -StartupTimeoutSeconds $StartupTimeoutSeconds `
        -DecisionTimeoutSeconds $DecisionTimeoutSeconds `
        -SkipBuild:$SkipBuild `
        -StopExistingPorts:$StopExistingPorts `
        -KeepServices:$KeepServices
    return
}

$SgaiRoot = (Resolve-Path -LiteralPath $SgaiRoot).Path

$myAgentBaseUrl = "http://127.0.0.1:$MyAgentPort"
$gatewayBaseUrl = "http://127.0.0.1:$GatewayPort"
$runRoot = Join-Path $MyAgentRoot '.codex\skills\myagent2-sgai-http-e2e\.run'
$myAgentStdout = Join-Path $runRoot 'myagent2-api.stdout.log'
$myAgentStderr = Join-Path $runRoot 'myagent2-api.stderr.log'
$gatewayStdout = Join-Path $runRoot 'sgai-airobotgateway.stdout.log'
$gatewayStderr = Join-Path $runRoot 'sgai-airobotgateway.stderr.log'
$resultPath = Join-Path $runRoot 'myagent2-sgai-http-e2e-result.json'

New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue

$python = Join-Path $MyAgentRoot '.venv\Scripts\python.exe'
$appMain = Join-Path $MyAgentRoot 'src\api\main.py'
$appDll = Join-Path $SgaiRoot 'Bin\App.dll'
$gameConfigRoot = Join-Path $SgaiRoot 'Config\Excel\cs\GameConfig'

Assert-PathExists -Path $python -Message "myAgent2 Python 不存在：$python"
Assert-PathExists -Path $appMain -Message "myAgent2 FastAPI 入口不存在：$appMain"

$requiredConfigFiles = @(
    'startprocessconfigcategory.bytes',
    'startsceneconfigcategory.bytes',
    'startzoneconfigcategory.bytes',
    'robotgatewaycoreconfigcategory.bytes',
    'robotgatewayllmconfigcategory.bytes',
    'robotgatewayruntimeconfigcategory.bytes',
    'robotgatewayskilldefaultsconfigcategory.bytes',
    'robotgatewayactivityconfigcategory.bytes',
    'robotgatewayconnectionprofilerowcategory.bytes',
    'robotgatewayconnectionprofilesettingscategory.bytes'
)
foreach ($configFile in $requiredConfigFiles) {
    Assert-PathExists -Path (Join-Path $gameConfigRoot $configFile) -Message "SGAI 配置缺失：Config/Excel/cs/GameConfig/$configFile。请先执行 Unity 导表。"
}

if ($StopExistingPorts) {
    Stop-PortOwners -Ports @($MyAgentPort, $GatewayPort, $GatewayInnerPort)
    Start-Sleep -Milliseconds 500
}
else {
    Assert-PortAvailable -Ports @($MyAgentPort, $GatewayPort, $GatewayInnerPort)
}

if (-not $SkipBuild) {
    Invoke-DotNetBuild -SgaiRoot $SgaiRoot
}
Assert-PathExists -Path $appDll -Message "SGAI App.dll 不存在：$appDll。请先构建 SGAI。"

$appSecrets = @{}
$appSecrets[$AppId] = $AppSecret
$appTenants = @{}
$appTenants[$GatewayId] = $GatewayId

$envValues = @{
    'GAME_DATA_SOURCE' = 'robotgateway'
    'ROBOTGATEWAY_BASE_URL' = $gatewayBaseUrl
    'ROBOTGATEWAY_CALLBACK_URL' = "$gatewayBaseUrl/callbacks/analysis"
    'LLM_GATEWAY_APP_SECRETS' = ($appSecrets | ConvertTo-Json -Compress)
    'LLM_GATEWAY_APP_TENANTS' = ($appTenants | ConvertTo-Json -Compress)
    'LLM_GATEWAY_DECISION_URL' = "$gatewayBaseUrl/api/v1/hosting/llm/decision"
    'LLM_GATEWAY_DECISION_APP_ID' = $AppId
    'LLM_GATEWAY_DECISION_APP_SECRET' = $AppSecret
    'LLM_GATEWAY_DECISION_TIMEOUT_SECONDS' = '10'
    'ROBOT_GATEWAY_CONTROL_ADDRESS' = "$gatewayBaseUrl/"
    'ROBOT_GATEWAY_GATEWAY_ID' = $GatewayId
    'ROBOT_GATEWAY_APP_ID' = $AppId
    'ROBOT_GATEWAY_APP_SECRET' = $AppSecret
    'ROBOT_GATEWAY_PROCESS_INNER_PORT_20' = [string]$GatewayInnerPort
    'ROBOT_GATEWAY_LLM_ENABLED' = '1'
    'ROBOT_GATEWAY_LLM_BASE_URL' = $myAgentBaseUrl
    'ROBOT_GATEWAY_LLM_RECEIVE_EVENTS_PATH' = '/api/gateway/events'
    'ROBOT_GATEWAY_LLM_TIMEOUT_MS' = '5000'
    'ROBOT_GATEWAY_LLM_RETRY_COUNT' = '2'
    'ROBOT_GATEWAY_LLM_DEFAULT_LEASE_TTL_MS' = '30000'
    'ROBOT_GATEWAY_LLM_MAX_EVENT_BATCH_SIZE' = '10'
    'ROBOT_GATEWAY_LLM_MAX_DECISION_TTL_MS' = '30000'
    'ROBOT_GATEWAY_LLM_RETRY_BACKOFF_BASE_MS' = '200'
}

$previousEnv = Set-ProcessEnvironment -Values $envValues
$myAgentProcess = $null
$gatewayProcess = $null
$sessionId = $null
$statusBody = $null

try {
    $myAgentProcess = Start-LoggedProcess `
        -FilePath $python `
        -ArgumentList @('-m', 'uvicorn', 'src.api.main:app', '--host', '127.0.0.1', '--port', [string]$MyAgentPort) `
        -WorkingDirectory $MyAgentRoot `
        -StdoutLog $myAgentStdout `
        -StderrLog $myAgentStderr

    Wait-MyAgentHealth -BaseUrl $myAgentBaseUrl -TimeoutSeconds $StartupTimeoutSeconds

    $gatewayProcess = Start-LoggedProcess `
        -FilePath 'dotnet' `
        -ArgumentList @($appDll, '--AppType=Server', '--Process=20', '--StartConfig=StartConfig/Localhost', '--CreateScenes=1', '--Console=0', '--Develop=1') `
        -WorkingDirectory (Join-Path $SgaiRoot 'Bin') `
        -StdoutLog $gatewayStdout `
        -StderrLog $gatewayStderr

    Wait-PortOpen -HostName '127.0.0.1' -Port $GatewayPort -TimeoutSeconds $StartupTimeoutSeconds -Name 'SGAI AiRobotGateway'

    $profileQueryBody = [ordered]@{
        gatewayId = $GatewayId
        targetKey = 'myagent2-sgai-e2e'
        includeDisabled = $true
    }
    $profileQuery = Invoke-RobotGatewayRequest `
        -BaseUrl $gatewayBaseUrl `
        -Path '/api/v1/hosting/connection-profiles' `
        -Body $profileQueryBody `
        -RequestId 'myagent2-sgai-e2e-connection-profiles' `
        -AppId $AppId `
        -Secret $AppSecret `
        -ExpectedStatus 200 `
        -CaseName 'connection-profiles'

    $spawn = [ordered]@{
        position = [ordered]@{ x = 1.25; y = 0; z = -3.5 }
        rotation = [ordered]@{ x = 0; y = 0; z = 0; w = 1 }
    }
    $policy = [ordered]@{
        scriptId = 'myagent2-sgai-e2e'
        scriptPayload = ''
        tickIntervalMs = 1000
        maxReconnectCount = 1
        circuitBreakerFailureThreshold = 3
        allowedSkills = @('observe_state', 'move_to', 'stop_move', 'jump', 'play_action', 'scene_tornado', 'stop_hosting')
    }
    $accountLoginBody = [ordered]@{
        gatewayId = $GatewayId
        accountGroupId = 'myagent2-sgai-e2e-group'
        account = $SmokeAccount
        loginMode = $LoginMode
        password = $SmokePassword
        loginProfileKey = $LoginProfileKey
        gameProfileKey = $GameProfileKey
        serverId = $ServerId
        roleId = $RoleId
        targetSceneId = 0
        spawn = $spawn
        policy = $policy
    }

    $start = Invoke-RobotGatewayRequest `
        -BaseUrl $gatewayBaseUrl `
        -Path '/api/v1/hosting/account-login-start' `
        -Body $accountLoginBody `
        -RequestId 'myagent2-sgai-e2e-account-login-start' `
        -AppId $AppId `
        -Secret $AppSecret `
        -ExpectedStatus 200 `
        -CaseName 'account-login-start'

    $sessionId = [string]$start.Body.sessionId
    if ([string]::IsNullOrWhiteSpace($sessionId)) {
        throw 'account-login-start 未返回 sessionId。'
    }

    $statusBody = [ordered]@{
        sessionId = $sessionId
        accountId = ''
        roleId = $RoleId
    }

    $runningStatus = Wait-HostingRunning `
        -BaseUrl $gatewayBaseUrl `
        -StatusBody $statusBody `
        -AppId $AppId `
        -Secret $AppSecret `
        -TimeoutSeconds $RunningTimeoutSeconds

    $statusBody.accountId = [string]$runningStatus.Body.accountId
    $statusBody.roleId = [string]$runningStatus.Body.roleId

    $metrics = Wait-LlmMetrics `
        -BaseUrl $gatewayBaseUrl `
        -AppId $AppId `
        -Secret $AppSecret `
        -TimeoutSeconds $DecisionTimeoutSeconds

    $stopBody = [ordered]@{
        sessionId = $sessionId
        accountId = $statusBody.accountId
        roleId = $statusBody.roleId
        reason = 'myagent2-sgai-e2e-cleanup'
    }
    $stop = Invoke-RobotGatewayRequest `
        -BaseUrl $gatewayBaseUrl `
        -Path '/api/v1/hosting/stop' `
        -Body $stopBody `
        -RequestId 'myagent2-sgai-e2e-stop' `
        -AppId $AppId `
        -Secret $AppSecret `
        -ExpectedStatus 200 `
        -CaseName 'stop'

    $summary = [pscustomobject]@{
        success = $true
        mode = 'Real'
        provesRealSgai = $true
        myAgentBaseUrl = $myAgentBaseUrl
        gatewayBaseUrl = $gatewayBaseUrl
        sessionId = $sessionId
        profileStatusCode = $profileQuery.StatusCode
        startState = $start.Body.state
        runningState = $runningStatus.Body.state
        stopState = $stop.Body.state
        llmMetrics = [pscustomobject]@{
            llmEventsQueued = $metrics.Body.metrics.llmEventsQueued
            llmEventsSent = $metrics.Body.metrics.llmEventsSent
            llmEventsFailed = $metrics.Body.metrics.llmEventsFailed
            llmEventsDropped = $metrics.Body.metrics.llmEventsDropped
            llmDecisionsAccepted = $metrics.Body.metrics.llmDecisionsAccepted
            llmDecisionsRejected = $metrics.Body.metrics.llmDecisionsRejected
        }
        logs = [pscustomobject]@{
            myAgentStdout = $myAgentStdout
            myAgentStderr = $myAgentStderr
            gatewayStdout = $gatewayStdout
            gatewayStderr = $gatewayStderr
            result = $resultPath
        }
    }

    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $resultPath -Encoding UTF8
    $summary | ConvertTo-Json -Depth 8
}
finally {
    Restore-ProcessEnvironment -Previous $previousEnv

    if (-not $KeepServices) {
        if ($gatewayProcess -ne $null -and -not $gatewayProcess.HasExited) {
            Stop-Process -Id $gatewayProcess.Id -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $gatewayProcess.Id -Timeout 5 -ErrorAction SilentlyContinue
        }
        if ($myAgentProcess -ne $null -and -not $myAgentProcess.HasExited) {
            Stop-Process -Id $myAgentProcess.Id -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $myAgentProcess.Id -Timeout 5 -ErrorAction SilentlyContinue
        }
    }
}
