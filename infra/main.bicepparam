using 'main.bicep'

param location = 'swedencentral'
param namePrefix = 'aifiqh'

// Explicit tags, not 'latest': Container Apps only rolls a new revision when
// the image reference changes, so re-pushing under the same tag deploys
// nothing. Bump these (v2, v3, ...) with each new image.
param webImageTag = 'v1'
param ollamaImageTag = 'v1'

// Not secrets, but specific to your Azure OpenAI resource (from .env).
param azureOpenAiEndpoint = 'https://randi.cognitiveservices.azure.com/'
param azureOpenAiDeployment = 'gpt-5.4'
