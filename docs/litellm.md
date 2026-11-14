# LiteLLM discovery

Select `--mode litellm` explicitly. The default remains `legacy`; JSON `metadata` mode keeps its existing authoritative control-replacement behavior. `--metadata-json` is only accepted in metadata mode.

## Connection and response

Set `OPEN_WEBUI_URL` and `OPEN_WEBUI_API_KEY` for the Open WebUI admin API, and separately set `LITELLM_URL` and `LITELLM_API_KEY` for discovery. The LiteLLM credential must be allowed to read `/model/info`, including every relevant deployment for the public names being configured. The proxy root URL may include a reverse-proxy prefix but must omit `/v1` and `/model/info`. A trailing slash is accepted.

The script reads `GET /model/info` with `Authorization: Bearer <LITELLM_API_KEY>` in a separate session before reading Open WebUI. An invalid response envelope, malformed deployment record, HTTP error, invalid JSON, or network failure aborts discovery. No changes are submitted and no legacy fallback occurs. Errors omit response bodies to avoid printing deployment secrets.

Expected shape:

```json
{
  "data": [
    {
      "model_name": "my-public-model",
      "litellm_params": {"model": "provider/private-deployment"},
      "model_info": {
        "id": "deployment-id",
        "mode": "chat",
        "supported_endpoints": ["/v1/chat/completions"],
        "reasoning_effort_levels": ["low", "medium", "high"]
      }
    }
  ]
}
```

Only the exact public `model_name` is matched to Open WebUI's upstream model ID. Provider/deployment IDs are not aliases. A saved preset uses `base_model_id` first. Unmatched and unknown-chat models remain unchanged.

## Chat capability rules

The target is Open WebUI's OpenAI-compatible Chat Completions path, with LiteLLM handling any provider translation. This script does not change request routing or enable Open WebUI's Responses API mode.

| `model_info.mode` | Without endpoint metadata | With endpoint metadata |
| --- | --- | --- |
| `chat` | Supported, including multimodal chat | Supported if `/chat/completions` is listed; otherwise unknown |
| `responses` | Supported through LiteLLM's documented bridge | Supported if `/chat/completions` or `/responses` is listed; otherwise unknown |
| `completion` | Unknown: mode alone does not prove a chat adapter | Supported only if `/chat/completions` is explicitly listed; otherwise unknown |
| `embedding`, `rerank`, `image_generation`, `image_edit`, `audio_transcription`, `audio_speech`, `moderation` | Unsupported | Unsupported unless chat or Responses endpoints contradict the mode, then unknown |
| Missing or unfamiliar | Unknown | Unknown |

Endpoint paths accept an optional `/v1` prefix, case differences, and a trailing slash. Empty or malformed endpoint declarations remain unknown. Unrecognized modes are intentionally not guessed. Endpoint declarations are informational in LiteLLM by default; these rules express advertised compatibility, not proof of a successful request.

For duplicate public names, all deployments must agree on a known chat result; mixed or unknown results leave the group unchanged. Routing filters, aliases, wildcard expansion, fallbacks, or deployments invisible to the discovery credential can limit what the response establishes. Ensure the credential sees the full deployment group used by Open WebUI.

Known non-chat models follow metadata mode's policy: hide via `meta.hidden`, preserve `is_active` and existing grants, grant no new public read access, and remove only script-managed reasoning controls. Known chat models receive public read access; existing manual hiding remains unless `--unhide-chat-models` is specified. Other settings and grants are retained.

## Reasoning effort

Only `model_info.reasoning_effort_levels` establishes choices. Neither `supports_reasoning` nor parameter names in `supported_openai_params` establish valid values. No choices are derived from these fields or from model names.

The declaration must be a list of unique, nonempty strings safe for Open WebUI control keys: `[A-Za-z][A-Za-z0-9_-]*`, excluding `constructor` and `prototype`. Malformed lists are unknown, with a warning. Values are kept verbatim, including provider-specific levels; no eight-level fallback is used.

For duplicates, intersect every deployment's known list, preserving the first list's order. One missing, null, or invalid list makes group choices unknown and emits a warning. An explicit empty list or empty intersection establishes no common advertised choices; script-managed reasoning controls are removed. Empty lists do not prove the provider rejects the parameter.

For known chat models with unknown choices, preserve all existing Thinking and Reasoning Effort controls while applying independently known visibility/access settings. A verified nonempty list creates the existing Reasoning Effort menu/slider, replaces the script-managed Thinking control, and leaves unrelated controls intact. Pipe/direct/arena models retain the existing control eligibility restrictions. Defaults stay unset; no ON/OFF control is inferred. A declared `none` is simply an effort choice, not a separately inferred Thinking switch.

If necessary, annotate a deployment in LiteLLM:

```yaml
model_list:
  - model_name: my-public-model
    litellm_params:
      model: hosted_vllm/my-private-model
      api_base: https://my-vllm.example.com/v1
    model_info:
      mode: chat
      supported_endpoints: ["/v1/chat/completions"]
      reasoning_effort_levels: ["low", "high"]
```

Declare only values verified for that deployment and check that they appear in `/model/info`. Alternatively, use the [existing JSON metadata mode](metadata.md), which also supports explicit toggle definitions and defaults. Its omission semantics are unchanged: it is authoritative, so omitted controls can remove previously managed controls.

## Official references checked

- [LiteLLM model management](https://docs.litellm.ai/docs/proxy/model_management): `/model/info`, arbitrary `model_info` annotations, public versus provider model names, and informational endpoint metadata.
- [LiteLLM reasoning declarations](https://docs.litellm.ai/docs/reasoning_content#round-reasoning_effort-to-the-levels-a-deployment-accepts): explicit per-deployment `reasoning_effort_levels`, documented since v1.108.0. Older installations may not provide these declarations. Declared values can also affect proxy effort rounding; the script only advertises the declared values.
- [LiteLLM Responses API](https://docs.litellm.ai/docs/response_api): Chat Completions bridging based on model mode.
- [LiteLLM source: proxy_server.py](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/proxy_server.py): `model_info_v1`, deployment grouping and the `data` response envelope.
- [LiteLLM source: main.py](https://github.com/BerriAI/litellm/blob/main/litellm/main.py): Responses bridging and provider-specific text-completion adapters. Completion mode alone does not establish universal Chat Completions compatibility.

These fields describe configuration, not a live inference probe. Provider support, proxy versions, Open WebUI versions, and manual declarations can still differ from runtime behavior. Review the dry-run output before applying.
