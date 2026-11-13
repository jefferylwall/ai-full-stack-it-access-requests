# OpenAI provider configuration and verification

The adapter uses non-streaming `POST /v1/responses`. Its default model is
`gpt-6-luna`, with `max_output_tokens=2048` and `reasoning.effort=none`.
[`config/provider.json`](../config/provider.json) records the complete structural
configuration. The model page lists Responses and structured-output support;
the protocol documentation review was recorded on 2026-11-13 in
[`provider-protocol.json`](provider-protocol.json).
Sources: [model](https://developers.openai.com/api/docs/models/gpt-6-luna),
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[Responses reference](https://developers.openai.com/api/reference/resources/responses/methods/create).

Mock mode remains the default. To select an actual provider request, the operator
must choose `--provider openai` or `PROVIDER=openai`. The facilitator supplies
`API_KEY` in the environment. There is no credential argument, credential file
loader or `.env` auto-loader. The credential accessor runs only when sending an
OpenAI request; constructing an adapter, inspecting records and previewing request
evidence do not load it.

| Setting | CLI override before command | Default |
| --- | --- | --- |
| `PROVIDER` | `--provider` | `mock`; supported values `mock` and `openai` |
| `MODEL` | `--model` | `gpt-6-luna` for OpenAI |
| `PROVIDER_ENDPOINT` | `--endpoint` | `https://api.openai.com/v1/responses` |
| `REQUEST_SETTINGS` | `--request-settings` | JSON max output tokens 2048, reasoning effort none |
| `API_KEY` | None | Environment only, required at live send time |

An optional live invocation, requiring the supplied environment credential, is:

```bash
python3 -m access_intake --provider openai --model gpt-6-luna intake materials/requests/IT-001.json
```

This command is not part of automated tests or the mock handover examples.
Model access and live accuracy have not been verified by a live call. Other model
IDs are configurable; their parameter compatibility requires operator review.
Sampling settings `temperature` and `top_p`, `max_output_tokens` and
`reasoning.effort` are supported request parameters. Unknown parameter keys are
rejected rather than allowing credentials, roles, tools or schema settings to be
overridden. Endpoints must use HTTPS without URL credentials, query or fragment.

Requests preserve the extraction prompt's system/user roles. The entire decoded
message is JSON data in the user content. `text.format` selects strict
`json_schema`, name `access_request`, and the supplied schema. Tools, streaming,
background execution, provider storage and truncation are disabled. These fixed
request settings are captured with live attempts and included in their processing
fingerprint. Requested model/settings stay in provenance; successful
`response_source` uses `openai/` followed by the returned model identifier.

The standard-library HTTP transport uses a finite I/O timeout and refuses
redirects so authorization is not forwarded to another endpoint. Worker deadlines
also bound total waits. HTTP 429 maps to `rate_limit`, HTTP 5xx to `unavailable`,
and other HTTP/API errors to `provider_error`. Transport failures and timeouts
have separate safe categories. There are no automatic retries. Raw provider error
bodies, headers and exception URLs are discarded; optional structured diagnostics
contain only safe event names, categories, states and numeric status codes.

Native response and message completion are checked before extraction text is
used. Refusal content discards accompanying text. Incomplete responses discard
even valid-looking JSON. Only completed output text enters local JSON, schema
and exact-source-evidence validation, followed by deterministic review routing.
Missing or unsupported native structure produces an explicit error. Every failure
has null extraction, the error route and a reproducible retry command.

`OpenAIResponsesProvider.request_evidence(prompt)` builds an offline request
preview without loading a credential or contacting the model. Called without a
prompt after a request, it copies the actual last request construction. Evidence
includes the model/configuration, schema, roles, safe outcome and implemented
response-handling descriptions. Authorization is always `Bearer [REDACTED]` in
evidence, and credentials never become extraction text. Producing HTTP
authorization exists only in the request transport, not in stored attempts.

Observed Step 8 verification: T31–T33 passed using saved native responses and a
fake HTTP opener. They exercise the actual adapter and HTTP request construction,
CLI environment selection with the real credential accessor replaced, HTTP
errors, refusal/interruption, redacted evidence and a successful override case.
The test credential was absent from SQLite files including sidecars, queue,
history, logs and exports. These observations establish mocked transport behavior;
they do not establish provider availability, live extraction accuracy or general
instruction-override recognition.
