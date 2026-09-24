using 'main.bicep'

param location = 'swedencentral'
param namePrefix = 'aifiqh'

// Explicit tags, not 'latest': Container Apps only rolls a new revision when
// the image reference changes, so re-pushing under the same tag deploys
// nothing. Bump these (v2, v3, ...) with each new image.
param webImageTag = 'v3'
param ollamaImageTag = 'v2'

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
