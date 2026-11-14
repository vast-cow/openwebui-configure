# JSON Metadata and Legacy Modes

For optional proxy discovery, see [LiteLLM mode](litellm.md). Commands below run from the repository root.

This script uses the Open WebUI admin API to configure model display settings, Thinking/Reasoning Effort, and public read permissions in bulk.

## Required Files

- `configure_openwebui_models.py`: The main script. Requires the `requests` library (`pip install requests`).
- `vllm_model_capabilities.example.json`: **Example metadata** for 36 models.  Unconfirmed values should be `null`. Adjust to match the actual vLLM environment and chat templates of your models.
- `test_configure_openwebui_models.py`: Offline unit tests.

## How to Run

Set the Open WebUI admin API key and URL as environment variables:

```bash
export OPEN_WEBUI_URL='https://your-webui.example.com'
export OPEN_WEBUI_API_KEY='YOUR_ADMIN_API_KEY'
```

**JSON Mode** (start with a dry run):

```bash
python3 configure_openwebui_models.py \
  --mode metadata \
  --metadata-json vllm_model_capabilities.example.json
```

Review the dry-run output. If everything looks correct, add the `--apply` flag.

```bash
python3 configure_openwebui_models.py \
  --mode metadata \
  --metadata-json vllm_model_capabilities.example.json \
  --apply
```

**Legacy Mode** (without using JSON, applies an 8-level Thinking slider and public read permissions to all target models):

```bash
python3 configure_openwebui_models.py --mode legacy             # dry-run
python3 configure_openwebui_models.py --mode legacy --apply     # apply
```

The legacy `--ultra-value max` option can also be used.

## JSON Format

Place the vLLM model IDs as keys under the `models` section. Open WebUI presets that have a `base_model_id` will, in principle, inherit the settings of the upstream model.

```json
{
  "schema_version": "1.0",
  "runtime": "vllm",
  "models": {
    "Qwen/Qwen3.8-27B": {
      "chat_supported": true,
      "thinking": {
        "supported": true,
        "toggle": {
          "parameter": "chat_template_kwargs.enable_thinking",
          "on": true,
          "off": false
        },
        "default": true
      },
      "reasoning_effort": {
        "parameter": "reasoning_effort",
        "options": ["low", "medium", "xhigh"],
        "default": "xhigh"
      }
    }
  }
}
```

The above model will have a single Thinking control with options: Off / On / low / medium / xhigh. Off sets `chat_template_kwargs.enable_thinking=false`, On sets `true`, and selecting each effort level adds the corresponding `reasoning_effort` value in addition to enabling Thinking. This avoids the problem of double-sending the same API parameter for Thinking and Effort; therefore, it does not use separate controls.

- `chat_supported: false`: Hides the model from the chat model selection list using `meta.hidden = true`.  `is_active` is not changed, and existing public permissions are not revoked. No new public permissions are granted.
- `chat_supported: true`: Configures controls specific to the model. Grants public read permissions if they do not already exist. Existing manual hiding is preserved.
- `chat_supported: null` or the model is not in the JSON: **No changes are made.**
- If both `thinking.supported: true` and `thinking.toggle` are present: An ON/OFF toggle is generated.
- If `reasoning_effort.options` is a non-empty array: Only the options within the array are generated.
- If `thinking.default` or `reasoning_effort.default` is `null`: No default value is arbitrarily set.
- No reasoning controls are configured for pipe/direct/arena models that Open WebUI considers to be incompatible with controls.

`toggle.parameter` and `reasoning_effort.parameter` can specify either a root parameter (e.g., `reasoning_effort`) or a dot-separated path (e.g., `chat_template_kwargs.enable_thinking`). Dot-separated paths are expanded as nested objects in the parameter dictionary passed to Open WebUI. As an exception, the old JSON `thinking_budget` is treated as `chat_template_kwargs.thinking_budget`. If `toggle.on` is the old placeholder `positive_integer_budget`, it is converted to a default of 2048 (can be changed with `--seed-on-budget 4096`).

## Options and Safety

- `--unhide-chat-models`: Re-displays models that are explicitly set to `chat_supported: true`. Usually, existing manual hiding is preserved.
- `--output-dir DIR`: Specifies the directory for saving backups (`models-backup-*.json`) and the imported configuration (`model-controls-bulk.json`).
- If `--apply` is not used, no changes are sent to the API. A dry run will still write out the backup and plan JSON.
- After applying, the changes are verified by retrieving the models again from `/api/v1/models/all` and checking the hidden status, controls, and public read permissions.
- Granting public read permissions uses a different API, `/api/v1/models/model/access/update`, and not `/api/v1/models/import`.
- The script does **not verify** the vLLM compatibility of the models themselves, the supported chat templates, or the actual Thinking behavior during real requests because these modes do not probe the upstream inference server.

## Testing

```bash
python3 -m unittest -v test_configure_openwebui_models.py
```
