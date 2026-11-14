# Open WebUI Model Configuration

Bulk-configure model visibility, Thinking/Reasoning Effort controls, and authenticated-user public read access. Requires Python 3.9+ and `requests` (`pip install requests`).

```bash
export OPEN_WEBUI_URL='https://your-webui.example.com'
export OPEN_WEBUI_API_KEY='YOUR_ADMIN_API_KEY'

# Default: legacy eight-level Thinking slider and public read grants.
python3 configure_openwebui_models.py

# Explicit JSON capabilities:
python3 configure_openwebui_models.py --mode metadata \
  --metadata-json vllm_model_capabilities.example.json

# Optional LiteLLM discovery (separate credential with /model/info access):
export LITELLM_URL='https://your-litellm.example.com'
export LITELLM_API_KEY='YOUR_LITELLM_KEY'
python3 configure_openwebui_models.py --mode litellm          # dry run
python3 configure_openwebui_models.py --mode litellm --apply  # apply
```

`LITELLM_URL` is the proxy root, without `/v1` or `/model/info`; a reverse-proxy prefix is allowed, e.g. `https://example.com/litellm`. Discovery requests append `/model/info`. Only `--mode litellm` contacts LiteLLM, using its own HTTP session and Bearer credential. Discovery failures stop execution without changing Open WebUI; there is no fallback.

LiteLLM discovery uses public `model_name`, `model_info.mode`, `model_info.supported_endpoints`, and explicit `model_info.reasoning_effort_levels`. Presets match through `base_model_id`. Duplicate deployments must agree on chat capability; effort choices use their intersection only if every deployment supplies a valid list.

Missing effort choices preserve existing reasoning controls. `supports_reasoning` and `supported_openai_params` cannot establish choices. Supply verified `model_info.reasoning_effort_levels` in LiteLLM or use JSON metadata mode. No legacy effort fallback, inferred ON/OFF toggle, or default is added. Unknown chat models stay unchanged; discovery is metadata-based and does not test inference. See [classification rules, metadata example, sources, and limitations](docs/litellm.md).

Dry runs write a timestamped backup and plan but make no API writes. Writes require `--apply` and are verified afterward. Known non-chat models are hidden using `meta.hidden`, preserving `is_active` and grants. Manual hiding is preserved unless `--unhide-chat-models` is supplied. Use `--output-dir DIR` to select the backup/plan directory.

See [JSON metadata, existing modes, and advanced options](docs/metadata.md).

```bash
python3 -m unittest -v
```
