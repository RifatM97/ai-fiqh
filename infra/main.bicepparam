using 'main.bicep'

param location = 'swedencentral'
param namePrefix = 'aifiqh'

// The running environment: in the VNet (snet-aca), still publicly reachable
// (environmentInternal defaults to false). Kept here rather than passed on the
// command line, so a plain redeploy reproduces what is running instead of
// falling back to the defaults and attempting the old non-VNet environment.
param useVnet = true
param environmentName = 'cae-aifiqh-net'

// Fallback image tags only. Once CI/CD runs, GitHub Actions owns the running
// image (tagged with the commit SHA), so these go stale on purpose. Deploy
// infrastructure with `infra/deploy.sh`, which reads the tags actually running
// and passes them through — running Bicep directly with these values would
// roll the apps back to them (§8).
param webImageTag = 'v4'
param ollamaImageTag = 'v2'

// Who can reach the web app at all (§4d). Home broadband (Community Fibre):
// usually stable, not guaranteed static. If it changes you get a 403 before
// the sign-in page — update this and run infra/deploy.sh, or the one-line CLI
// fix in §4d. Office, VPN and mobile are blocked unless added here.
param allowedIpRanges = [
  '185.238.221.84/32'
]

// 1 = GPU fallback kept warm, billed continuously. Set to 0 between sessions
// to stop the cost; the fallback times out from cold until it's back to 1.
param ollamaMinReplicas = 1

// Not secrets, but specific to your Azure OpenAI resource (from .env).
param azureOpenAiEndpoint = 'https://randi.cognitiveservices.azure.com/'
param azureOpenAiDeployment = 'gpt-5.4'

// Google OAuth client ID (ends in .apps.googleusercontent.com); its secret
// goes only to Key Vault — docs/deployment.md §7b. Empty means sign-in is
// never set up. Once it has been, emptying this does NOT turn it off; use
// `az containerapp auth update --enabled false` (§7b).
param googleClientId = '760706835429-bjur3n33n3m5ijvd1fb4ect7u48bvo6q.apps.googleusercontent.com'

// The identity GitHub Actions deploys as (§8). Federated, so there is no
// secret to store in GitHub.
param deployCiIdentity = true

// This repo uses GitHub's immutable OIDC subjects, which embed these IDs:
//   gh api repos/RifatM97/ai-fiqh/actions/oidc/customization/sub
param githubOwnerId = '72074116'
param githubRepositoryId = '1318088740'
