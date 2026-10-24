# Deploying Pitchside on Azure

Everything in one resource group, from one Bicep template ([`infra/main.bicep`](../infra/main.bicep)), deployed by
[`infra/deploy.ps1`](../infra/deploy.ps1) and removed after the judging period by
[`infra/teardown.ps1`](../infra/teardown.ps1). Judges never need Azure: the project runs locally with one command and
no credentials (`uv run pitchside serve --open`, see the README).

```
                       GitHub Actions
     container.yml ──► ghcr.io/<owner>/pitchside-mcp:<sha>   (public package)
     static-web-app.yml ─────────────────────────────┐ deploy token (repository secret)
                                                     ▼
 rg-pitchside ─────────────────────────────────────────────────────────────────────────────
 │ Static Web App (Free, eastus2)            web/ + recorded run bundles ◄── judges (public)
 │ Container Apps env (Consumption, swedencentral)
 │   └─ pitchside-mcp  0.25 vCPU / 0.5 GiB, 0..1 replicas, IP allow-list, rate limit
 │        ▲  Streamable HTTP /mcp (PITCHSIDE_MCP_URL)
 │ Log Analytics (container logs, 1 GB/day cap)       Budget (25 EUR/month, e-mail alerts)
 │ Microsoft Foundry: gpt-5-mini deployments (created in the portal, same group recommended)
 └──────────────────────────────────────────────────────────────────────────────────────────
          ▲                                   ▲
          └── your machine: `uv run pitchside run --live --tools mcp` (az login, Entra ID)
```

The live recordings run on your machine under `az login`: the interpreter reaches the stats engine through the
**deployed** MCP server, the agents call the model deployments with Entra ID, and the run bundle records the server
URL (`pipeline.mcp_server`, shown in the UI's run info). The bundles are then published to the Static Web App.

The Static Web App is in `eastus2` because `westeurope`, the first choice, was refused for this subscription ("not
accepting new customers") when it was deployed (decision 27). The other resources stay in `swedencentral`.

The model is **gpt-5-mini** for now: the trial subscription has 0 quota for GPT-6 Luna, the target model, which
replaces it if quota is granted (decision 19). Only the deployments and `PITCHSIDE_PRICE_MODEL` change.

## What is deployed, and what is not

| Service | Status | Why |
|---|---|---|
| Azure Static Web Apps, Free | Deployed, in `eastus2` | Hosts the UI and recorded runs at $0; no server needed |
| Azure Container Apps, Consumption | In the template | Hosts the read-only MCP server; scales to zero |
| Log Analytics (Azure Monitor) | In the template | Container logs, one JSON line per MCP request |
| Cost Management budget | Deployed: 25 EUR a month on the resource group | E-mail at 50% and 100% of actual cost, and at 100% of forecast cost |
| Microsoft Foundry (gpt-5-mini) | Created by you in the portal | Model names and quota are account-specific; not guessed in code |
| Managed identity + cloud recorder | **Planned** | The production path: a Container Apps Job running the pipeline with a user-assigned identity holding Cognitive Services OpenAI User (`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`) on the Foundry account, writing bundles to Blob Storage. Not built: nothing deployed calls a model today, so an identity would be unused privilege |
| Entra ID built-in auth on the MCP server | **Planned** | The production way to protect it (app registration, app roles checked in code). Today: IP allow-list + rate limit, no keys |
| Application Insights (OpenTelemetry) | **Planned** | `microsoft-opentelemetry` with `APPLICATIONINSIGHTS_CONNECTION_STRING`; logs and platform metrics cover the demo |
| Cosmos DB, Event Grid, Functions, Translator, Speech | Not used | No consumer or need in a static replay; Spanish comes from the personalizer and is checked (decision 14) |

Only services that are actually deployed may be claimed in the README; everything else stays "planned".

## How the MCP server is protected

- **IP allow-list** on the ingress (`ipSecurityRestrictions`): only the addresses you pass to `deploy.ps1` get in;
  everyone else gets "RBAC: Access Denied". Judges don't need the server (the public demo is static).
- **Rate limit in the app** (`pitchside.ratelimit`): 120 requests per minute per client (burst 60), 300 per minute
  overall, 429 with `Retry-After`; one replica, so the in-memory limit covers every request. Container Apps has no
  built-in request rate limiting. A whole match uses about 10 requests offline.
- **Host header allow-list** (DNS-rebinding protection) set to the app's own name; `/healthz` for probes only.
- **No keys anywhere**: public image (no registry credentials), IP rules instead of shared secrets, the Log Analytics
  key resolved by Azure at deploy time.

## Roles you need

| Task | Role (scope) |
|---|---|
| Run `deploy.ps1` (providers, resource group, deployment, budget) | **Contributor** on the subscription. With Contributor on the resource group only, someone with subscription rights must register the four providers first |
| Live recording (your user calls the model deployments) | **Cognitive Services OpenAI User** (`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`) or **Foundry User** (`53ca6127-db72-4b80-b1b0-d745d6d5456d`) on the Foundry resource, assigned in the portal by an Owner |
| Make the GHCR package public, add the repository secret, run workflows | Admin of the GitHub repository |

The template creates no role assignments, so User Access Administrator is not needed.

## Steps, in order (planned for Oct 24)

Each step says what it does. No command here deletes anything except `teardown.ps1`, after Nov 10.

1. **Install the Azure CLI** (Microsoft's documented WinGet package), then open a new terminal:
   ```powershell
   winget install --exact --id Microsoft.AzureCLI
   az version            # deploy.ps1 needs 2.53.0 or later (.bicepparam support)
   ```
2. **Sign in and pick the subscription.** Opens a browser for Entra ID sign-in; no keys are stored in the repo.
   ```powershell
   az login
   az account show       # check this is the subscription you want; `az account set --subscription <id>` if not
   ```
3. **Build the MCP image.** GitHub > Actions > *MCP server image* > *Run workflow* on `main`. The run summary prints the
   image to deploy, `ghcr.io/<owner>/pitchside-mcp:<commit sha>`.
4. **Make the package public, once.** A new GHCR package is **private by default, even from a public repository**
   (visibility is not inherited; GitHub docs, "Configuring a package's access control and visibility"), and Container
   Apps pulls it without credentials. On GitHub: your profile > *Packages* > `pitchside-mcp` > *Package settings* >
   *Danger Zone* > *Change visibility* > *Public*, type the name, confirm. (It cannot be made private again.)
5. **Find your public IPv4 address** (router page or any service you trust). Only this address will reach the server.
6. **Deploy.** Shows the subscription and a what-if preview and asks before changing anything:
   ```powershell
   ./infra/deploy.ps1 -Image ghcr.io/<owner>/pitchside-mcp:<sha> -AllowedIp <your.ip> -BudgetEmail <you@example.com>
   ```
   Stages: `preflight` (CLI, sign-in, inputs), `providers` (registers Microsoft.App, OperationalInsights, Web and
   Consumption if needed; checks with `az provider show` that Container Apps is offered in `swedencentral` and Static
   Web Apps in `eastus2`, and stops if not), `group` (creates or updates `rg-pitchside` with tags), `deploy`
   (`az deployment group what-if`, then `az deployment group create --parameters infra/main.bicepparam`), `verify`
   (waits for `/healthz`, writes `infra/.deploy-outputs.json`, git-ignored). Every stage is idempotent: if one fails,
   fix the cause and re-run with `-FromStage <stage>`; re-running the whole script is also safe.
7. **Create Foundry and the gpt-5-mini deployments** in the portal, in `rg-pitchside` (Sweden Central, Global
   Standard), and assign yourself one of the roles above. Fill `.env` from `.env.example` (`PITCHSIDE_LLM=foundry`,
   `FOUNDRY_ENDPOINT`, `MODEL_*`, `PITCHSIDE_PRICE_MODEL=gpt-5-mini`), then `uv run python scripts/smoke_foundry.py`.
8. **Record the live runs through the cloud MCP server.** In `.env`: `PITCHSIDE_TOOLS=mcp` and
   `PITCHSIDE_MCP_URL=<mcpUrl from step 6>`, `PITCHSIDE_MATCH_BUDGET_USD=0.25` (the default). The app scales to zero after about 5
   idle minutes and a cold start takes about 15 s (measured at 0.25 vCPU), so wake it first and record the runs one
   after another:
   ```powershell
   Invoke-RestMethod <mcpHealthUrl from step 6>     # wait for status "ok" (retry for up to a minute)
   uv run pitchside run --live --seed 7 --storyline comeback --home aur --away irv
   uv run pitchside run --live --seed 3 --storyline siege --home har --away wst
   uv run pitchside run --live --seed 4 --storyline end_to_end --home wst --away aur --inject-faults demo
   uv run pitchside demo-data --bundle runs/bundles/<dir> --slug comeback --title "Comeback: Aurora Rovers v Ironvale Athletic"
   # ... same for siege and faults-demo; then check the run info panel shows "Stats tools: mcp (https://...)"
   uv run python scripts/capture_screenshots.py
   uv run pytest
   ```
   Every match the cloud server holds is listed in `mcpMatches` in `infra/main.bicep` (a test checks it covers every
   published run). The held-out evaluation ([LIVE_EVAL.md](LIVE_EVAL.md), seeds 201-205) uses the local tools by
   default; to run it against the cloud server, add those specs to `mcpMatches` first (each match adds a few seconds
   of start-up at 0.25 vCPU: 23 matches took about 63 s).
9. **Give GitHub the Static Web App deployment token.** Portal: Static Web App `pitchside-web` > *Overview* > *Manage
   deployment token* > copy. GitHub: repository *Settings* > *Secrets and variables* > *Actions* > *New repository
   secret*, name `AZURE_STATIC_WEB_APPS_API_TOKEN`. (`az staticwebapp secrets list --name pitchside-web
   --resource-group rg-pitchside` also lists it; treat the output as a secret.)
10. **Publish the site.** Commit and push the new `web/data`, then GitHub > Actions > *Static Web App* > *Run workflow*.
    It re-runs the data, static-site and replay-engine tests, then uploads `web/` (no Node; app build skipped). Open
    `staticWebAppUrl` and check the badge reads "Recorded run generated by the live pipeline".
11. **Update the README** to claim exactly what is now deployed (and the live URL), then tag `submission-v1`.

### Freezing `main` after submission without breaking the demo

The Static Web Apps workflow runs only when started by hand, so pushes (or no pushes) to `main` never change the live
site, and the last upload stays up until the resource group is deleted. The MCP server keeps running the image it was
deployed with (an immutable commit-sha tag). After tagging `submission-v1`, do any further work on a branch.

The deployed MCP server image is still the one built from `90ee028`. Commit D (decisions 24-26) is client-side only
(ledger, checker, prompts; the stats engine and the MCP tools are unchanged), so it needs no new image.

### Your IP changed

Re-run step 6 with the new `-AllowedIp` (it replaces the rule list), or update one rule (Container Apps IP
restriction docs):
```powershell
az containerapp ingress access-restriction set --name pitchside-mcp --resource-group rg-pitchside `
  --rule-name allow-0 --ip-address <new.ip>/32 --action Allow
```

### Logs

Portal: Container App `pitchside-mcp` > *Monitoring* > *Log stream* (live) or *Logs* (Log Analytics). Each MCP request
is one JSON line: method, path, status (429 when rate limited), client, milliseconds.

## Cost

Sources: Container Apps billing docs, Static Web Apps plans and quotas, Azure Monitor pricing page, GitHub Packages
billing (all read 2026-10-23). Measured numbers replace these after the deployment.

| Service | Price basis | Expected cost Oct 24 - Nov 10 |
|---|---|---|
| Static Web Apps, Free | $0; 100 GB/month bandwidth, no overage | $0 |
| Container Apps, Consumption | Free each month per subscription: 180,000 vCPU-s, 360,000 GiB-s, 2M requests; nothing while scaled to zero | $0 (an hour of recording at 0.25 vCPU is 900 vCPU-s) |
| Log Analytics | First 5 GB/month per billing account free, 31 days retention included | $0 (megabytes of logs; capped at 1 GB/day) |
| Budget alerts | No charge | $0 |
| GHCR (public package) | Free for public packages | $0 |
| Foundry, gpt-5-mini (Global Standard, Sweden Central) | $0.25 in / $0.03 cached / $2.00 out per 1M tokens (Azure OpenAI pricing page in EUR, at 1 USD = 0.88 EUR) | About $0.12-0.13 per storyline match with the MCP tools, about $2.10-2.30 for the 20-match live evaluation (estimates, decision 19); measured cost goes here after the runs |
| Foundry, GPT-6 Luna (if quota is granted) | $0.10 in / $0.01 cached / $0.50 out per 1M tokens | About $0.04 per storyline match (estimate, decision 14) |

The budget defaults to 25 in the billing currency, the amount deployed (25 EUR a month on the resource group, with
e-mail alerts at 50% and 100% of actual cost and at 100% of forecast cost); change it with `-BudgetAmount`.

## After Nov 10

```powershell
./infra/teardown.ps1     # refuses before 2026-11-11 unless -Force; lists resources; asks you to type the name
```
It runs `az group delete --name rg-pitchside --yes`, which also removes Foundry if it is in the same group. Then, by
hand: delete the GHCR package (Package settings > Delete) and the `AZURE_STATIC_WEB_APPS_API_TOKEN` secret. Keep the
repository public.

## What was verified offline

- `bicep build` and `bicep lint` (Bicep CLI 0.48.1) pass with no warnings; a misspelled property is reported, so the
  types for these API versions are checked: `Microsoft.App/containerApps@2026-07-01`,
  `Microsoft.App/managedEnvironments@2026-07-01`, `Microsoft.OperationalInsights/workspaces@2026-03-01`,
  `Microsoft.Web/staticSites@2025-03-01`, `Microsoft.Consumption/budgets@2026-06-01`.
- The image builds and runs locally (Docker) with the same arguments the template passes: health probe, MCP tool
  calls, 421 for a wrong Host, 429 after a burst, non-root user.
- `deploy.ps1` and `teardown.ps1` parse, and their input checks were exercised without Azure. **No `az` command was run
  while building this**; the first real run is step 6.
- Not verifiable offline: region availability (checked live by `deploy.ps1`), provider registration, quota, and the
  Static Web Apps region list (no official list is published; `deploy.ps1` checks the chosen region against
  `az provider show` before deploying). A listed region can still refuse a subscription: `westeurope` was refused at
  deployment ("not accepting new customers"), so the default is now `eastus2`, where the Static Web App was deployed.
