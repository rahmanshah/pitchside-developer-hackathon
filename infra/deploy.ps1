<#
.SYNOPSIS
    Deploys Pitchside to one Azure resource group: MCP server on Container Apps, Static Web App, logs, budget.

.DESCRIPTION
    Five stages. Every stage is safe to re-run (resource group creation and ARM deployments are idempotent), and
    -FromStage resumes after a failure without redoing what already worked:

      preflight   az CLI present and new enough, signed in, inputs valid; shows the subscription and asks to go on
      providers   registers Microsoft.App / OperationalInsights / Web / Consumption if needed, and checks that the
                  chosen regions are offered for Container Apps and Static Web Apps (stops if not)
      group       creates or updates the tagged resource group
      deploy      what-if preview, confirmation, then the Bicep deployment (infra/main.bicep + main.bicepparam)
      verify      waits for the MCP server's /healthz from this machine (an allowed IP), writes
                  infra/.deploy-outputs.json and prints the next steps

    Nothing here stores a secret: the image, your IP and the alert e-mail are passed as environment variables
    that main.bicepparam reads, for this process only. See docs/AZURE.md for the full order of steps.

.EXAMPLE
    ./infra/deploy.ps1 -Image ghcr.io/you/pitchside-mcp:<sha> -AllowedIp 203.0.113.7 -BudgetEmail you@example.com

.EXAMPLE
    ./infra/deploy.ps1 -Image ghcr.io/you/pitchside-mcp:<sha> -AllowedIp 203.0.113.7 -BudgetEmail you@example.com -FromStage verify
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Image,
    [Parameter(Mandatory = $true)][string[]]$AllowedIp,
    [Parameter(Mandatory = $true)][string]$BudgetEmail,
    [string]$ResourceGroup = "rg-pitchside",
    [string]$Location = "swedencentral",
    [string]$StaticWebAppLocation = "westeurope",
    [int]$BudgetAmount = 10,
    [ValidateSet("preflight", "providers", "group", "deploy", "verify")][string]$FromStage = "preflight",
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0

$Root = Split-Path -Parent $PSScriptRoot
$ParamFile = Join-Path $PSScriptRoot "main.bicepparam"
$OutputsFile = Join-Path $PSScriptRoot ".deploy-outputs.json"
$DeploymentName = "pitchside-main"
$Stages = @("preflight", "providers", "group", "deploy", "verify")
$MinAzVersion = [version]"2.53.0"   # .bicepparam support (Bicep CLI docs, "Deploy Bicep files with the Azure CLI")
$Providers = @("Microsoft.App", "Microsoft.OperationalInsights", "Microsoft.Web", "Microsoft.Consumption")
$Tags = @("project=pitchside", "env=demo", "purpose=hackathon-submission", "expires=2026-11-10", "managed-by=bicep")

function Write-Stage([string]$Name, [string]$Text) {
    Write-Host ""
    Write-Host "== [$Name] $Text" -ForegroundColor Cyan
}

function Invoke-Az {
    <# Runs az with the given arguments; throws with a resume hint if it fails. Returns stdout text. #>
    param([Parameter(Mandatory = $true)][string[]]$Arguments, [string]$Stage = $script:CurrentStage)
    $output = & az @Arguments --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw "az $($Arguments[0..([Math]::Min(2, $Arguments.Count - 1))] -join ' ') failed (exit $LASTEXITCODE). " +
              "Fix the cause, then re-run with -FromStage $Stage."
    }
    return ($output -join "`n")
}

function Confirm-Step([string]$Question) {
    if ($Yes) { return }
    $answer = Read-Host "$Question [y/N]"
    if ($answer -notmatch '^(y|yes)$') { throw "Stopped by you. Re-run with -FromStage $script:CurrentStage to continue." }
}

function ConvertTo-Cidr([string]$Value) {
    $text = $Value.Trim()
    if ($text -notmatch '^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(/(\d{1,2}))?$') {
        throw "'$Value' is not an IPv4 address or CIDR range (IPv6 is not accepted by Container Apps IP rules)."
    }
    foreach ($octet in 1..4) {
        if ([int]$Matches[$octet] -gt 255) { throw "'$Value' is not a valid IPv4 address." }
    }
    $prefix = 32
    if ($Matches[6]) { $prefix = [int]$Matches[6] }
    if ($prefix -lt 8 -or $prefix -gt 32) { throw "'$Value': use a /8 to /32 range (a single IP is /32)." }
    return "$($Matches[1]).$($Matches[2]).$($Matches[3]).$($Matches[4])/$prefix"
}

function Get-RegionNames([string]$Namespace, [string]$Type) {
    <# Regions Azure offers for a resource type, normalised ("West Europe" -> "westeurope"). Empty if unknown. #>
    $query = "resourceTypes[?resourceType=='$Type'].locations | [0]"
    $json = Invoke-Az -Arguments @("provider", "show", "--namespace", $Namespace, "--query", $query, "-o", "json")
    if (-not $json -or $json -eq "null") { return @() }
    return @(($json | ConvertFrom-Json) | ForEach-Object { ($_ -replace '\s', '').ToLowerInvariant() })
}

function Assert-Region([string]$Namespace, [string]$Type, [string]$Region) {
    $regions = Get-RegionNames $Namespace $Type
    if ($regions.Count -eq 0) {
        Write-Warning "Azure returned no region list for $Namespace/$Type; continuing, the deployment will fail early if '$Region' is not offered."
        return
    }
    if ($regions -notcontains $Region.ToLowerInvariant()) {
        throw "$Namespace/$Type is not offered in '$Region'. Offered: $($regions -join ', '). Pass another region and re-run."
    }
    Write-Host "  $Namespace/$Type is offered in $Region"
}

# ------------------------------------------------------------------ inputs (validated on every run)
$Cidrs = @($AllowedIp | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() } | ForEach-Object { ConvertTo-Cidr $_ })
if ($Cidrs.Count -eq 0) { throw "-AllowedIp needs at least one IPv4 address." }
if ($Image -notmatch '^[a-z0-9.\-]+(:\d+)?/[a-z0-9._/\-]+:[A-Za-z0-9._\-]+$') {
    throw "-Image must be a full reference with a tag, e.g. ghcr.io/you/pitchside-mcp:<commit sha>."
}
if ($Image -match ':latest$') { throw "-Image: use the commit-sha tag the workflow printed, not 'latest' (Container Apps docs advise unique tags)." }
if ($BudgetEmail -notmatch '^[^@\s,]+@[^@\s,]+\.[^@\s,]+$') { throw "-BudgetEmail must be one e-mail address." }

$startIndex = [Array]::IndexOf($Stages, $FromStage)
function Test-Stage([string]$Name) { return [Array]::IndexOf($Stages, $Name) -ge $startIndex }

# ------------------------------------------------------------------ 1. preflight
$script:CurrentStage = "preflight"
if (Test-Stage "preflight") {
    Write-Stage "preflight" "Azure CLI, sign-in and inputs"
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        throw "Azure CLI (az) not found. Install it (docs/AZURE.md, step 1), open a new terminal, then re-run."
    }
    $azVersion = [version]((Invoke-Az -Arguments @("version", "-o", "json") | ConvertFrom-Json).'azure-cli')
    if ($azVersion -lt $MinAzVersion) { throw "Azure CLI $azVersion is too old; $MinAzVersion or later is needed (run 'az upgrade')." }
    $account = Invoke-Az -Arguments @("account", "show", "-o", "json") | ConvertFrom-Json
    Write-Host "  Azure CLI $azVersion"
    Write-Host "  Subscription: $($account.name) ($($account.id))"
    Write-Host "  Signed in as: $($account.user.name)"
    Write-Host "  Resource group: $ResourceGroup ($Location); Static Web App region: $StaticWebAppLocation"
    Write-Host "  MCP image: $Image"
    Write-Host "  MCP server reachable only from: $($Cidrs -join ', ')"
    Confirm-Step "Deploy to this subscription?"
}

# ------------------------------------------------------------------ 2. providers and regions
$script:CurrentStage = "providers"
if (Test-Stage "providers") {
    Write-Stage "providers" "resource providers and regions"
    foreach ($ns in $Providers) {
        $state = Invoke-Az -Arguments @("provider", "show", "--namespace", $ns, "--query", "registrationState", "-o", "tsv")
        if ($state -ne "Registered") {
            Write-Host "  registering $ns (was: $state) ..."
            Invoke-Az -Arguments @("provider", "register", "--namespace", $ns, "--wait") | Out-Null
        }
        Write-Host "  $ns registered"
    }
    Assert-Region "Microsoft.App" "managedEnvironments" $Location
    Assert-Region "Microsoft.App" "containerApps" $Location
    Assert-Region "Microsoft.OperationalInsights" "workspaces" $Location
    Assert-Region "Microsoft.Web" "staticSites" $StaticWebAppLocation
}

# ------------------------------------------------------------------ 3. resource group
$script:CurrentStage = "group"
if (Test-Stage "group") {
    Write-Stage "group" "resource group $ResourceGroup"
    $exists = Invoke-Az -Arguments @("group", "exists", "--name", $ResourceGroup)
    if ($exists.Trim() -eq "true") {
        $current = Invoke-Az -Arguments @("group", "show", "--name", $ResourceGroup, "--query", "location", "-o", "tsv")
        if ($current.Trim() -ne $Location) {
            throw "Resource group '$ResourceGroup' already exists in '$($current.Trim())', not '$Location'. Use -ResourceGroup with another name or -Location $($current.Trim())."
        }
        Write-Host "  exists in $Location; updating tags"
    }
    Invoke-Az -Arguments (@("group", "create", "--name", $ResourceGroup, "--location", $Location, "--tags") + $Tags + @("-o", "none")) | Out-Null
    Write-Host "  ready"
}

# Environment variables read by main.bicepparam (this process and its children only)
$env:PITCHSIDE_MCP_IMAGE = $Image
$env:PITCHSIDE_ALLOWED_IPS = ($Cidrs -join ",")
$env:PITCHSIDE_BUDGET_EMAIL = $BudgetEmail
$env:PITCHSIDE_LOCATION = $Location
$env:PITCHSIDE_SWA_LOCATION = $StaticWebAppLocation
$env:PITCHSIDE_BUDGET_AMOUNT = [string]$BudgetAmount

# ------------------------------------------------------------------ 4. deploy
$script:CurrentStage = "deploy"
if (Test-Stage "deploy") {
    Write-Stage "deploy" "what-if preview"
    $whatIf = Invoke-Az -Arguments @("deployment", "group", "what-if", "--resource-group", $ResourceGroup,
        "--parameters", $ParamFile)
    Write-Host $whatIf
    Confirm-Step "Apply these changes?"
    Write-Stage "deploy" "deploying (a few minutes; safe to re-run)"
    Invoke-Az -Arguments @("deployment", "group", "create", "--resource-group", $ResourceGroup,
        "--name", $DeploymentName, "--parameters", $ParamFile, "-o", "none") | Out-Null
    Write-Host "  deployment '$DeploymentName' succeeded"
}

# ------------------------------------------------------------------ 5. verify
$script:CurrentStage = "verify"
if (Test-Stage "verify") {
    Write-Stage "verify" "outputs and health check"
    $outputs = Invoke-Az -Arguments @("deployment", "group", "show", "--resource-group", $ResourceGroup,
        "--name", $DeploymentName, "--query", "properties.outputs", "-o", "json") | ConvertFrom-Json
    $result = [ordered]@{
        resourceGroup     = $ResourceGroup
        mcpUrl            = $outputs.mcpUrl.value
        mcpHealthUrl      = $outputs.mcpHealthUrl.value
        containerAppName  = $outputs.containerAppName.value
        staticWebAppName  = $outputs.staticWebAppName.value
        staticWebAppUrl   = $outputs.staticWebAppUrl.value
        logAnalytics      = $outputs.logAnalyticsWorkspaceName.value
        image             = $Image
        allowedIpRanges   = $Cidrs
        deployedUtc       = (Get-Date).ToUniversalTime().ToString("s") + "Z"
    }
    $result | ConvertTo-Json | Set-Content -Path $OutputsFile -Encoding UTF8
    Write-Host "  outputs written to $OutputsFile (git-ignored)"

    # The app scales to zero, so the first request starts a replica (cold start).
    $healthy = $false
    for ($i = 1; $i -le 12 -and -not $healthy; $i++) {
        try {
            $health = Invoke-RestMethod -Uri $result.mcpHealthUrl -TimeoutSec 20
            if ($health.status -eq "ok") {
                $healthy = $true
                Write-Host "  MCP server healthy: $($health.matches) matches loaded"
            }
        } catch {
            Write-Host "  waiting for the MCP server to start ($i/12): $($_.Exception.Message)"
            Start-Sleep -Seconds 10
        }
    }
    if (-not $healthy) {
        throw "The MCP server did not answer /healthz. If your public IP is not in $($Cidrs -join ', '), " +
              "requests are refused (docs/AZURE.md, 'Your IP changed'). Check the logs, then re-run with -FromStage verify."
    }

    Write-Host ""
    Write-Host "Done. Next steps (docs/AZURE.md, steps 7-10):" -ForegroundColor Green
    Write-Host "  1. In .env: PITCHSIDE_TOOLS=mcp and PITCHSIDE_MCP_URL=$($result.mcpUrl)"
    Write-Host "  2. Record the live runs: uv run pitchside run --live --tools mcp ... (docs/LIVE_EVAL.md)"
    Write-Host "  3. Static Web App '$($result.staticWebAppName)': copy its deployment token (portal: Overview >"
    Write-Host "     Manage deployment token) into the GitHub secret AZURE_STATIC_WEB_APPS_API_TOKEN"
    Write-Host "  4. GitHub: Actions > Static Web App > Run workflow; then open $($result.staticWebAppUrl)"
}
