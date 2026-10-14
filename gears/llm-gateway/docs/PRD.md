# PRD — LLM Gateway


<!-- toc -->

- [1. Overview](#1-overview)
  - [1.1 Purpose](#11-purpose)
  - [1.2 Background / Problem Statement](#12-background--problem-statement)
  - [1.3 Goals (Business Outcomes)](#13-goals-business-outcomes)
  - [1.4 Glossary](#14-glossary)
- [2. Actors](#2-actors)
  - [2.1 Human Actors](#21-human-actors)
  - [2.2 System Actors](#22-system-actors)
- [3. Operational Concept & Environment](#3-operational-concept--environment)
  - [3.1 Gear-Specific Environment Constraints](#31-gear-specific-environment-constraints)
  - [3.2 Data Ownership & Classification](#32-data-ownership--classification)
- [4. Scope](#4-scope)
  - [4.1 In Scope](#41-in-scope)
  - [4.2 Out of Scope](#42-out-of-scope)
- [5. Functional Requirements](#5-functional-requirements)
  - [P1 — Core](#p1--core)
  - [P2 — Reliability, Governance & Media](#p2--reliability-governance--media)
  - [P3 — Additional Capabilities](#p3--additional-capabilities)
  - [P4 — Enterprise](#p4--enterprise)
- [6. Non-Functional Requirements](#6-non-functional-requirements)
  - [Scalability](#scalability)
  - [Data Retention](#data-retention)
  - [Compatibility](#compatibility)
  - [NFR Exclusions](#nfr-exclusions)
  - [Recovery](#recovery)
  - [Observability](#observability)
- [7. Public Library Interfaces](#7-public-library-interfaces)
- [8. Use Cases](#8-use-cases)
- [9. Acceptance Criteria](#9-acceptance-criteria)
- [10. Dependencies](#10-dependencies)
- [11. Assumptions](#11-assumptions)
- [12. Risks](#12-risks)
- [13. Open Questions](#13-open-questions)
- [14. Traceability](#14-traceability)

<!-- /toc -->

## 1. Overview

### 1.1 Purpose

LLM Gateway provides unified access to multiple LLM providers. Consumers interact with a single interface regardless of underlying provider. Gateway normalizes requests and responses but does not execute tools or interpret content — this is consumer responsibility.

LLM Gateway is the central integration point between platform consumers and external AI providers. It abstracts provider differences — request formats, authentication, error handling, rate limits — behind a unified API. Consumers send requests in a normalized format; Gateway translates them to provider-specific calls and normalizes responses back.

Provider-specific logic is isolated in **provider plugins**. Each plugin implements a common plugin trait that covers the superset of provider capabilities needed by the Gateway API: it converts normalized requests into provider HTTP requests, parses provider responses back into the normalized format, and computes the call cost in AI credits from the pricing in the model's provider settings. Plugins do not use third-party provider SDK libraries. The Gateway core owns cross-cutting concerns: model resolution, capability checks, credential retrieval, quota, usage reporting, and timeouts.

The first phase (P1) covers synchronous and streaming chat completion and embeddings, including image and file input passed as base64 data or external URLs, reasoning, tool calling, web search, and structured output. Later phases add media generation (images, audio, video), FileStorage-backed media, async jobs, batch processing, and realtime audio. All provider calls go through the Outbound API Gateway (OAGW). The Gateway obtains provider API keys from CredStore and passes them to the provider plugin, which sets them on the outbound request.

Gateway is stateless for request processing — it does not store conversation history or execute tools, and consumers provide full context with each request. Server-side conversation state at the provider (`previous_response_id`, `store=true`) is not used. From P2, Gateway persists async and batch job state (internal-to-provider job ID mappings, job results) to support long-running operations that can take up to 24 hours. Gateway guarantees at-least-once delivery of usage records to the Usage Collector gear.

**Target Users**:
- **Platform Developers** — build AI-powered features using Gateway API
- **External API Consumers** — third-party developers accessing AI capabilities via public API

### 1.2 Background / Problem Statement

Platform consumers need access to AI capabilities from multiple LLM providers — OpenAI, Anthropic, Google, and others. Each provider has its own API format, authentication mechanism, error semantics, and rate limiting behavior. Without a unified abstraction, every consumer must implement provider-specific integration logic, handle failover independently, and manage credentials directly. This fragments the codebase, increases the surface area for security issues, and makes it difficult to enforce consistent governance policies across the platform.

The current state requires each consuming gear to negotiate provider differences at the application layer: translating request formats, normalizing error responses, and implementing retry logic per provider. Usage tracking and budget enforcement are ad-hoc or absent, making it impossible to provide tenant-level cost visibility or enforce spending limits.

LLM Gateway addresses these problems by providing a single integration point that abstracts provider differences behind a unified API. It centralizes governance — budget enforcement, rate limiting, usage tracking, and audit logging — at the tenant level.

### 1.3 Goals (Business Outcomes)

**Success Criteria** (required at GA, baseline: new gear — no prior data; measured over 7-day rolling window after initial 30-day burn-in period):
- Gateway overhead < 50ms P99 at up to 1 000 concurrent requests (excluding provider latency)
- Availability ≥ 99.9% measured monthly (Gateway infrastructure only — excludes provider outages)
- Expected steady-state throughput: up to 500 requests/second; peak: up to 2 000 requests/second
- Maintenance windows follow platform-defined schedule; Gateway supports rolling restarts with zero-downtime deployments

**Post-GA Growth Targets** (6 months after GA):
- Steady-state throughput: up to 1 000 requests/second; peak: up to 5 000 requests/second
- Maintain < 50ms P99 overhead at increased load
- Support up to 50 000 async/batch jobs/day per tenant

**Capacity Planning Inputs**:
- Expected async/batch job volume: up to 10 000 jobs/day per tenant at steady state
- DB row growth: up to 50 000 rows/day across all tenants (job records, from P2); bounded by retention cleanup
- Storage per async result: up to 1 MB (text responses); up to 100 MB (media generation results stored in FileStorage, not DB)

**Capabilities** (P1):
- Text generation (chat completion), synchronous and streaming
- Embeddings generation
- Image and file input (base64 data or external URLs)
- Reasoning configuration and reasoning output
- Tool/function calling, Gateway web search tool, provider-specific tools
- Structured output with schema validation
- Usage reporting with cost in AI credits
- Per-tenant AI credit quota enforcement

**Capabilities** (P2+): media generation (images, audio, video), video and audio understanding, FileStorage-backed media, async jobs, batch processing, realtime audio.

### 1.4 Glossary

| Term | Definition |
|------|------------|
| OAGW | Outbound API Gateway — executes external API calls (`proxy_request`) to registered upstreams and routes, applies circuit breaking; may optionally handle upstream authentication |
| TTFT | Time-to-first-token — latency until first response chunk |
| GTS | Global Types System — JSON Schema-based type definitions |
| FileStorage | Platform gear for storing and retrieving binary files (images, audio, video, documents) |
| Model Registry | Platform gear that resolves model identifiers to model info per tenant and validates model settings against the provider settings schema |
| Model Info | Model Registry record forwarded as-is to the provider plugin: provider GTS type, provider model name, `base_url`, secret reference, generic default settings, and provider-specific settings (including pricing) |
| Provider Plugin | Toolkit gear implementing the provider plugin trait for one provider (or one provider API family, e.g. OpenAI-compatible). Owns request/response translation, transport via OAGW, and cost calculation |
| Provider Settings Schema | GTS schema, registered by a provider plugin, that defines the format of provider-specific model settings, including pricing |
| CredStore | Platform service storing provider API keys; Gateway fetches the key referenced by the model's secret reference |
| Type Registry | Platform gear storing GTS schemas and instances: provider plugin instances, provider settings schemas, tool schemas |
| AI credits | Platform billing unit. Provider plugins convert provider usage into AI credits using the model's pricing |
| Open Responses | Responses-style request/response contract used by the Gateway SDK (`CreateResponseBody`, `ResponseResource`, streaming events) |

## 2. Actors

### 2.1 Human Actors

No dedicated human actors. All interactions with LLM Gateway are mediated through the Consumer system actor (platform gears, external API clients). Platform administrators who configure models and providers do so through Model Registry, not through LLM Gateway directly.

### 2.2 System Actors

#### Consumer

**ID**: `cpt-cf-llm-gateway-actor-consumer`

- **Role**: Sends requests to the Gateway.
- **Needs**: Provider-agnostic API for all AI modalities; normalized responses; async job management; usage visibility.
- **Direction**: inbound
- **Data exchanged**: Chat completion requests (messages, model, parameters), tool definitions, async job commands; receives normalized responses, streaming chunks, job status/results.
- **Availability**: Consumer availability is not a Gateway concern — Gateway responds to incoming requests as they arrive.

#### Provider

**ID**: `cpt-cf-llm-gateway-actor-provider`

- **Role**: External AI service that processes requests. Accessed by provider plugins via Outbound API Gateway. Includes cloud providers (OpenAI, Anthropic, Google, …) and self-hosted or OpenAI-compatible endpoints (Ollama, vLLM, …) reached through a model-specific `base_url`.
- **Direction**: outbound
- **Data exchanged**: Provider-formatted requests (prompts, parameters, tool schemas, API key in request headers); receives completions, embeddings, generated media, tool calls, reasoning output, web search results, usage metrics.
- **Availability**: Provider unavailability returns a provider-unavailable error to the consumer. OAGW circuit breaking protects against cascading failures.

#### Provider Plugin

**ID**: `cpt-cf-llm-gateway-actor-provider-plugin`

- **Role**: In-process extension (toolkit gear) that isolates one provider behind the common provider plugin trait. Translates normalized requests to provider format and back, applies model settings, sets the provider API key on the request, calls the provider via OAGW, and computes call cost in AI credits.
- **Direction**: bidirectional (invoked by Gateway core; calls OAGW)
- **Data exchanged**: Inbound: normalized request, model info (as received from Model Registry), optional provider API key. Outbound: normalized response or stream of chunks, generic usage, provider-specific raw usage, cost in AI credits.
- **Availability**: Plugins are compiled into the same binary and registered at startup. If no plugin is registered for the model's provider type, Gateway returns a provider-not-supported error.

#### OAGW

**ID**: `cpt-cf-llm-gateway-actor-oagw`

- **Role**: Outbound API Gateway — the only path from provider plugins to providers. Executes provider HTTP calls (`proxy_request`) to registered upstreams and routes and applies circuit breaking; may handle upstream authentication for models without a secret reference.
- **Direction**: outbound (called by provider plugins)
- **Data exchanged**: Provider-formatted HTTP requests (OAGW alias and path built from the model's `base_url` and the provider API path); receives provider HTTP responses and streams.
- **Availability**: If OAGW is unavailable, the provider cannot be reached and Gateway returns a provider-unavailable error. An open circuit for a provider is reported the same way.

#### CredStore

**ID**: `cpt-cf-llm-gateway-actor-credstore`

- **Role**: Stores provider API keys.
- **Direction**: outbound
- **Data exchanged**: Secret lookup by the model's secret reference; receives the provider API key.
- **Availability**: If CredStore is unavailable and the model requires a key, Gateway returns a credentials-unavailable error. Models without a secret reference (auth handled by OAGW, local provider) do not require CredStore.

#### Type Registry

**ID**: `cpt-cf-llm-gateway-actor-type-registry`

- **Role**: Stores GTS types and instances used by the Gateway: provider plugin instances, provider settings schemas, and tool schemas referenced in requests.
- **Direction**: outbound
- **Data exchanged**: Plugin and settings schema registration at startup; schema lookups for tool definitions.
- **Availability**: Required at startup for plugin registration. If a referenced tool schema cannot be resolved, Gateway returns a schema-not-found error.

#### Usage Collector

**ID**: `cpt-cf-llm-gateway-actor-usage-collector`

- **Role**: Separate platform service (`gears/system/usage-collector`) that receives AI credit consumption reports.
- **Direction**: outbound
- **Data exchanged**: AI credit consumption records (credit amount, attribution: tenant, user, model; completeness flag for interrupted streams). The provider plugin converts provider usage into AI credits using the pricing in the model's provider settings (obtained from Model Registry). Gateway does not report raw token counts or cost estimates to Usage Collector — only AI credit amounts.
- **Availability**: Usage record delivery is guaranteed at-least-once. Usage Collector unavailability does not block request processing.

Note: Gateway may report more AI credits than the tenant's allocated quota because token consumption cannot be predicted before the request completes. This is expected behavior — the quota check is a best-effort gate, not a hard limit.

#### Quota Enforcement

**ID**: `cpt-cf-llm-gateway-actor-quota-enforcement`

- **Role**: Separate platform service (`gears/system/quota-enforcement`) that checks available AI credit quotas before request execution.
- **Direction**: outbound
- **Data exchanged**: Quota check requests (tenant context); receives quota status (available/exceeded).
- **Availability**: If Quota Enforcement is unavailable, Gateway rejects the request with a quota-check-unavailable error. Gateway does not bypass quota checks silently.

#### Audit Gear

**ID**: `cpt-cf-llm-gateway-actor-audit-gear`

- **Role**: Compliance event logging.
- **Direction**: outbound
- **Data exchanged**: Audit events (request started, completed, failed) with tenant/user/model attribution.
- **Availability**: Audit Gear unavailability does not block request processing. Audit events are delivered best-effort — Gateway logs a warning if delivery fails but does not reject the consumer request.

#### Model Registry

**ID**: `cpt-cf-llm-gateway-actor-model-registry`

- **Role**: Resolves model identifiers to model info per tenant. Gateway queries Model Registry on every request to determine the provider plugin, endpoint, and settings. Model Registry also validates provider-specific model settings against the provider settings schema registered by the plugin in Type Registry.
- **Direction**: outbound
- **Data exchanged**: Model resolution requests (model identifier, tenant context); receives model info: provider GTS type, provider model name, `base_url`, secret reference, capabilities, generic default settings, provider-specific settings including pricing.
- **Availability**: If Model Registry is unavailable, Gateway returns a model-resolution-unavailable error. Gateway cannot route requests without model resolution.

#### FileStorage

**ID**: `cpt-cf-llm-gateway-actor-filestorage`

- **Role** (P2): Platform gear that stores and serves media files. Gateway resolves FileStorage URLs in media inputs and stores generated media (images, audio, video) in it.
- **Direction**: outbound
- **Data exchanged**: Media reads by FileStorage URL (bytes prefetched when the provider plugin requires them); uploads of generated media, receiving a retrievable URL.
- **Availability**: Required only for requests that reference FileStorage URLs or generate media. If FileStorage is unavailable, such requests fail with a storage error; other requests are unaffected.

## 3. Operational Concept & Environment

### 3.1 Gear-Specific Environment Constraints

- Reference deployment: LLM Gateway, its provider plugins, Model Registry, and Type Registry (optionally the AuthZ resolver) are compiled into one binary. Model Registry owns its database.
- From P2, requires persistent storage for async/batch job state; Gateway does not persist usage records
- All external provider calls route through Outbound API Gateway (OAGW): provider plugins call the OAGW SDK `proxy_request` function. The OAGW alias and path are built from the model's `base_url` (defined in Model Registry) plus the provider API path. Gateway never calls providers directly.
- OAGW upstreams and routes are configured outside the Gateway by default. Provider plugins can declare the routes they use, but deciding which base URLs may be called is the deployment's responsibility. Automatic provisioning of upstreams by Model Registry or plugins is an open question (see § 13). Deployments without a full OAGW can substitute a plain HTTP client behind the same SDK interface.
- P1: image and file inputs are accepted as base64 data or external URLs and forwarded to the provider. From P2: media files are stored and retrieved via the FileStorage gear.

### 3.2 Data Ownership & Classification

Gateway acts as a processor — consumers own request/response data; Gateway owns operational metadata (job state, usage records sent to Usage Collector). Provider response data in persisted job records is held on behalf of the consumer and subject to retention-based cleanup.

| Data Category | Examples | Persistence | Sensitivity | Owner |
|---------------|----------|-------------|-------------|-------|
| Transient request/response | Prompts, completions, embeddings | Not persisted — in-memory only | Consumer-defined (may contain PII) | Consumer |
| Provider credentials | Provider API keys fetched from CredStore | Not persisted — in-memory for the duration of the call; never logged | Secret | Tenant / platform (stored in CredStore) |
| Persisted job records | Async job state, batch job ID mappings, job results | Retention-bound (10 min / 48 h) | Potentially sensitive — treated as PII-capable | Consumer (held by Gateway) |
| Usage telemetry | AI credit amounts, tenant/user/model attribution | Not persisted by Gateway — sent to Usage Collector | Personal data (pseudonymous) — user ID links usage to an individual | Gateway |

## 4. Scope

### 4.1 In Scope

**P1**:
- Unified LLM API aligned with the Open Responses contract (create response, sync and streaming; embeddings)
- Provider abstraction via provider plugins (OpenAI, Anthropic, Google, OpenAI-compatible and self-hosted endpoints, and others)
- Per-model endpoints: `base_url` defined in Model Registry, including self-hosted and OpenAI-compatible endpoints
- Provider API key retrieval from CredStore and hand-off to the provider plugin
- Image and file input as base64 data or external URLs
- Reasoning configuration and reasoning output, including multi-turn reasoning continuity without server-side state
- Tool/function calling pass-through (consumer executes tools), including tools defined by GTS reference or inline GTS schema
- Gateway web search tool (`cf:web_search`), forwarded to providers that support it natively
- Provider-specific tools, namespaced by provider (e.g. `openai:shell`)
- Structured output with schema validation
- Generic generation parameters (max tokens, temperature, top_p, seed, …), generic cache-control parameters, and forwarding of provider-specific parameters
- Usage in responses: cost in AI credits, generic token usage, provider-specific raw usage
- AI credit usage tracking with guaranteed at-least-once delivery (cost computed by the provider plugin from model pricing)
- Per-tenant AI credit quota enforcement via Quota Enforcement

**P2+**:
- Media generation (images, audio, video) and video/audio understanding
- FileStorage-backed media input and output
- Async job execution with durably persisted job state and results
- Batch processing with durably persisted job ID mappings
- Timeout enforcement
- Rate limiting
- Realtime audio

### 4.2 Out of Scope

- Conversation state storage — consumers provide full context per request
- Provider-side conversation state: `previous_response_id`, `store=true`
- Tool execution — Gateway returns tool calls for consumer to execute. This includes web search: Gateway does not run searches itself; `cf:web_search` is executed by providers that support it
- Prompt engineering or prompt optimization
- Model training, fine-tuning, or model hosting
- Direct provider access bypassing OAGW
- Use of third-party provider SDK libraries inside provider plugins
- Gateway-side content caching or response deduplication (provider-side prompt caching controls are in scope)

## 5. Functional Requirements

### P1 — Core

#### Chat Completion

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-chat-completion-v1`

The system **MUST** accept a chat completion request with input items and model identifier, route it to the provider plugin for the resolved model, and return a normalized response with usage. Request and response formats **MUST** follow the Open Responses contract defined in the Gateway SDK. The system **MUST NOT** rely on provider-side conversation state (`previous_response_id`, `store=true`).

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`, `cpt-cf-llm-gateway-actor-provider`

#### Streaming Chat Completion

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-streaming-v1`

The system **MUST** support streaming mode for chat completions, delivering response chunks in a normalized event format as they arrive from the provider. When the consumer disconnects, the system **MUST** cancel the upstream provider request. Usage for an interrupted stream cannot be calculated reliably: the system **MUST** report it explicitly marked as incomplete; it **MAY** include an estimate, labeled as such.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`, `cpt-cf-llm-gateway-actor-provider`

#### Embeddings Generation

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-embeddings-v1`

The system **MUST** accept one or more text inputs and return vector embeddings in a normalized format with usage metrics.

The system **MUST** accept the following generic embedding parameters and map them to the provider's equivalents:

- `dimensions` — output vector size. Consumers depend on it because it is fixed by their vector store
- `input_type` — `query` or `document` (e.g. Google `task_type`, Voyage `input_type`); affects retrieval quality
- `encoding_format` — `float` or `base64`

Provider-specific embedding parameters with no generic equivalent are forwarded as defined in `cpt-cf-llm-gateway-fr-generation-parameters-v1`.

Unsupported parameters are handled per `cpt-cf-llm-gateway-fr-generation-parameters-v1`, with these exceptions:

- `dimensions` **MUST NOT** be dropped, even when `drop_unsupported_params` is set: vectors of the wrong size break the consumer's vector store. If the model cannot produce the requested size, the request is rejected with `unsupported_parameter`. A model with a fixed size accepts `dimensions` equal to that size.
- `encoding_format` is supported for every model: if the provider does not return the requested format, the system converts the vectors.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`, `cpt-cf-llm-gateway-actor-provider`

#### Provider Plugins

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-provider-plugins-v1`

The system **MUST** isolate provider-specific logic in provider plugins implemented with the ModKit/toolkit plugin system. Each plugin **MUST**:
- on init, register its plugin GTS instance and its provider settings schema in Type Registry;
- implement the provider plugin trait: create response (sync), create response (streaming), and create embedding;
- convert normalized requests into provider HTTP requests and provider responses into the normalized format, without third-party provider SDK libraries;
- apply generic and provider-specific settings from model info;
- send all HTTP requests through OAGW (`proxy_request`);
- compute the call cost in AI credits from the pricing in the model's provider settings.

The Gateway **MUST** select the plugin by the provider GTS type in the resolved model info and forward model info to the plugin as received from Model Registry. Model Registry **MUST** be able to validate provider-specific model settings against the plugin's settings schema.

**Actors**: `cpt-cf-llm-gateway-actor-provider-plugin`, `cpt-cf-llm-gateway-actor-type-registry`, `cpt-cf-llm-gateway-actor-model-registry`, `cpt-cf-llm-gateway-actor-oagw`

#### Custom Model Endpoints

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-custom-endpoints-v1`

The system **MUST** support models with a custom `base_url` (self-hosted or OpenAI-compatible endpoints such as Ollama or vLLM). The model's `base_url` is defined in Model Registry and comes with the model info. The provider plugin **MUST** build the OAGW alias and path from that `base_url` and the provider API path.

**Actors**: `cpt-cf-llm-gateway-actor-provider-plugin`, `cpt-cf-llm-gateway-actor-model-registry`, `cpt-cf-llm-gateway-actor-oagw`

#### Provider Credentials

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-provider-credentials-v1`

When model info contains a secret reference, the system **MUST** fetch the provider API key from CredStore and pass it to the provider plugin, which sets it in the provider request headers. The key is optional: models without a secret reference (auth handled by OAGW, local provider) **MUST** be callable without a key. The system **MUST NOT** persist or log provider API keys.

**Actors**: `cpt-cf-llm-gateway-actor-credstore`, `cpt-cf-llm-gateway-actor-provider-plugin`

#### Image and File Input

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-vision-v1`

The system **MUST** accept messages with images and files (documents) as base64 data or external URLs, route them to a model that supports the content type, and return the result with usage metrics. Inputs **MUST** be forwarded to the provider as URLs where the provider accepts them. FileStorage URLs are covered by `cpt-cf-llm-gateway-fr-filestorage-media-v1` (P2).

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`

#### Document Understanding

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-document-understanding-v1`

The system **MUST** accept messages with a document as base64 data or external URL, route to a capable model, and return the analysis with usage metrics. FileStorage document references are added in P2.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`

#### Reasoning

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-reasoning-v1`

The system **MUST** accept a normalized reasoning configuration (enable/disable, effort, token budget where supported) and map it to the provider's native reasoning parameters. The system **MUST** return reasoning output (reasoning summaries or reasoning content, and reasoning token usage) in a normalized format. To support multi-turn reasoning without provider-side state, the system **MUST** return the opaque reasoning artifacts providers require for continuation (signatures, encrypted reasoning content) and accept them back in subsequent requests.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`

#### Tool/Function Calling

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-tool-calling-v1`

The system **MUST** accept requests with user-defined function tools (unified OpenAI-like format, GTS reference, or inline GTS schema), resolve schema references, and forward them to the provider. The system **MUST** return tool calls in a unified format for the consumer to execute. The system **MUST NOT** execute tools — this is consumer responsibility.

The system **MUST** accept tool selection controls and map them to the provider's equivalents:

- `tool_choice`: `none` (model must not call tools), `auto` (model decides), `required` (model must call at least one tool), or a specific function by name (model must call that function)
- `parallel_tool_calls`: whether the model may return several tool calls in one response

A `tool_choice` that names a function not defined in the request **MUST** be rejected with a validation error. A `tool_choice` value or `parallel_tool_calls` setting the model does not support is handled as an unsupported generic parameter (`cpt-cf-llm-gateway-fr-generation-parameters-v1`): rejected with `unsupported_parameter` by default, or dropped and listed in `ignored_parameters` when `drop_unsupported_params` is set.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`, `cpt-cf-llm-gateway-actor-type-registry`

#### Web Search Tool

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-web-search-v1`

The system **MUST** support a provider-agnostic web search tool (`cf:web_search`). The provider plugin **MUST** map it to the provider's native web search tool when the provider supports one; otherwise the request is rejected by the capability check. The Gateway does not run searches itself. Web search results and citations **MUST** be returned in a normalized format.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`

#### Provider-Specific Tools

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-provider-tools-v1`

The system **MUST** accept provider-specific tools namespaced with the provider prefix (e.g. `openai:shell`) and forward them to the matching provider plugin. Results of provider-executed tool calls **MUST** be returned in the response, preserving provider-specific output items.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`

#### Structured Output

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-structured-output-v1`

The system **MUST** accept a JSON schema with the request and validate the provider response against it, returning either the validated response or a validation error with details.

The system **MUST** also support JSON mode without a schema (`json_object`): the plugin maps it to the provider's JSON mode, and the system validates that the response is well-formed JSON. If the model supports neither JSON mode nor schema-constrained output, the request is rejected with `capability_not_supported`.

Providers accept different subsets of JSON Schema (e.g. Google requires `$ref` to be inlined and does not accept `additionalProperties`). The provider plugin **MUST** adapt the schema to the provider's subset when this can be done without loosening the final result — for example, inlining `$ref` or removing keywords the provider does not accept. Removed constraints remain enforced, because the system validates the response against the original schema, not the adapted one. If the schema cannot be expressed in the provider's subset (e.g. recursive `$ref` for a provider that requires inlining), the system **MUST** reject the request with an `unsupported_schema` error naming the construct, before calling the provider.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`

#### Generation Parameters and Provider-Specific Parameters

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-generation-parameters-v1`

The system **MUST** accept generic generation parameters (max output tokens, temperature, top_p, seed, penalties, etc.) and generic cache-control parameters, and map them to the provider's equivalents. The system **MUST** support forwarding provider-specific parameters that have no generic equivalent; which parameters may be forwarded is governed by the model configuration in Model Registry.

The provider plugin **MUST** report which generic parameters the resolved model supports (support can differ between models of the same provider, e.g. reasoning models that reject `temperature`). When a request contains a generic generation parameter the model does not support, the system **MUST**, before calling the provider:

- by default, reject the request with an `unsupported_parameter` error that names the parameter;
- if the request sets `drop_unsupported_params`, drop the parameter and proceed.

Cache-control parameters are hints: when the provider has no equivalent, the system **MUST** drop them without an error, regardless of `drop_unsupported_params`. Every dropped parameter **MUST** be listed in the response (`ignored_parameters`), so dropping is never silent.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`, `cpt-cf-llm-gateway-actor-model-registry`

#### Usage Tracking

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-usage-tracking-v1`

The system **MUST** return usage with every response: cost in AI credits, generic usage (input, output, cached, and reasoning tokens), and the provider-specific raw usage structure. The provider plugin **MUST** compute the cost by applying the pricing from the model's provider settings. The system **MUST** report AI credit consumption after each request via Usage Collector with attribution (tenant, user, model), and **MUST** guarantee at-least-once delivery of usage records. Usage for interrupted streams **MUST** be marked as incomplete.

Cross-cutting concern — applies to all operations, no dedicated UC.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`, `cpt-cf-llm-gateway-actor-usage-collector`, `cpt-cf-llm-gateway-actor-model-registry`

#### Per-Tenant Quota Enforcement

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-budget-enforcement-v1`

The system **MUST** check available AI credit quota before execution via Quota Enforcement and reject requests when the quota is exhausted. After request completion, the system **MUST** report actual AI credit consumption to Usage Collector.

Note: Gateway may consume more AI credits than the allocated quota because token consumption cannot be predicted before the request completes. This is expected behavior — the pre-request quota check is a best-effort gate, not a hard ceiling.

Cross-cutting concern — applies to all operations, no dedicated UC.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-quota-enforcement`, `cpt-cf-llm-gateway-actor-usage-collector`

#### Model Capability Check

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-model-capability-check-v1`

The system **MUST** verify that the resolved model supports the capabilities required by the request (e.g. streaming, image or file input, tool calling, web search, reasoning, structured output) before dispatching to the provider plugin. The check also takes into account the integration-level capabilities reported by the plugin. If a required capability is not supported, the system **MUST** return a `capability_not_supported` error to the consumer without calling the provider.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`

#### Provider Error Normalization

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-provider-error-normalization-v1`

The provider plugin **MUST** map provider errors to a fixed set of normalized categories, so that consumers can branch on the category regardless of provider. The system **MUST** support at least the following categories:

- `context_length_exceeded` — input exceeds the model context window; the consumer can trim history and retry
- `rate_limited` — provider rate limit hit; includes `retry_after` when the provider supplies it
- `content_filtered` — request or response blocked by the provider's content policy
- `invalid_request` — request rejected by the provider as malformed or unsupported; retrying without changes will not help
- `authentication_failed` — provider rejected the API key
- `provider_unavailable` — provider outage, overload, server error, or network failure; the request may succeed on retry

Errors that do not match any category **MUST** be returned as `provider_error`. Every normalized error **MUST** include the provider's original error code and message and the `provider_request_id` (when available). Errors that occur mid-stream **MUST** be delivered as a stream error event with the same category.

Cross-cutting concern — applies to all operations, no dedicated UC.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`, `cpt-cf-llm-gateway-actor-provider`

#### Completion Status Normalization

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-completion-status-v1`

The provider plugin **MUST** map the provider's stop reason to the normalized response `status` and `incomplete_details.reason` of the Open Responses contract, so that consumers can branch on the outcome regardless of provider:

| Outcome | Provider examples | `status` | `incomplete_details.reason` |
|---|---|---|---|
| Normal stop, including stop sequence and tool calls | OpenAI `stop`, `tool_calls`; Anthropic `end_turn`, `stop_sequence`, `tool_use`; Google `STOP` | `completed` | — |
| Truncated by the output token limit | OpenAI `length`; Anthropic `max_tokens`; Google `MAX_TOKENS` | `incomplete` | `max_output_tokens` |
| Output blocked by the provider's content policy | OpenAI `content_filter`; Google `SAFETY`, `RECITATION` | `incomplete` | `content_filter` |

The consumer can continue generation on `max_output_tokens` and surface a refusal on `content_filter`. A request that the provider blocks before generating any output is a provider error (`content_filtered`, see `cpt-cf-llm-gateway-fr-provider-error-normalization-v1`), not an incomplete response. A stop reason the plugin does not recognize **MUST** be mapped to `completed`. The provider's original stop reason **MUST** be included in the response in every case.

For streaming, every stream **MUST** end with exactly one terminal event — `response.completed`, `response.incomplete`, or `response.failed` — carrying the final status and usage, even when the provider omits one:

- the provider stream ends normally without a stop reason → `response.completed`;
- the provider connection breaks or the stream ends abnormally → `response.failed` with the normalized error category, and usage marked as incomplete.

When the consumer disconnects there is no one to receive a terminal event; usage is reported as incomplete (`cpt-cf-llm-gateway-fr-streaming-v1`).

Cross-cutting concern — applies to all operations, no dedicated UC.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`, `cpt-cf-llm-gateway-actor-provider`

#### Request Traceability

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-fr-request-traceability-v1`

The response `id` serves as the gateway request identifier. The system **MUST** propagate this identifier throughout the entire request processing pipeline — provider plugin calls, usage events reported to Usage Collector, error events, and audit events. All internal processing stages **MUST** be correlatable back to the originating response `id`.

The system **MUST** capture the `provider_request_id` returned by the provider (when available) and include it in usage events reported to Usage Collector and in error events. This enables end-to-end correlation between gateway operations and provider-side request logs.

Cross-cutting concern — applies to all operations, no dedicated UC.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`, `cpt-cf-llm-gateway-actor-usage-collector`

### P2 — Reliability, Governance & Media

#### FileStorage Media

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-fr-filestorage-media-v1`

The system **MUST** accept FileStorage URLs for image, file, audio, and video inputs, resolve the media (prefetching bytes when the provider plugin requires it), and store generated media in FileStorage, returning a retrievable URL.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider-plugin`, `cpt-cf-llm-gateway-actor-filestorage`

#### Image Generation

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-fr-image-generation-v1`

The system **MUST** accept a text prompt, generate an image via the resolved provider, store the result, and return a retrievable URL with usage metrics.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`

#### Text-to-Speech

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-fr-text-to-speech-v1`

The system **MUST** accept a text input, synthesize audio via the resolved provider, store the result, and return a retrievable URL with usage metrics. The system **MUST** support streaming mode for audio delivery.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`

#### Video Understanding

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-fr-video-understanding-v1`

The system **MUST** accept messages with a video reference (FileStorage URL or external URL), resolve the media, route to a video-capable model, and return the analysis with usage metrics.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`, `cpt-cf-llm-gateway-actor-filestorage`

#### Async Jobs

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-fr-async-jobs-v1`

The system **MUST** support async execution for long-running operations, returning a job ID that the consumer polls for results. The system **MUST** durably persist job state and store job results until the retention period expires.

The system **MUST** provide a uniform async experience regardless of whether the provider supports async natively:
- Consumer always receives a job ID and polls for results
- For providers without native async support, the job is simulated by running the regular provider call in the background
- For providers with native async support, the provider plugin exposes job submission and job result retrieval
- The system **MUST** guarantee result availability for the retention period after completion

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`

#### Timeout Enforcement

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-fr-timeout-v1`

The system **MUST** enforce the following timeout types:
- Time-to-first-token (TTFT): max wait for initial response chunk
- Total generation timeout: max duration for complete response

On timeout the system **MUST** return a timeout error.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`

#### Rate Limiting

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-fr-rate-limiting-v1`

The system **MUST** enforce rate limits at tenant and user levels and reject requests exceeding configured limits. Rate limiting is closely related to quota management; both may be provided by the same component.

Note: The specific mechanism and component providing rate limiting is an open question — see Open Questions section.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`

### P3 — Additional Capabilities

#### Speech-to-Text

- [ ] `p3` - **ID**: `cpt-cf-llm-gateway-fr-speech-to-text-v1`

The system **MUST** accept messages with an audio reference (FileStorage URL or external URL), resolve the media, route to an STT-capable model, and return the transcription with usage metrics.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`, `cpt-cf-llm-gateway-actor-filestorage`

#### Video Generation

- [ ] `p3` - **ID**: `cpt-cf-llm-gateway-fr-video-generation-v1`

The system **MUST** accept a text prompt, generate video via the resolved provider, store the result, and return a retrievable URL with usage metrics. Video generation typically requires async mode due to long processing times.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`

#### Realtime Audio

- [ ] `p3` - **ID**: `cpt-cf-llm-gateway-fr-realtime-audio-v1`

The system **MUST** support bidirectional audio streaming via a persistent connection for real-time voice conversations, with usage reported on session close.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`

#### Batch Processing

- [ ] `p3` - **ID**: `cpt-cf-llm-gateway-fr-batch-processing-v1`

The system **MUST** accept a batch of requests for async processing at reduced cost, abstracting provider batch APIs. Batch jobs can take up to 24 hours to complete; the system **MUST** durably persist batch-to-provider job ID mappings and results. Provider plugins that support batches expose batch submission, status retrieval, and result file retrieval.

**Actors**: `cpt-cf-llm-gateway-actor-consumer`, `cpt-cf-llm-gateway-actor-provider`

### P4 — Enterprise

#### Audit Events

- [ ] `p4` - **ID**: `cpt-cf-llm-gateway-fr-audit-events-v1`

The system **MUST** emit audit events via Audit Gear for compliance: request started, completed, failed.

Cross-cutting concern — applies to all operations, no dedicated UC.

**Actors**: `cpt-cf-llm-gateway-actor-audit-gear`

## 6. Non-Functional Requirements

### Scalability

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-nfr-scalability-v1`

The system **MUST** support horizontal scaling without instance-local state or inter-instance coordination. Any instance **MUST** be able to serve any request. Job state (from P2) **MUST** be accessible from all instances.

### Data Retention

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-nfr-data-retention-v1`

Job records apply from P2 (async) and P3 (batch). Async job records (ID mappings, results) are retained for 10 minutes after completion for LLM completion jobs and 48 hours after completion for batch jobs. Expired records are cleaned up automatically.

### Compatibility

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-nfr-compatibility-v1`

Gateway API **MUST** maintain backward compatibility within the same major version. Breaking changes require a new API version prefix. Co-existence with other platform gears is guaranteed — Gateway communicates exclusively through SDK traits and ClientHub, with no shared mutable state. The API contract is aligned with Open Responses (see § 7); protocol details are documented in DESIGN.md.

Existing consumers of llm-connectors use an OpenAI Chat Completions–compatible API. Whether a Chat Completions compatibility layer is required for migration is an open question (see § 13).

### NFR Exclusions

The following quality domains are handled at the platform level and do not require gear-specific NFRs:

- **Authentication / Authorization**: Handled by platform AuthN/AuthZ gears via SecurityContext. All Gateway endpoints require valid authentication; authorization is enforced per tenant through platform middleware.
- **Security implementation**: Credential storage handled by CredStore; Gateway only fetches keys per request and never persists or logs them (see `cpt-cf-llm-gateway-fr-provider-credentials-v1`); TLS and network security handled by infrastructure. Data classification and ownership are documented in § 3.2.
- **Safety**: Not applicable — LLM Gateway is a software-only API gear with no physical interaction or safety-critical operations.
- **Usability / Accessibility**: Not applicable — API-only gear with no user interface.
- **Compliance / Regulatory**: LLM Gateway does not store conversation content. Synchronous request/response data is transient. Async and batch job results are durably persisted for the retention period (up to 48 hours) and may contain provider response data that includes PII depending on consumer requests; Gateway treats all persisted job results as potentially sensitive and relies on retention-based cleanup as the primary data protection control. Compliance requirements for data processed by LLM providers are the responsibility of consumers and the providers themselves.
- **Operations (Deployment / Monitoring)**: Deployment, infrastructure monitoring, distributed tracing, and health checks are handled by platform infrastructure. Gear-level operational metrics (request counters, latency histograms, error breakdowns) are in scope — see Observability NFR below.
- **Maintainability / Documentation**: Follows platform-wide documentation standards. API documentation generated from OpenAPI specs in DESIGN.

### Recovery

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-nfr-recovery-v1`

**Native async jobs** (provider supports async natively — Gateway stores the provider job ID and resumes polling after restart): job state and results must survive Gateway instance restarts with zero data loss (RPO: 0). Recovery time is bounded by infrastructure restart time (RTO: platform-defined). Persistent storage availability is an infrastructure concern.

**Simulated async jobs** (sync provider + async request — Gateway executes the provider call synchronously on behalf of the consumer): NOT guaranteed to survive Gateway outages. If the Gateway instance is interrupted mid-execution, the in-flight provider call is lost and the job is marked failed. Interrupted simulated jobs are not retried to prevent unauthorized spending of provider tokens — the consumer receives a failed job status and must resubmit if desired.

Batch job metadata (ID mappings, status) survives restarts. Individual batch request results follow the same native/simulated distinction as async jobs.

### Observability

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-nfr-observability-v1`

The system **MUST** emit OpenTelemetry-compatible operational metrics covering the request lifecycle. Metrics **MUST** include:

- Request counters with model, provider, and status dimensions
- Streaming: streams started and streams aborted (by reason: client disconnect, timeout, error)
- Usage records reported as incomplete (interrupted streams)
- Time-to-first-token latency histogram with model and provider dimensions
- Quota operations: checks (allowed / rejected); reservation and settlement metrics if the reserve/settle model is adopted (see § 13)
- Schema validation failures for structured output with model and provider dimensions
- Async job cache misses (job not found on poll)

All metrics **MUST** be emittable via the platform's OpenTelemetry metrics infrastructure. Metric names **MUST** use the `llm_gateway_` prefix. Label cardinality **MUST** remain bounded — labels are limited to enumerable dimensions (model, provider, status, reason); unbounded values (tenant ID, request ID) **MUST NOT** be used as metric labels.

## 7. Public Library Interfaces

LLM Gateway exposes a REST API (documented in DESIGN.md) and the `llm-gateway-sdk` crate:

- **`LlmGatewayClientV1`** — consumer trait registered in ClientHub: `create_response` (sync), `create_response_stream` (streaming events), `create_embedding`. Request and response models follow the Open Responses contract.
- **`LlmGatewayProviderPluginClientV1`** — provider plugin trait: `capabilities`, `create_response`, `create_response_stream`, `create_embedding`. Each call receives the per-call context (model info as received from Model Registry, request id) and, per `cpt-cf-llm-gateway-fr-provider-credentials-v1`, an optional provider API key.
- **`LlmGatewayProviderPluginSpecV1`** — GTS type for provider plugin instances; `provider_type` matches the model info provider GTS type and drives plugin selection.

Note: the current SDK `ProviderCallCtx` states that plugins never read provider credentials. It needs to be updated to carry the optional API key as required by this PRD.

## 8. Use Cases

#### UC-001: Chat Completion

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-usecase-chat-completion-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant.

**Flow**:
1. Consumer sends create_response(model, input)
2. Gateway resolves model info via Model Registry
3. Gateway validates model capabilities against the request
4. Gateway selects the provider plugin by the model info provider GTS type
5. Gateway fetches the provider API key from CredStore (if the model has a secret reference)
6. Gateway calls the plugin with the request, model info, and key
7. Plugin converts the request to provider format, applies model settings, sets the key in request headers
8. Plugin calls the provider via OAGW (`proxy_request`)
9. Plugin converts the provider response to the normalized format and computes cost in AI credits
10. Gateway reports usage and returns the normalized response with usage

**Postconditions**: Response returned, usage reported.

**Alternative Flows**:
- **Provider error**: Gateway returns the normalized error category (`cpt-cf-llm-gateway-fr-provider-error-normalization-v1`).
- **Timeout** (P2): Gateway enforces TTFT and total timeouts; on expiry returns timeout error.
- **Invalid model**: Gateway returns model-not-found error if model is unavailable for tenant.
- **No plugin for provider**: Gateway returns provider-not-supported error.
- **Credentials unavailable**: Gateway returns credentials-unavailable error if the key cannot be fetched from CredStore.
- **Model not capable**: Gateway returns capability_not_supported error if model does not support a capability required by the request (e.g., tool calling, image input, web search, reasoning, structured output).
- **Unsupported generation parameter**: Gateway returns unsupported_parameter error naming the parameter; with `drop_unsupported_params` set, Gateway drops the parameter, proceeds, and lists it in `ignored_parameters` (`cpt-cf-llm-gateway-fr-generation-parameters-v1`).

**Acceptance criteria**:
- Response in normalized format regardless of provider
- Usage included: cost in AI credits, generic token usage, provider raw usage
- Provider errors returned with a normalized category, the original provider code and message, and `provider_request_id`
- Response `status` and `incomplete_details.reason` normalized from the provider stop reason (`cpt-cf-llm-gateway-fr-completion-status-v1`); original stop reason included
- Generic parameters the model does not support are rejected by default, or dropped and listed in `ignored_parameters` when `drop_unsupported_params` is set; unsupported cache-control hints are always dropped and listed

#### UC-002: Streaming Chat Completion

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-usecase-streaming-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant, model supports streaming.

**Flow**:
1. Consumer sends create_response(stream=true)
2. Gateway resolves model info via Model Registry, validates capabilities, selects plugin, fetches key (as in UC-001)
3. Plugin establishes streaming connection to provider via OAGW
4. Plugin normalizes each chunk
5. Gateway streams chunks to Consumer
6. Gateway ends the stream with exactly one terminal event (`response.completed` or `response.incomplete`) carrying the normalized status and usage, even if the provider sent no explicit stop event

**Postconditions**: Stream completed, usage reported.

**Alternative Flows**:
- **Mid-stream provider failure**: Gateway ends the stream with a `response.failed` terminal event carrying the normalized error category and reports usage marked as incomplete.
- **Consumer disconnects**: Gateway cancels the upstream provider request and reports usage marked as incomplete (optionally with a labeled estimate).
- **Model not capable**: Gateway returns capability_not_supported error if model does not support streaming.

**Acceptance criteria**:
- Chunks normalized from provider format
- Every stream ends with exactly one terminal event (`response.completed`, `response.incomplete`, or `response.failed`), even when the provider omits one
- Terminal event includes the normalized status, `incomplete_details.reason` when incomplete, and usage metrics
- Connection errors propagated to consumer as `response.failed`

#### UC-003: Embeddings Generation

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-usecase-embeddings-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Embedding model available for tenant.

**Flow**:
1. Consumer sends embed(model, input[]) with optional `dimensions`, `input_type`, `encoding_format`, and provider-specific parameters
2. Gateway resolves model info via Model Registry
3. Gateway validates that the model supports embeddings
4. Gateway selects the provider plugin by the model info provider GTS type
5. Gateway fetches the provider API key from CredStore (if the model has a secret reference)
6. Gateway calls the plugin with the request, model info, and key
7. Plugin converts the request to provider format, maps generic embedding parameters, applies model settings, sets the key in request headers
8. Plugin calls the provider via OAGW (`proxy_request`)
9. Plugin converts the provider response to normalized vectors in the requested `encoding_format` and computes cost in AI credits
10. Gateway reports usage and returns the vectors with usage

**Postconditions**: Vectors returned, usage reported.

**Alternative Flows**:
- **Provider error**: Gateway returns the normalized error category (`cpt-cf-llm-gateway-fr-provider-error-normalization-v1`).
- **Invalid input**: Gateway returns validation error if input is empty or exceeds provider limits.
- **Invalid model**: Gateway returns model-not-found error if model is unavailable for tenant.
- **No plugin for provider**: Gateway returns provider-not-supported error.
- **Credentials unavailable**: Gateway returns credentials-unavailable error if the key cannot be fetched from CredStore.
- **Model not capable**: Gateway returns capability_not_supported error if model does not support embeddings.
- **Unsupported dimensions**: Gateway returns unsupported_parameter error if the model cannot produce the requested `dimensions`, even when `drop_unsupported_params` is set.
- **Unsupported input_type**: Gateway returns unsupported_parameter error; with `drop_unsupported_params` set, Gateway drops it and lists it in `ignored_parameters`.

**Acceptance criteria**:
- Vectors returned in normalized format regardless of provider
- Returned vectors have exactly the requested `dimensions`, or the request is rejected
- `input_type` mapped to the provider's equivalent (e.g. Google `task_type`, Voyage `input_type`)
- Both `float` and `base64` encoding formats supported for every model
- Usage included: cost in AI credits, generic token usage, provider raw usage
- Provider errors returned with a normalized category, the original provider code and message, and `provider_request_id`

#### UC-004: Vision (Image Analysis)

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-usecase-vision-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant, model supports required content type.

**Flow**:
1. Consumer sends create_response with images as base64 data or external URLs
2. Gateway resolves model info via Model Registry and selects the provider plugin
3. Plugin forwards image URLs to the provider
4. Plugin sends request via Outbound API Gateway
5. Provider returns analysis
6. Gateway returns response with usage

**Postconditions**: Response returned, usage reported.

**Alternative Flows**:
- **Media resolution failure**: Gateway returns media-not-found error if an image cannot be retrieved or decoded.
- **FileStorage URL** (P2): Gateway resolves the media via FileStorage before dispatch.
- **Provider error**: Gateway returns the normalized error category (`cpt-cf-llm-gateway-fr-provider-error-normalization-v1`).

**Acceptance criteria**:
- Multiple images supported per request
- Response in normalized format
- Usage metrics included

#### UC-005: Image Generation

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-usecase-image-generation-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Image generation model available for tenant.

**Flow**:
1. Consumer sends generation request with prompt
2. Gateway resolves provider via Model Registry
3. Gateway sends request via Outbound API Gateway
4. Provider returns generated image
5. Gateway stores image in FileStorage
6. Gateway returns URL with usage

**Postconditions**: Image stored, URL returned, usage reported.

**Alternative Flows**:
- **Provider error**: Gateway returns the normalized error category (`cpt-cf-llm-gateway-fr-provider-error-normalization-v1`).
- **FileStorage upload failure**: Gateway returns storage error; generated image is lost.

**Acceptance criteria**:
- Generated image accessible via returned URL
- Response in normalized format
- Usage metrics included

#### UC-006: Speech-to-Text

- [ ] `p3` - **ID**: `cpt-cf-llm-gateway-usecase-speech-to-text-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: STT model available for tenant.

**Flow**:
1. Consumer sends message with audio URL
2. Gateway resolves provider via Model Registry
3. Gateway fetches audio from FileStorage
4. Gateway sends request via Outbound API Gateway
5. Provider returns transcription
6. Gateway returns text response with usage

**Postconditions**: Transcription returned, usage reported.

**Alternative Flows**:
- **Media resolution failure**: Gateway returns media-not-found error if audio URL is invalid or inaccessible.
- **Provider error**: Gateway returns the normalized error category (`cpt-cf-llm-gateway-fr-provider-error-normalization-v1`).

**Acceptance criteria**:
- Transcription in normalized format
- Usage metrics included

#### UC-007: Text-to-Speech

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-usecase-text-to-speech-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: TTS model available for tenant.

**Flow**:
1. Consumer sends TTS request with text
2. Gateway resolves provider via Model Registry
3. Gateway sends request via Outbound API Gateway
4. Provider returns audio
5. Gateway stores audio in FileStorage
6. Gateway returns URL with usage

**Postconditions**: Audio stored, URL returned, usage reported.

**Alternative Flows**:
- **Provider error**: Gateway returns the normalized error category (`cpt-cf-llm-gateway-fr-provider-error-normalization-v1`).
- **FileStorage upload failure**: Gateway returns storage error; generated audio is lost.

**Acceptance criteria**:
- Generated audio accessible via returned URL
- Streaming mode supported (audio chunks returned directly)
- Usage metrics included

#### UC-008: Video Understanding

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-usecase-video-understanding-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant, model supports required content type.

**Flow**:
1. Consumer sends message with video URL
2. Gateway resolves provider via Model Registry
3. Gateway fetches video from FileStorage
4. Gateway sends request via Outbound API Gateway
5. Provider returns analysis
6. Gateway returns response with usage

**Postconditions**: Response returned, usage reported.

**Alternative Flows**:
- **Media resolution failure**: Gateway returns media-not-found error if video URL is invalid or inaccessible.
- **Provider error**: Gateway returns the normalized error category (`cpt-cf-llm-gateway-fr-provider-error-normalization-v1`).

**Acceptance criteria**:
- Response in normalized format
- Usage metrics included

#### UC-009: Video Generation

- [ ] `p3` - **ID**: `cpt-cf-llm-gateway-usecase-video-generation-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Video generation model available for tenant.

**Flow**:
1. Consumer sends generation request with prompt
2. Gateway resolves provider via Model Registry
3. Gateway sends request via Outbound API Gateway
4. Provider returns generated video
5. Gateway stores video in FileStorage
6. Gateway returns URL with usage

**Postconditions**: Video stored, URL returned, usage reported.

**Alternative Flows**:
- **Provider error**: Gateway returns the normalized error category (`cpt-cf-llm-gateway-fr-provider-error-normalization-v1`).
- **FileStorage upload failure**: Gateway returns storage error; generated video is lost.

**Acceptance criteria**:
- Generated video accessible via returned URL
- Async mode supported (typically required due to long processing)
- Usage metrics included

#### UC-010: Tool/Function Calling

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-usecase-tool-calling-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant, model supports function calling.

**Flow**:
1. Consumer sends create_response with tool definitions and, optionally, `tool_choice` and `parallel_tool_calls`
2. Gateway resolves model info via Model Registry and selects the provider plugin
3. Gateway resolves schemas via Type Registry (for reference and inline GTS formats)
4. Plugin converts tools and tool selection controls to provider format
5. Plugin sends request via Outbound API Gateway
6. Provider returns tool_calls
7. Gateway returns tool_calls in unified format
8. Consumer executes tools, sends results
9. Gateway forwards tool results to provider
10. Provider returns final response
11. Gateway returns response with usage

**Postconditions**: Response returned, usage reported.

**Alternative Flows**:
- **Schema resolution failure**: Gateway returns schema-not-found error if GTS reference is unresolvable.
- **Provider error**: Gateway returns the normalized error category (`cpt-cf-llm-gateway-fr-provider-error-normalization-v1`).
- **Partial tool execution**: Consumer sends partial tool results; Gateway forwards to provider for continued generation.
- **Unknown function in tool_choice**: Gateway returns validation error if `tool_choice` names a function not defined in the request.
- **Unsupported tool selection control**: Gateway returns unsupported_parameter error if the model does not support the requested `tool_choice` value or `parallel_tool_calls` setting; with `drop_unsupported_params` set, Gateway drops it and lists it in `ignored_parameters`.

**Acceptance criteria**:
- Tool definitions supported: reference, inline GTS, unified format (OpenAI-like)
- Gateway web search (`cf:web_search`) and provider-specific tools (`<provider>:<tool>`) can be combined with function tools in one request (see UC-020)
- Tool calls returned in unified format
- `tool_choice` (`none`, `auto`, `required`, specific function) and `parallel_tool_calls` mapped to provider equivalents
- Response in normalized format

#### UC-011: Structured Output

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-usecase-structured-output-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant.

**Flow**:
1. Consumer sends create_response with a JSON schema output format (or `json_object` for JSON mode without a schema)
2. Gateway resolves model info via Model Registry and selects the provider plugin
3. Plugin adapts the schema to the JSON Schema subset the provider accepts, maps it to the provider's native structured output, and sends the request via Outbound API Gateway
4. Provider returns JSON response
5. Gateway validates the response against the original schema (for `json_object`: that it is well-formed JSON)
6. Gateway returns validated response with usage (or validation_error if invalid)

**Postconditions**: Valid JSON returned, usage reported.

**Alternative Flows**:
- **Schema validation failure**: Gateway returns validation_error with details if response does not match schema.
- **Unsupported schema**: Gateway returns unsupported_schema error naming the construct if the schema cannot be expressed in the provider's JSON Schema subset; the provider is not called.
- **Model not capable**: Gateway returns capability_not_supported error if the model supports neither JSON mode nor schema-constrained output.
- **Provider error**: Gateway returns the normalized error category (`cpt-cf-llm-gateway-fr-provider-error-normalization-v1`).

**Acceptance criteria**:
- Response validated against the original schema, including constraints removed during schema adaptation
- `json_object` responses validated as well-formed JSON
- Schemas adapted to the provider's JSON Schema subset where possible; otherwise rejected with unsupported_schema before calling the provider
- Returns validation_error with details if schema validation fails
- Response in normalized format

#### UC-012: Document Understanding

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-usecase-document-understanding-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant, model supports required content type.

**Flow**:
1. Consumer sends message with a document as base64 data or external URL
2. Gateway resolves model info via Model Registry and selects the provider plugin
3. Plugin forwards the document to the provider, or converts it to the format the provider requires
4. Plugin sends request via Outbound API Gateway
5. Provider returns analysis
6. Gateway returns response with usage

**Postconditions**: Response returned, usage reported.

**Alternative Flows**:
- **Media resolution failure**: Gateway returns media-not-found error if the document cannot be retrieved or decoded.
- **FileStorage URL** (P2): Gateway resolves the document via FileStorage before dispatch.
- **Provider error**: Gateway returns the normalized error category (`cpt-cf-llm-gateway-fr-provider-error-normalization-v1`).

**Acceptance criteria**:
- Response in normalized format
- Usage metrics included

#### UC-013: Async Jobs

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-usecase-async-jobs-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant.

**Flow**:
1. Consumer sends request with async=true
2. Gateway resolves provider via Model Registry
3. Gateway initiates async job
4. Gateway returns job_id
5. Consumer polls get_job(job_id)
6. Gateway returns status/result
7. (Optional) Consumer cancels job via cancel_job(job_id)

**Postconditions**: Job completed, cancelled, or result returned.

**Alternative Flows**:
- **Provider error during execution**: Gateway marks job as failed, stores error details in job result.
- **Job not found**: Gateway returns job-not-found error if job ID is invalid or expired.
- **Cancellation of completed job**: Gateway returns job-already-completed status; no-op.

**Acceptance criteria**:
- Sync provider + async request: Gateway simulates job
- Async provider + sync request: Gateway polls internally
- Job status: pending, running, completed, failed, cancelled
- Job cancellation supported

#### UC-014: Realtime Audio

- [ ] `p3` - **ID**: `cpt-cf-llm-gateway-usecase-realtime-audio-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant, model supports realtime audio.

**Flow**:
1. Consumer establishes persistent bidirectional connection
2. Gateway resolves provider via Model Registry
3. Gateway connects to provider via bidirectional channel
4. Bidirectional audio/text streaming
5. Consumer closes connection
6. Gateway returns usage summary

**Postconditions**: Session closed, usage reported.

**Alternative Flows**:
- **Connection failure**: Gateway returns connection error if realtime channel cannot be established.
- **Mid-session provider failure**: Gateway closes session with error event and reports partial usage.

**Acceptance criteria**:
- Bidirectional streaming supported
- Usage summary on close

#### UC-015: Timeout Enforcement

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-usecase-timeout-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant.

**Flow**:
1. Consumer sends request
2. Gateway starts timeout tracking (TTFT, total)
3. Gateway sends request to provider
4. If TTFT timeout: Gateway returns timeout error
5. If total timeout: Gateway returns timeout error
6. Gateway returns response or error

**Postconditions**: Response returned or timeout error.

**Acceptance criteria**:
- TTFT (time-to-first-token) timeout enforced
- Total generation timeout enforced
- On timeout: timeout error returned

#### UC-016: Rate Limiting

- [ ] `p2` - **ID**: `cpt-cf-llm-gateway-usecase-rate-limiting-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Rate limits configured for tenant.

**Flow**:
1. Consumer sends request
2. Gateway checks rate limits
3. If limit exceeded: Gateway returns rate_limited error
4. If within limits: Gateway proceeds with request

**Postconditions**: Request processed or rejected.

**Acceptance criteria**:
- Rate limits enforced at tenant level
- Rate limits enforced at user level
- Exceeded requests return rate_limited error

#### UC-017: Batch Processing

- [ ] `p3` - **ID**: `cpt-cf-llm-gateway-usecase-batch-processing-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant. If the resolved provider does not support a native batch API, Gateway rejects the batch request with a batch-not-supported error.

**Flow**:
1. Consumer submits batch of requests
2. Gateway resolves provider via Model Registry
3. Gateway submits to provider batch API
4. Gateway returns batch_id
5. Consumer polls for results
6. Gateway returns status and results
7. (Optional) Consumer cancels batch

**Postconditions**: Batch completed, results returned.

**Acceptance criteria**:
- Abstracts OpenAI Batch API, Anthropic Message Batches
- Partial results available as completed
- Batch cancellation supported

#### UC-018: Provider Plugin Registration

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-usecase-provider-plugin-registration-v1`
**Actor**: `cpt-cf-llm-gateway-actor-provider-plugin`

**Preconditions**: Plugin compiled into the Gateway binary; Type Registry available.

**Flow**:
1. On init, the plugin resolves the OAGW client
2. Plugin registers its plugin GTS instance in Type Registry
3. Plugin registers its provider settings schema in Type Registry
4. A model is registered in Model Registry with provider-specific settings; Model Registry fetches the settings schema from Type Registry and validates the settings
5. On a request for that model, Gateway selects the plugin whose provider type matches the model info provider GTS type

**Postconditions**: Model Registry can validate settings for the provider; Gateway can route requests to the plugin.

**Alternative Flows**:
- **Invalid settings**: Model Registry rejects the model with a validation error.

**Acceptance criteria**:
- Plugin instance and settings schema are discoverable in Type Registry after startup
- Settings that do not match the schema are rejected by Model Registry

#### UC-019: Reasoning

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-usecase-reasoning-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant, model supports reasoning.

**Flow**:
1. Consumer sends create_response with a reasoning configuration (effort and/or token budget)
2. Gateway validates that the model supports the requested reasoning options
3. Plugin maps the configuration to the provider's native reasoning parameters
4. Provider returns the answer and reasoning output
5. Gateway returns reasoning output, reasoning token usage, and opaque continuation artifacts (signatures, encrypted reasoning content)
6. In the next turn, the consumer sends the previous output items back; the plugin forwards the reasoning artifacts to the provider

**Postconditions**: Response with reasoning returned, usage reported.

**Alternative Flows**:
- **Unsupported option** (e.g. budget on an effort-only provider): Gateway returns capability_not_supported.

**Acceptance criteria**:
- Multi-turn reasoning works without `previous_response_id` or `store=true`
- Reasoning tokens reported in generic usage

#### UC-020: Web Search and Provider-Specific Tools

- [ ] `p1` - **ID**: `cpt-cf-llm-gateway-usecase-web-search-v1`
**Actor**: `cpt-cf-llm-gateway-actor-consumer`

**Preconditions**: Model available for tenant; the provider supports the requested tool.

**Flow**:
1. Consumer sends create_response with `cf:web_search` and/or a provider-specific tool (e.g. `openai:shell`)
2. Gateway validates model capabilities
3. Plugin maps `cf:web_search` to the provider's native web search tool and forwards provider-specific tools as-is
4. Provider executes the tools and returns the answer
5. Gateway returns the answer with normalized citations, web search results, and provider tool call outputs

**Postconditions**: Response returned, usage reported.

**Alternative Flows**:
- **Provider has no native web search**: Gateway returns capability_not_supported.
- **Tool prefix does not match the model's provider**: Gateway returns a validation error.

**Acceptance criteria**:
- Gateway never runs searches itself
- Citations returned in a normalized format regardless of provider

## 9. Acceptance Criteria

- [ ] P1 capabilities (text, image and file input, embeddings, reasoning, tools, web search, structured output) produce normalized responses regardless of provider
- [ ] Every P1 response includes usage with cost in AI credits; usage for interrupted streams is marked incomplete
- [ ] Provider plugins register their plugin instance and settings schema in Type Registry at startup
- [ ] Provider API keys are never persisted or logged by Gateway
- [ ] Gateway overhead < 50ms P99 excluding provider latency
- [ ] Availability ≥ 99.9%
- [ ] Usage records are delivered to Usage Collector at least once for every completed request
- [ ] (P2+) All supported modalities (audio, video, generated media) produce normalized responses regardless of provider
- [ ] (P2+) Async and batch job state survives Gateway instance restarts
- [ ] (P2+) Expired job records are cleaned up within the defined retention windows

## 10. Dependencies

| Dependency | Description | Criticality |
|------------|-------------|-------------|
| Outbound API Gateway (OAGW) | Executes all external provider calls via SDK `proxy_request`, provides circuit breaking; may optionally handle upstream auth. Can be substituted with a plain HTTP client in minimal deployments | `p1` |
| Model Registry | Resolves model identifiers to model info (provider type, model name, `base_url`, secret reference, settings, pricing); validates provider settings against plugin schemas | `p1` |
| CredStore | Provides provider API keys to Gateway by the model's secret reference | `p1` |
| Type Registry | Stores provider plugin instances and provider settings schemas; resolves GTS schema references for tool definitions | `p1` |
| Usage Collector | Receives AI credit consumption reports | `p1` |
| FileStorage | Stores and retrieves media files (images, audio, video, documents) | `p2` |
| Quota Enforcement | Checks available AI credit quotas before request execution | `p1` |
| Audit Gear | Receives compliance audit events | `p4` |

## 11. Assumptions

- LLM providers are accessible via OAGW; OAGW upstreams and routes for provider endpoints are configured by the deployment (unless automatic provisioning is adopted — see § 13)
- Provider API keys are stored in CredStore and referenced from model info
- Model Registry maintains an up-to-date catalog of available models and their capabilities per tenant
- FileStorage is available for media operations from P2
- From P2, a database is available and shared across all Gateway instances for job state

## 12. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Provider API breaking changes | Plugin incompatibility, request failures | Plugins own provider HTTP contracts (no third-party SDKs), integration test suite per provider |
| Provider API key exposure | Credential leak via logs or memory dumps | Keys fetched per request, never persisted or logged; redaction in plugin request logging |
| Inaccurate billing of interrupted streams | Under- or over-charging AI credits | Usage explicitly marked incomplete; estimation policy decided with quota ownership (§ 13) |
| Feature regression vs llm-connectors | Consumers lose web search, reasoning, custom endpoints, providers | Explicit parity list before migration (§ 13) |
| Provider rate limit exhaustion | Requests rejected by provider | Gateway-level rate limiting |
| Database growth from batch jobs | Storage pressure, degraded query performance | Automated retention cleanup, monitoring on table sizes |
| Provider outage during batch processing | Batch jobs stuck in pending state | Timeout-based job expiration, consumer-visible status updates |
| Usage record delivery lag | Budget enforcement decisions based on stale data | Guaranteed delivery mechanism with short polling interval |

## 13. Open Questions

- **Quota enforcement ownership** (Owner: Platform Architecture, Resolve by: before P1 implementation begins): *Ownership boundary*: does Gateway own quota *enforcement* (atomic preflight reserve + terminal settle + bounded debit on abort) or only *metering* (report usage, external component enforces)? The current `check_quota()` → proceed → `report_usage()` sequence is non-atomic — under concurrent load, multiple requests pass `check_quota()` before any `report_usage()` completes, allowing a tenant to exceed their limit by N×budget (N = concurrent in-flight requests). If Gateway owns enforcement, the reserve/settle pattern is required, not optional — a best-effort gate is effectively decorative under load. If Gateway does metering only, the external component handles enforcement asynchronously. The decision must be made before implementing `cpt-cf-llm-gateway-fr-budget-enforcement-v1`.
- **Budget enforcement edge cases** (Owner: Platform Architecture, Resolve by: with quota enforcement ownership decision above): The following scenarios must be addressed when the ownership boundary is decided: (a) *Provider stream without usage* — if a provider stream closes before delivering usage data (network error, provider error mid-stream, consumer disconnect), usage is reported marked as incomplete (`cpt-cf-llm-gateway-fr-streaming-v1`); policy must specify what is debited in that case: input tokens only, an estimate, or zero; (b) *Background job budgeting* — quota is checked at submission time but the job executes minutes later under a possibly changed quota state, requiring the reserve/settle pattern to span the submission-to-execution gap if Gateway owns enforcement.
- **Rate limiting mechanism** (Owner: Platform Architecture, Resolve by: before P2 implementation begins): Rate limiting (`cpt-cf-llm-gateway-fr-rate-limiting-v1`) is closely related to quota management. Whether rate limiting and quota enforcement are provided by the same component or separate components is an open question.
- **Request monitoring and observability** (Owner: Platform Architecture, Resolve by: before P1 implementation begins): The LLM Gateway needs to track how many requests are being processed, including metrics such as request counts, latency, error rates, and token usage per provider/model. However, the platform does not yet have a standardized approach to monitoring and observability across gears. A platform-wide monitoring strategy must be agreed upon before implementing gear-level metrics, to ensure consistency and avoid fragmented solutions.
- **Cross-gear request traceability** (Owner: Platform Architecture, Resolve by: before P1 implementation begins): The platform needs a mechanism that assigns a request ID at the incoming requests gateway (api-gateway gear) and propagates it through all inter-gear calls via ClientHub/SDK traits. This would enable end-to-end request tracing across gear boundaries — for example, a consumer request arriving at the API gateway, flowing through LLM Gateway, then to Model Registry and Usage Collector, all correlated by a single platform-level request ID. Currently, each gear operates with its own request context; there is no standardized correlation ID propagated across inter-gear boundaries. This is a platform-level concern that affects all gears, not just LLM Gateway. The LLM Gateway's response `id` (`cpt-cf-llm-gateway-fr-request-traceability-v1`) provides gear-level traceability, but a platform-wide mechanism is needed for cross-gear correlation.
- **OAGW upstream provisioning** (Owner: LLM Gateway team, Resolve by: before P1 implementation begins): Default: OAGW is configured externally and plugins only call `proxy_request`. If Model Registry and plugins become the source of OAGW upstreams and routes, the following must be decided: (1) well-known provider upstreams and routes registered by plugins at init and picked up by OAGW in post-init; provider routes must be part of the provider GTS, since some providers (e.g. Ollama) have no well-known upstream; (2) for models with a user-provided `base_url`, Model Registry registers the upstream and routes in OAGW at model registration — under which identity and in which tenant (root tenant is the likely choice, to avoid upstream overrides in the tenant hierarchy and extra AuthZ rights for the user); (3) registration must be idempotent — existing routes are skipped; (4) when routes are removed from OAGW. Alternative: allowed base URLs are listed in plugin configuration and registered by the plugin at init; only those base URLs are allowed.
- **Plugin capabilities** (Owner: LLM Gateway team, Resolve by: before P2 implementation begins): Gateway needs a way to know what a provider plugin can do natively and what must be emulated — blocking vs streaming prediction, native async jobs, batches. The current SDK `capabilities()` covers streaming transport and media input mode; the structure must be extended before async and batch work.
- **Stream cancellation** (Owner: LLM Gateway team, Resolve by: before P1 implementation begins): Mechanism for cancelling the upstream provider request when the consumer drops the connection, across the Gateway core, plugin stream, and OAGW.
- **GTS identifiers** (Owner: LLM Gateway team, Resolve by: before P1 implementation begins): Specific GTS names for provider plugin instances, provider settings schemas, and provider route declarations.
- **Migration from llm-connectors** (Owner: LLM Gateway team, Resolve by: before migration planning): (1) Whether a Chat Completions–compatible API layer is needed for existing llm-connectors consumers. (2) Which providers are required at migration. llm-connectors currently supports chat models from OpenAI (Chat Completions and Responses), Anthropic, Google, Groq, Mistral, xAI, Perplexity (including Perplexity Agent), Together AI, vLLM, and Ollama, and embeddings from OpenAI, Google, Ollama, and VoyageAI. (3) How user-supplied provider keys and private models (currently billed at a flat rate) map to CredStore and Model Registry.

## 14. Traceability

- **Design**: [DESIGN.md](./DESIGN.md) — not yet created
- **ADRs**: [ADR/](./ADR/) — not yet created
- **Design inputs**: provider plugins proposal (plugin trait, settings schemas, P1 scope); LLM Gateway integration diagrams (deployment, OAGW integration, eval flow)
- **SDK**: `llm-gateway-sdk` (`LlmGatewayClientV1`, `LlmGatewayProviderPluginClientV1`, `LlmGatewayProviderPluginSpecV1`)
- **Decomposition**: Not yet created — planned for feature-level breakdown