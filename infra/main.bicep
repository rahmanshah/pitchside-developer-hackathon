// Pitchside on Azure: everything in one resource group, deployed by infra/deploy.ps1 (see docs/AZURE.md).
//
//   Log Analytics workspace      Container Apps logs (daily ingestion cap)
//   Container Apps environment   Consumption workload profile
//   Container app (MCP server)   0.25 vCPU / 0.5 GiB, scale 0..1, IP allow-list, in-app rate limit
//   Static Web App (Free)        the web UI and its recorded run bundles (content deployed by a GitHub workflow)
//   Budget                       e-mail alerts on the resource group's cost
//
// Not here on purpose: a managed identity (nothing in this deployment calls a model; the live recording runs
// locally under `az login`) and the Foundry resource (created in the portal; model names are not guessed).
// API versions and property names checked against the Bicep reference docs (2026-10); `bicep build` and
// `bicep lint` validate them offline against the types bundled with the Bicep CLI.

targetScope = 'resourceGroup'

@description('Region for Container Apps and Log Analytics (next to the Foundry deployment).')
param location string = 'swedencentral'

@description('Region for the Static Web App. deploy.ps1 checks it against the regions Azure offers for Microsoft.Web/staticSites before deploying.')
param staticWebAppLocation string = 'westeurope'

@description('Prefix for every resource name.')
@minLength(3)
@maxLength(12)
param namePrefix string = 'pitchside'

@description('MCP server image, e.g. ghcr.io/<owner>/pitchside-mcp:<commit sha> (the package must be public).')
param mcpImage string

@description('IPv4 ranges (CIDR) allowed to reach the MCP server, e.g. ["203.0.113.7/32"]. Everything else is refused.')
@minLength(1)
param allowedIpRanges array

// Matches load at start-up. Measured locally at 0.25 vCPU: 3 matches are ready in about 15 s (89 MiB); 23 matches
// took about 63 s (319 MiB), longer than the pipeline's 30 s MCP request timeout after a scale-from-zero.
@description('Matches the MCP server holds, SEED:STORYLINE[:HOME:AWAY]. Must include every match a recorded run uses.')
param mcpMatches array = [
  '7:comeback:aur:irv'
  '3:siege:har:wst'
  '4:end_to_end:wst:aur'
]

@description('Requests per minute per client (MCP server rate limit).')
@minValue(1)
param rateLimitPerMinute int = 120

@description('Requests a client may make at once.')
@minValue(1)
param rateLimitBurst int = 60

@description('Requests per minute across all clients.')
@minValue(1)
param globalRateLimitPerMinute int = 300

@description('Log Analytics daily ingestion cap in GB (whole numbers; ingestion stops for the day when reached).')
@minValue(1)
param logDailyCapGb int = 1

@description('Monthly budget for the resource group, in the billing currency.')
@minValue(1)
param budgetAmount int = 10

@description('Who gets the budget alert e-mails.')
@minLength(1)
param budgetContactEmails array

@description('Budget start: the first day of the current month (Azure requires the first of a month).')
param budgetStartDate string = '${utcNow('yyyy-MM')}-01T00:00:00Z'

@description('Tags on every resource.')
param tags object = {
  project: 'pitchside'
  env: 'demo'
  purpose: 'hackathon-submission'
  expires: '2026-11-10'
  'managed-by': 'bicep'
}

var mcpAppName = '${namePrefix}-mcp'
var mcpPort = 8000
var generateArgs = flatten(map(mcpMatches, m => ['--generate', m]))

resource logs 'Microsoft.OperationalInsights/workspaces@2026-03-01' = {
  name: '${namePrefix}-logs'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      dailyQuotaGb: logDailyCapGb
    }
  }
}

resource env 'Microsoft.App/managedEnvironments@2026-07-01' = {
  name: '${namePrefix}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey // resolved by Azure at deploy time; never in git or outputs
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource mcp 'Microsoft.App/containerApps@2026-07-01' = {
  name: mcpAppName
  location: location
  tags: tags
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: mcpPort
        transport: 'auto'
        allowInsecure: false
        ipSecurityRestrictions: [
          for (cidr, i) in allowedIpRanges: {
            name: 'allow-${i}'
            description: 'Allowed caller of the MCP server'
            ipAddressRange: cidr
            action: 'Allow'
          }
        ]
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
    }
    template: {
      containers: [
        {
          name: 'mcp'
          image: mcpImage
          args: concat(
            [
              'mcp'
              '--transport'
              'streamable-http'
              '--host'
              '0.0.0.0'
              '--port'
              string(mcpPort)
              '--stateless'
              '--json-response'
              '--allowed-host'
              '${mcpAppName}.${env.properties.defaultDomain}'
              '--trust-forwarded'
              '--rate-limit'
              string(rateLimitPerMinute)
              '--burst'
              string(rateLimitBurst)
              '--global-rate-limit'
              string(globalRateLimitPerMinute)
            ],
            generateArgs
          )
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: mcpPort
              }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: mcpPort
              }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1 // one replica: the in-memory rate limit then covers every request
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
}

resource web 'Microsoft.Web/staticSites@2025-03-01' = {
  name: '${namePrefix}-web'
  location: staticWebAppLocation
  tags: tags
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    stagingEnvironmentPolicy: 'Disabled'
    allowConfigFileUpdates: true
  }
}

resource budget 'Microsoft.Consumption/budgets@2026-06-01' = {
  name: '${namePrefix}-budget'
  properties: {
    amount: budgetAmount
    category: 'Cost'
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    notifications: {
      actual50: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: budgetContactEmails
      }
      actual100: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: budgetContactEmails
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: budgetContactEmails
      }
    }
  }
}

@description('The MCP endpoint for PITCHSIDE_MCP_URL.')
output mcpUrl string = 'https://${mcp.properties.configuration.ingress.fqdn}/mcp'

@description('Health probe URL (from an allowed IP).')
output mcpHealthUrl string = 'https://${mcp.properties.configuration.ingress.fqdn}/healthz'

output containerAppName string = mcp.name
output staticWebAppName string = web.name
output staticWebAppUrl string = 'https://${web.properties.defaultHostname}'
output logAnalyticsWorkspaceName string = logs.name
