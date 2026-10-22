// Parameters for infra/main.bicep. Values that differ per person (image, your IP, e-mail) come from environment
// variables that infra/deploy.ps1 sets, so nothing personal is committed. Everything else uses the defaults in
// main.bicep (regions, matches, rate limits, budget).
using 'main.bicep'

param mcpImage = readEnvironmentVariable('PITCHSIDE_MCP_IMAGE')
param allowedIpRanges = split(readEnvironmentVariable('PITCHSIDE_ALLOWED_IPS'), ',')
param budgetContactEmails = split(readEnvironmentVariable('PITCHSIDE_BUDGET_EMAIL'), ',')
param location = readEnvironmentVariable('PITCHSIDE_LOCATION', 'swedencentral')
param staticWebAppLocation = readEnvironmentVariable('PITCHSIDE_SWA_LOCATION', 'westeurope')
param budgetAmount = int(readEnvironmentVariable('PITCHSIDE_BUDGET_AMOUNT', '10'))
