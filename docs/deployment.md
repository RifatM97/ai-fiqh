# AI-Fiqh — Deployment

**Status:** exploration, architecture direction chosen 2026-09-16, nothing
built yet. This document exists to work through options before committing to
implementation. See `PROJECT_TRACKER.md` for build status — the app itself is
feature-complete; this is about putting it in front of users.

**Reframed 2026-09-16.** This is now explicitly a learning vehicle for
building and deploying AI applications on Azure, not just "ship the app
cheaply." That changes what "good" looks like here: depth of the Azure stack
and what it teaches now matters alongside cost and simplicity. Kept as an
explicit goal so later choices in this doc can be read against it.

**Working assumptions, current as of 2026-09-16:**

| Question | Answer |
|---|---|
| Audience | Public, low–medium traffic (tens to low hundreds of users) |
| Provider / tenant | **Resolved** — Azure, corporate policy permits this use (see §3) |
| Ollama fallback (`gemma4:12b`) | Keep it, self-hosted |
| Hosting stack | **Azure**, "Option A" shape (app + fallback model co-located in one environment) — see §4 |
| Cost approach | Right-size compute, minimize idle cost via auto-shutdown / scale-to-zero rather than run everything 24/7 |
| Learning scope | Full Azure-native stack: compute + Key Vault + Monitor/App Insights + IaC + CI/CD |
| Auth | **Added 2026-09-16** — Container Apps built-in auth, Microsoft Entra ID, any signed-in identity let in (no allowlist) — see §4a |
| IaC tool | **Resolved 2026-09-17** — Bicep |
| Ollama cold start (§4c) | Bake-into-image confirmed working; **compute sizing changed** — Consumption's 8 GiB ceiling OOM-killed the model in local testing, moved to a Dedicated workload profile at 16 GiB. End-to-end Azure cold start still unmeasured |
| Rate limiting | **Resolved 2026-09-17** — Azure API Management in front, not app-level — see §4b for a real nuance this creates |

Superseded from the previous pass: the Oracle Cloud free-tier VM idea (§4,
Option A originally) — Azure doesn't have an equivalent always-free shape
with enough RAM for a 12B model, and the brief has changed anyway now that
Azure is the explicit point of the exercise, not just the cheapest host.

---

## 1. What's actually being deployed

Unchanged from the first pass — restated because it still drives every
decision below:

| Property | Value | Why it matters for hosting |
|---|---|---|
| App | `src/ai_fiqh/app.py`, Streamlit, 3 tabs | Single Python process, stateless across sessions except `@st.cache_resource` |
| Retrieval index | `index/` — 1.3 MB (`chunks.json` + `embeddings.npy`, 177×1024 float32) | No vector DB, no external retrieval service. Ships as static files baked into the image; loads into memory once per process |
| Source PDF | `data/Fiqh Class Book.pdf`, 15 MB | **Not needed at runtime** — only `ingest.py` reads it. Don't ship `data/` to prod |
| Retrieval-time external calls | Voyage API (embeddings for grey-band multi-query, reranking) | Needs network egress + `VOYAGE_API_KEY`; billed per call |
| Generation | Azure OpenAI (default), Anthropic (`cloud` dep group), Ollama (fallback) | See §3 — provider/tenant question, now resolved |
| Concurrency model | Streamlit's default: one process, one thread per session rerun | Fine for read-only retrieval. LLM calls are per-request with `max_retries` + `guarded()` error handling already in place |
| Secrets | `.env` today: `VOYAGE_API_KEY`, `ANTHROPIC_API_KEY`, `AI_FIQH_LLM_PROVIDER`, `AZURE_OPENAI_ENDPOINT`/`_API_KEY`/`_DEPLOYMENT`, `AI_FIQH_LLM_FALLBACK_PROVIDER` | Moves to Key Vault (§4) — this is now a deliberate learning target, not just a chore |

---

## 2. The tension, restated for Azure

The original tension (free budget + self-hosted Ollama + public traffic) was
resolved for a generic cheap host by pointing at Oracle's free ARM tier.
That specific escape hatch doesn't exist on Azure:

- **Azure's free-tier VM allowance is a B1S** (1 vCPU, 1 GB RAM) — nowhere
  near enough to run `gemma4:12b`, which the tracker already measured
  needing real headroom at 16,384 context tokens.
- A VM actually sized for it — roughly a **B2ms class, ~2 vCPU / 8 GB RAM**
  — is real, ongoing money if left running 24/7. Rough order of magnitude
  from general Azure Linux VM pricing: **~$60–70/mo pay-as-you-go** — treat
  this as a planning estimate, not a quote; run it through the Azure Pricing
  Calculator before committing to a size.
- The brief also changed: since this is now explicitly about learning Azure
  deployment, the goal isn't "avoid Azure cost," it's "spend deliberately
  and learn the tools that make that spend controllable" — which is exactly
  what auto-shutdown / scale-to-zero patterns are for.

Given the chosen answers (scale-to-zero over 24/7, full Azure-native stack),
the natural fit is **Azure Container Apps** rather than a raw IaaS VM:
Container Apps supports true per-app scale-to-zero (via KEDA under the
hood), which an IaaS VM's "auto-shutdown schedule" only approximates (a
schedule turns the box off at a fixed time — it doesn't spin back up on the
next request without extra automation). Container Apps also happens to be
a more idiomatic "learn cloud-native Azure" surface than SSH-ing into a VM:
containers, managed identity, KEDA scaling rules, and a proper CI/CD path
onto a registry are all standard modern-Azure skills, which fits the
reframed goal directly.

---

## 3. Provider / tenant — resolved

Previously flagged as the thing that blocks shipping regardless of hosting
choice: Azure OpenAI was being accessed through a corporate Vodafone tenant,
which is a bigger acceptable-use question once traffic is public rather than
solo dev exercise.

**Resolved 2026-09-16:** corporate policy permits this use. Azure OpenAI
stays the production primary, per the existing `llm.py` default.

One thing worth a deliberate (not urgent) check before real public traffic
starts, purely as good practice rather than because anything is currently
wrong: confirm which subscription/resource group this deployment lands in —
a dedicated dev/learning subscription is cleaner than sharing quota, cost
attribution, and blast radius with other tenant workloads, if that choice is
available. Not a blocker, just worth being deliberate about once the Azure
resources are actually being provisioned.

---

## 4. Architecture — Azure-native, Container Apps

### Shape

Two container apps in one **Azure Container Apps environment** (shared
virtual network, one Log Analytics workspace), rather than one VM running
two processes:

```
                          Public internet
                                │
                                ▼
                 Azure API Management (sole public
                 entry point — rate limiting, §4b)
                                │
                                ▼
                    Microsoft Entra ID (OIDC login)
                              │
                              ▼
              Container Apps Authentication (Easy Auth)
              — validates token before a request reaches
                the app; app never sees an unauthenticated
                request to a protected route —
                              │
┌─ Azure Container Apps environment ──────────┼───────────────┐
│           (VNet-integrated — reachable only from APIM,        │
│            no ingress is public on either app any more)       │
│                                              ▼                │
│  ┌─ ai-fiqh-web ──────────┐      ┌─ ai-fiqh-ollama ───────┐ │
│  │ Streamlit app           │─────▶│ Ollama + gemma4:12b     │ │
│  │ min replicas: 1          │      │ min replicas: 0         │ │
│  │ (always reachable)       │      │ (true scale-to-zero,    │ │
│  │ internal ingress only,   │      │ model baked into image  │ │
│  │ reachable via APIM only  │      │ internal ingress only — │ │
│  │                          │      │ never exposed publicly  │ │
│  └──────────────────────────┘      └──────────────────────────┘
│           │                                    │
│           ▼                                    ▼
│     Key Vault (secrets via managed identity, no .env in the image)
│     Log Analytics + Application Insights (both apps' logs/metrics)
└──────────────────────────────────────────────────────────────┘
        │                          │
        ▼                          ▼
  Azure OpenAI (primary)     Voyage API (retrieval)
  Anthropic (kept, optional)
```

**Ingress, now consistent across both apps:** both `ai-fiqh-web` and
`ai-fiqh-ollama` are **internal-only ingress** — APIM becomes the one public
door for the whole environment, rather than `ai-fiqh-web` having its own
public endpoint alongside APIM. This is a change from the previous pass
(where `ai-fiqh-web` had external ingress and only the Ollama app was
internal) and a direct consequence of putting APIM in front for rate
limiting — see §4b for what this requires (VNet integration) and the one
real open question it creates (reconciling APIM's request-level rate
limiting with Container Apps' session-based browser login).

**Why two apps with different scaling, not one:** the web app needs to feel
responsive to a visitor at any time, so it stays warm (`min replicas: 1`).
The Ollama fallback fires rarely — 2/177 chunks per the 2026-09-10
tracker entry — so paying to keep an 8GB-class container warm 24/7 for
something that fires a handful of times a week is the wrong trade. Scale to
zero and accept the cold-start cost on the rare path instead.

### Components and what each one teaches

| Component | Role | Learning surface |
|---|---|---|
| **Azure Container Registry (ACR)** | Stores the two built images (`ai-fiqh-web`, `ai-fiqh-ollama`) | Image build/push, registry auth from CI |
| **Container Apps environment** | Hosts both apps, shared networking | KEDA-based scaling rules, `min`/`max` replicas, ingress |
| **Key Vault** | Holds `VOYAGE_API_KEY`, `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY`, etc. | Managed identity, secret references instead of env files |
| **Managed identity** (system-assigned, on `ai-fiqh-web`) | Lets the app pull secrets from Key Vault without a stored credential | Passwordless Azure auth pattern |
| **Log Analytics + Application Insights** | Centralized logs, request traces, latency | Real observability instead of `print()` — first place to see `Answer.fallback_used` and `Answer.low_confidence` actually fire in production |
| **Bicep** (IaC) | Declares all of the above as code | Repeatable environments, diffable infra, no portal clicking to reproduce |
| **GitHub Actions + OIDC federated credential** | Builds images, pushes to ACR, deploys new revisions | No long-lived Azure secret sitting in GitHub — federated identity trust instead |
| **Container Apps Authentication + Entra ID app registration** | Gates `ai-fiqh-web`'s route behind sign-in | OIDC/OAuth2 login flow, platform-level auth with no app code, principal-header propagation — see §4a |
| **Azure API Management** | Sole public entry point; rate limiting | Gateway policies (`rate-limit-by-key`, `validate-jwt`), VNet integration to reach a privately-networked backend — see §4b |

## 4a. Authentication

**Chosen 2026-09-16:** Container Apps' built-in **Authentication** add-on
(the platform-level "Easy Auth" pattern), provider **Microsoft Entra ID**,
policy **require authentication, any signed-in identity allowed in** — no
allowlist, no app roles. The point of auth here is "prove you're a real
person" for abuse/cost control, not gatekeeping who gets to use a public
Fiqh Q&A tool.

**Why platform-level rather than in-app (e.g. `streamlit-authenticator` or a
password gate):** the token validation happens before the request reaches
the container at all — the app never has to see, reject, or reason about an
unauthenticated request on a protected route. It's also the more transferable
skill: this is the same pattern App Service Easy Auth and Azure Functions
auth use, not something specific to this one app.

**One config decision worth being explicit about, since it changes who can
actually get in:** an Entra ID app registration defaults to accepting
accounts only from *this* tenant's organization. For "anyone who signs in"
to mean what it says for a public visitor — not "anyone with a Vodafone
work account" — the app registration needs **"Accounts in any organizational
directory and personal Microsoft accounts"** (multi-tenant + personal MSA).
That's the intended setting here; flagged explicitly because the narrower
default is easy to leave in place by accident and would quietly turn "public
app" into "internal tool."

**What the app gets after a successful sign-in:** Container Apps injects
`X-MS-CLIENT-PRINCIPAL*` headers into the request (base64-encoded principal
ID, display name, identity provider). Two things this unlocks, neither built
yet:

- Showing "signed in as ..." in the Streamlit UI — cheap, mostly cosmetic.
- **Per-user rate limiting**, keyed off the principal ID instead of IP —
  this directly closes the "abuse / cost control" item that's been open
  in §5 since the first pass of this doc. Auth turns that from "guess who's
  hammering the app" into "count requests per identity," which is a much
  more tractable problem.

**Not addressed by this:** Entra ID sign-in proves *someone signed in*, not
that they're a Muslim seeking Fiqh guidance in good faith — it's an abuse
throttle, not a content moderation layer. That distinction is worth keeping
in mind if abuse turns out to look like "many requests from one authenticated
account" rather than "many anonymous requests," since the mitigations differ.

## 4b. Rate limiting — Azure API Management

**Chosen 2026-09-17:** put Azure API Management in front of the Container
Apps environment as the app's one public entry point, using its
`rate-limit-by-key` policy rather than an app-level counter. This is the
more idiomatic Azure pattern for gateway-level throttling and adds a
genuinely useful second learning surface (API gateway policies) alongside
Container Apps' own primitives — but it changes the architecture in two
ways worth being explicit about, and creates one real open question.

**What changes:** per the updated diagram in §4, both `ai-fiqh-web` and
`ai-fiqh-ollama` move to **internal-only ingress**. APIM becomes the sole
public hostname, and needs a private path into the Container Apps
environment to reach them — i.e. the Container Apps environment gets
**VNet integration**, and APIM needs a tier that can either be deployed into
that VNet or peer with it. That's a real infrastructure decision, not a
detail: **the Consumption tier** (pay-per-call, no fixed hourly cost — the
tier that best matches the cost-minimization goal elsewhere in this doc)
has historically had limited or no VNet-integration support compared to the
Developer/Standard/Premium tiers, which do support it but carry a fixed
hourly cost whether or not the app is getting traffic — the same "always-on
cost" trade this doc has been trying to avoid for compute. **Not yet
resolved: which APIM tier actually satisfies both "reaches a privately
networked backend" and "doesn't reintroduce a fixed always-on cost."** Needs
a current tier-capability check before committing — API Management's tier
lineup has changed over time (newer "v2" tiers exist specifically to bring
VNet support to a cheaper tier than Premium), so verify against what's
actually offered now rather than trusting a recalled tier table.

**The real open question: what key does `rate-limit-by-key` actually use
here?** APIM's rate-limiting policies are built around request-level keys
that are available *before* any backend is involved — client IP, an APIM
subscription key, or a claim pulled out of a bearer token APIM validates
itself (`validate-jwt` + `rate-limit-by-key` on a token claim). That fits an
API client that already holds a token. It fits less cleanly here because:

- The identity this app cares about is resolved by **Container Apps'
  interactive OIDC login (Easy Auth)** — a browser redirect-and-cookie
  session — which happens *behind* APIM in the request path, not before it.
  On the first request from a not-yet-signed-in browser, APIM has no
  identity to key on yet; only Container Apps does, after the login
  redirect completes.
- Streamlit's UI traffic (WebSocket-based, not discrete REST calls) is also
  a less common APIM workload than the stateless API calls its policies are
  usually written for. Supported, per Azure's docs on WebSocket APIs
  through APIM, but worth confirming hands-on rather than assuming it
  behaves like a normal HTTP API once through the gateway.

**Not resolved yet — needs a spike, flagged the same way the Ollama
cold-start question was before it got measured:** two candidate shapes,
neither built:

1. **Two-tier limiting.** APIM does a coarse, identity-blind limit (by
   client IP or a fixed subscription key) in front of everything — cheap,
   works today, protects the login page and the whole app from raw floods.
   True per-user fairness (the thing auth in §4a was meant to unlock) falls
   back to a small app-level counter keyed on the
   `X-MS-CLIENT-PRINCIPAL-ID` header Container Apps injects after login —
   i.e. APIM handles volumetric protection, the app handles per-user
   fairness. Less "pure APIM," more realistic given the login flow.
2. **Push more of the app behind a token-bearing API surface.** If a small
   REST endpoint ever sits alongside the Streamlit UI (not planned yet, but
   plausible as a follow-on learning step), *that* surface is where
   APIM's `validate-jwt` + `rate-limit-by-key` work in their intended,
   textbook form — a genuinely cleaner APIM lesson, just not one that
   covers the interactive UI itself.

Leaning toward (1) as the pragmatic default for the UI, with (2) as a
plausible future exercise rather than a requirement — not committed either
way. This is the item most likely to change shape once actually built.

### Considerations

- **Identity exists, but not in a form APIM reads natively.** Container
  Apps Easy Auth is cookie/session-based — it decodes the session into
  `X-MS-CLIENT-PRINCIPAL-*` headers for the app. APIM's `validate-jwt` /
  `rate-limit-by-key` expect a bearer token on `Authorization`. The two
  don't interoperate out of the box; bridging them needs custom policy work
  (e.g. calling `/.auth/me`, or forwarding a token APIM can validate).
- **VNet integration is not about public vs. private access to the app —
  the app stays public, via APIM's own endpoint.** It exists solely to
  remove the Container App's *own* public hostname, so APIM can't be
  bypassed. Without it, a client could call the app directly and skip every
  rate-limiting policy APIM enforces.

- **Mechanism varies by tier.** Classic "External" VNet mode
  (Developer/Premium) injects APIM's gateway into a subnet of a VNet — it
  must sit in the same VNet as the Container Apps environment, or a peered
  one — while its public IP/hostname stays internet-reachable for inbound
  traffic. The newer Standard v2 tier instead uses outbound-only VNet
  integration (comparable to App Service regional VNet integration): APIM's
  frontend stays outside the VNet, but outbound calls get a private path
  into a specified VNet/subnet, without full injection. Either way: public
  in, private out.
- **What makes the backend private.** `ai-fiqh-web`'s ingress must be set
  to Internal, restricting it to resolution/reachability from within the
  Container Apps environment's VNet only. APIM then reaches that internal
  hostname via the private network path (subnet injection, or the
  environment's private DNS zone, depending on mode) rather than over the
  public internet.

## 4c. Cold start on the Ollama app — resolved: bake the model into the image

Scale-to-zero is cheap but not free in latency. A cold start on
`ai-fiqh-ollama` means: container starts, Ollama server starts, then the
model weights (~8 GB for `gemma4:12b` Q4_K_M) have to become available to
it. Pulling the model fresh from Ollama's registry on every cold start would
be minutes, not seconds — bad for a "fallback so the user still gets an
answer" path.

**Chosen 2026-09-17: bake the model into the container image** (`ollama
pull` at image build time, `COPY` the resulting blob store in) rather than
mounting it from an Azure Files share. One artifact, no separate storage
resource to provision or secure; the model is local the instant the
container starts, no network pull at cold-start time. Trade-off accepted:
a materially bigger image in ACR (~8 GB+), which means slower image
*pushes/pulls on deploy*, even though it removes the *runtime* cold-start
pull.

**Measured locally 2026-09-17** (M3 Pro, model baked into the image, no
network fetch — not Azure hardware, and doesn't include ACR image pull or
the dedicated workload profile's own node cold start, see below): first
inference call after container start took **60.3s total, 56.8s of which
was model load alone**; a second call against the already-loaded model
took **5.9s**. So the one-time cost of a cold start is model *load*, not
network I/O — and it's real, not negligible.

**This also surfaced a bigger problem than cold-start latency: the
Consumption plan's 8 GiB ceiling isn't just tight, it's insufficient.**
Local testing reproducibly OOM-killed `gemma4:12b` at 8 GiB (both with an
unset context window and with the tracker's own recommended 16,384-token
ceiling applied) — Docker Desktop's VM had to be raised to 16 GiB before a
single inference call succeeded. **Resolved 2026-09-17: `ai-fiqh-ollama`
moves to a Dedicated workload profile** (`infra/modules/container-apps.bicep`),
sized to 16 GiB, with `minimumCount: 0` on the profile itself so the
underlying dedicated node can still deallocate when idle — the closest a
Dedicated profile gets to Consumption's scale-to-zero economics. This is a
firmer, more consequential change than the doc previously treated this
section as needing: it's not that scale-to-zero *might* need revisiting
toward `min replicas: 1`, it's that the compute tier itself had to change
first, before the scale-to-zero question could even be tested honestly.

**Still not measured: end-to-end cold start on the actual Azure Dedicated
profile.** The 60.3s figure is model load only, on different hardware, with
the container already running — it doesn't include pulling the (now larger,
16 GiB-headroom) image from ACR, scheduling onto the dedicated profile, or
that profile's own node-level cold start when scaling up from
`minimumCount: 0`. Real end-to-end latency after a genuine scale-from-zero
in Azure could be meaningfully higher than 60s. Whether that's acceptable
for a fallback firing on ~2/177 chunks, or whether `minReplicas: 1` on the
dedicated profile is worth its cost to avoid, is the concrete decision this
number was gathered to inform — not yet made.

---

## 5. Things this still needs, regardless of the details above

- **Real cost estimate**, not the rough VM-era number in §2 — Container
  Apps consumption pricing (vCPU-seconds + GiB-seconds, with a monthly free
  grant) needs running through the Azure Pricing Calculator for the actual
  shape (web app `min replicas: 1` at some size, Ollama app scale-to-zero
  with occasional cold starts).
- **Abuse / cost control** — direction now set by §4a/§4b (APIM in front,
  per-user fairness likely still needing an app-level counter keyed on the
  Entra ID principal — see §4b's open spike), but neither layer is built
  yet, and billing alerts on Azure OpenAI / Voyage / Anthropic are still
  needed regardless of how the limiter shakes out.
- **APIM tier + VNet integration** (§4b) — needs a current tier-capability
  check before committing to one; the cost-minimizing tier and the
  private-networking requirement may not be the same tier.
- **CI/CD pipeline shape** — not sketched yet: build-on-push vs. build-on-
  tag, whether staging/prod are separate Container Apps revisions or
  separate environments entirely.
- **The two `violence`-filtered chunks still have no golden-set coverage**
  (tracker, 2026-09-10) — still worth closing before the fallback path gets
  exercised by real public traffic for the first time.
- **Domain + TLS** — Container Apps gives a default `*.azurecontainerapps.io`
  hostname with managed TLS out of the box; a custom domain is optional
  polish, not a blocker.

---

## 6. Still open

- **Measure end-to-end cold start on the actual Azure Dedicated profile**
  (§4c) — local model-load time is measured (56.8s), but ACR image pull,
  scheduling, and the profile's own node-level cold start from
  `minimumCount: 0` are not. That full number is what decides whether
  `min replicas: 0` on `ai-fiqh-ollama` is still worth it.
- Real Container Apps cost estimate — not yet run through the pricing
  calculator.
- **APIM tier that satisfies both cost-minimization and VNet integration**
  (§4b) — needs a current capability check, not yet chosen.
- **Confirm the actual Dedicated workload profile SKU** (§4c,
  `infra/modules/container-apps.bicep`'s `dedicatedProfileWorkloadType`,
  currently a placeholder `'E4'`) is genuinely offered in the target region
  before deploying: `az containerapp env workload-profile
  list-supported --location <region>`.
- **How `rate-limit-by-key` reconciles with Container Apps' interactive
  Easy Auth login** (§4b) — the main unresolved design question this pass
  created; leaning toward APIM doing coarse/identity-blind limiting with an
  app-level per-principal counter behind it, not committed.
- **Entra ID app registration must be set multi-tenant + personal Microsoft
  accounts** (§4a) — decided, not yet built; easy to get wrong by leaving
  the single-tenant default in place.
- CI/CD pipeline shape — not sketched.
- Which subscription/resource group this lands in (§3) — worth being
  deliberate about, not urgent.
