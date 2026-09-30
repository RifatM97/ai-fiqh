// API Management as the single public entry point (docs/deployment.md §4b).
//
// Developer tier because it is the only affordable tier supporting classic
// VNet injection (the alternatives are Premium/Premium v2). It carries no SLA
// and is not for production — acceptable for a learning deployment, and worth
// remembering before anyone treats this as production-grade.
//
// External VNet mode: the gateway keeps a public hostname, while its outbound
// calls reach the internal-only Container Apps environment over the VNet.
// Provisioning takes 30-45+ minutes.

param location string
param apimName string
param apimSubnetId string
param publisherName string
param publisherEmail string

@description('Internal FQDN of ai-fiqh-web, e.g. ai-fiqh-web.internal.<env default domain>.')
param webInternalFqdn string

@description('Requests per minute per client IP before 429.')
param rateLimitCalls int = 60

resource apim 'Microsoft.ApiManagement/service@2022-08-01' = {
  name: apimName
  location: location
  sku: {
    name: 'Developer'
    capacity: 1
  }
  properties: {
    publisherName: publisherName
    publisherEmail: publisherEmail
    virtualNetworkType: 'External'
    virtualNetworkConfiguration: {
      subnetResourceId: apimSubnetId
    }
  }
}

// --- HTTP: the app itself ---------------------------------------------------
//
// `subscriptionRequired: false` is load-bearing: this is a browser-facing web
// app, and a browser has no APIM subscription key to send. Access control is
// the Google sign-in behind this, not an APIM key.
//
// APIM has no "any method" operation, so each method Streamlit uses gets a
// catch-all `/*` operation. A method that is missing here returns APIM's
// "Operation not found" 404 rather than reaching the app.
resource httpApi 'Microsoft.ApiManagement/service/apis@2022-08-01' = {
  parent: apim
  name: 'ai-fiqh-web'
  properties: {
    displayName: 'AI-Fiqh web'
    path: ''
    protocols: [
      'https'
    ]
    serviceUrl: 'https://${webInternalFqdn}'
    subscriptionRequired: false
  }
}

var proxiedMethods = [
  'GET'
  'POST'
  'PUT'
  'DELETE'
  'HEAD'
  'OPTIONS'
]

resource httpOperations 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = [
  for method in proxiedMethods: {
    parent: httpApi
    name: toLower(method)
    properties: {
      displayName: '${method} (all paths)'
      method: method
      urlTemplate: '/*'
    }
  }
]

// Rate limiting (§4b) plus the headers Container Apps' auth layer needs.
//
// Keyed on client IP, not on the signed-in user: the identity is established
// by Container Apps *behind* this gateway, so APIM has nothing to key on when
// a request arrives. §4b records this as the accepted trade — APIM handles
// volumetric protection, per-user fairness would need an app-level counter.
//
// X-Forwarded-Host is what makes sign-in work from behind the proxy: the auth
// layer's forwardProxy convention reads it to build redirect URLs with this
// gateway's hostname instead of the unreachable internal one. APIM does not
// set it by itself.
// Bicep multi-line strings are verbatim — no interpolation — so the rate is
// substituted into the placeholder below rather than written inline.
var policyXml = '''<policies>
  <inbound>
    <base />
    <rate-limit-by-key calls="__CALLS__" renewal-period="60"
      counter-key="@(context.Request.IpAddress)"
      remaining-calls-header-name="X-RateLimit-Remaining"
      retry-after-header-name="Retry-After" />
    <set-header name="X-Forwarded-Host" exists-action="override">
      <value>@(context.Request.OriginalUrl.Host)</value>
    </set-header>
    <set-header name="X-Forwarded-Proto" exists-action="override">
      <value>https</value>
    </set-header>
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>'''

resource httpPolicy 'Microsoft.ApiManagement/service/apis/policies@2022-08-01' = {
  parent: httpApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(policyXml, '__CALLS__', string(rateLimitCalls))
  }
  dependsOn: [
    httpOperations
  ]
}

// --- WebSocket: Streamlit's UI channel -------------------------------------
//
// Streamlit drives its whole interface over a WebSocket at /_stcore/stream, and
// APIM models WebSocket as a separate API type from HTTP with a much smaller
// policy surface (policies run on the handshake only). This is the piece most
// likely to need rework — see the risks in the phase 3 plan. If the UI cannot
// hold a connection through here, the alternative is Azure Front Door, which
// passes WebSockets without a separate API definition.
resource wsApi 'Microsoft.ApiManagement/service/apis@2022-08-01' = {
  parent: apim
  name: 'ai-fiqh-web-stream'
  properties: {
    displayName: 'AI-Fiqh web (Streamlit WebSocket)'
    type: 'websocket'
    path: '_stcore/stream'
    protocols: [
      'wss'
    ]
    serviceUrl: 'wss://${webInternalFqdn}/_stcore/stream'
    subscriptionRequired: false
  }
}

output apimName string = apim.name
output gatewayHostname string = replace(replace(apim.properties.gatewayUrl, 'https://', ''), '/', '')
output gatewayUrl string = apim.properties.gatewayUrl
