using 'main.bicep'

param location = 'uksouth'
param namePrefix = 'aifiqh'

param webImageTag = 'latest'
param ollamaImageTag = 'latest'

// Fill these in from your existing .env (AZURE_OPENAI_ENDPOINT /
// AZURE_OPENAI_DEPLOYMENT) before deploying — not secrets, but specific to
// your Azure OpenAI resource, so left as placeholders rather than guessed.
param azureOpenAiEndpoint = 'https://<your-azure-openai-resource>.openai.azure.com/'
param azureOpenAiDeployment = '<your-deployment-name>'
