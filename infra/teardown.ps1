<#
.SYNOPSIS
    Deletes the Pitchside resource group and everything in it. Run it only after the judging period (Nov 10, 2026).

.DESCRIPTION
    The Official Rules require the project to stay accessible to judges until the end of the Judging Period on
    2026-11-10 23:59 PT (2026-11-11 09:59 Helsinki), so this script refuses to run before 2026-11-11 12:00 Helsinki
    time (UTC+2, which is 10:00 UTC) unless you pass -Force. That instant is absolute, whatever this machine's time
    zone, and leaves about two hours of margin.
    It lists what will be deleted, asks you to type the resource group name, then runs `az group delete`.
    If the Foundry resource was created in the same resource group (docs/AZURE.md recommends it), it goes too.
    Not deleted here (manual, see docs/AZURE.md "After the judging period"): the GHCR image and the GitHub secret.

.EXAMPLE
    ./infra/teardown.ps1
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "rg-pitchside",
    [switch]$Force,
    [switch]$NoWait
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0
# Judging ends 2026-11-10 23:59 PT = 2026-11-11 09:59 Helsinki; the guard adds about two hours. A DateTimeOffset
# compares instants, so the machine's own time zone does not matter.
$TeardownFrom = [DateTimeOffset]::new(2026, 11, 11, 12, 0, 0, [TimeSpan]::FromHours(2))  # Helsinki, UTC+2

if ([DateTimeOffset]::UtcNow -lt $TeardownFrom -and -not $Force) {
    throw "Judging runs until 2026-11-10 23:59 PT (2026-11-11 09:59 Helsinki) and the demo must stay up until then " +
          "(Official Rules 4.8). Re-run after $($TeardownFrom.ToString('yyyy-MM-dd HH:mm zzz')) " +
          "($($TeardownFrom.UtcDateTime.ToString('HH:mm')) UTC), or pass -Force if you really mean it."
}
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw "Azure CLI (az) not found." }

$accountJson = & az account show --only-show-errors -o json
if ($LASTEXITCODE -ne 0) { throw "Not signed in: run 'az login' first." }
$account = $accountJson -join "`n" | ConvertFrom-Json
$exists = (& az group exists --name $ResourceGroup --only-show-errors).Trim()
if ($exists -ne "true") {
    Write-Host "Resource group '$ResourceGroup' does not exist in $($account.name): nothing to delete."
    return
}

Write-Host "Subscription: $($account.name) ($($account.id))"
Write-Host "These resources in '$ResourceGroup' will be DELETED (this cannot be undone):" -ForegroundColor Yellow
& az resource list --resource-group $ResourceGroup --only-show-errors --query "[].{name:name, type:type}" -o table
$typed = Read-Host "Type the resource group name to confirm"
if ($typed -ne $ResourceGroup) { throw "Names did not match; nothing deleted." }

$arguments = @("group", "delete", "--name", $ResourceGroup, "--yes", "--only-show-errors")
if ($NoWait) { $arguments += "--no-wait" }
& az @arguments
if ($LASTEXITCODE -ne 0) { throw "az group delete failed (exit $LASTEXITCODE); re-run the script to retry." }
if ($NoWait) { Write-Host "Deletion started; check with: az group exists --name $ResourceGroup" }
else { Write-Host "Deleted '$ResourceGroup'." -ForegroundColor Green }
Write-Host "Manual clean-up left: the GHCR package (GitHub > Packages > pitchside-mcp > Package settings > Delete)"
Write-Host "and the repository secret AZURE_STATIC_WEB_APPS_API_TOKEN (Settings > Secrets and variables > Actions)."
