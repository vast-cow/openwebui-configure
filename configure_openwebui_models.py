#!/usr/bin/env python3
"""Bulk-configure Open WebUI model visibility, inference controls and access.

Default: DRY RUN. --apply writes to Open WebUI.

Modes:
  legacy    Original eight-step Thinking/reasoning_effort slider on every
            supported model, plus public-read access (original behavior).
  metadata  Load metadata JSON from --metadata-json. Hide non-chat models using
            meta.hidden=true (NOT is_active=false), and build model-specific
            Thinking / Reasoning Effort controls for chat models. Only known
            chat models are granted public read. Unknown models are untouched.

A combined Thinking control uses Off / On and the supported effort levels to avoid
conflicting selections (especially if both controls use reasoning_effort).

Open WebUI requires 'menu' or 'slider' model controls, not a 'toggle'.
'Public' grants authenticated Open WebUI users read access; never anonymous.
Existing access grants are not revoked, even when a model is hidden.

The script retains the separate access-update endpoint because model imports
are not reliable for changing public grants of existing saved models.
"""

import argparse
import json
import os
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import requests

LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
PUBLIC_READ = {
    "principal_type": "user",
    "principal_id": "*",
    "permission": "read",
}
RECORD_FIELDS = (
    "id", "name", "base_model_id", "meta", "params", "access_grants", "is_active",
)
MANAGED_CONTROLS = ("thinking", "reasoning_effort")


def request_json(session, base_url, method, path, **kwargs):
    response = session.request(method, f"{base_url}{path}", timeout=120, **kwargs)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"{method} {path}: HTTP {response.status_code}: {response.text[:1000]}"
        ) from exc
    return response.json()


def write_json(path, data):
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def public_read_enabled(grants):
    return any(
        isinstance(g, dict)
        and g.get("principal_type") == "user"
        and g.get("principal_id") == "*"
        and g.get("permission") == "read"
        for g in (grants or [])
    )


def supports_controls(model):
    return (
        isinstance(model, dict)
        and "pipe" not in model
        and not model.get("direct")
        and model.get("owned_by") != "arena"
    )


def controls_equal(actual, expected):
    """Compare control settings despite optional Pydantic fields being omitted."""
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    fields = ("label", "description", "default", "options")
    return all(actual.get(field) == expected.get(field) for field in fields) and (
        actual.get("display", "menu") == expected.get("display", "menu")
    )


def make_legacy_control(ultra_value):
    # Retain the exact control serialized by the original script.
    return {
        "label": "Thinking",
        "description": "Reasoning effort",
        "display": "slider",
        "default": None,
        "options": {
            level: {
                "label": level,
                "params": {"reasoning_effort": ultra_value if level == "ultra" else level},
            }
            for level in LEVELS
        },
    }


def saved_and_available(saved, available):
    saved_by_id = {m["id"]: m for m in saved if isinstance(m, dict) and m.get("id")}
    available_by_id = {
        m["id"]: m for m in available if isinstance(m, dict) and m.get("id")
    }
    return saved_by_id, available_by_id


def make_record(model_id, saved_by_id, available_by_id):
    old = saved_by_id.get(model_id)
    if old is not None:
        return {
            key: deepcopy(old[key]) for key in RECORD_FIELDS if key in old
        }
    model = available_by_id[model_id]
    return {
        "id": model_id,
        "name": model.get("name") or model_id,
        "base_model_id": None,
        "meta": {},
        "params": {},
        "access_grants": [],
        "is_active": True,
    }


def empty_plan(saved_by_id, available_by_id):
    return {
        "saved_by_id": saved_by_id,
        "available_by_id": available_by_id,
        "imports": [],
        "public_ids": [],
        "controlled_ids": [],
        "changes_by_id": {},
        "verify_controls": {},
        "verify_hidden": {},
        "warnings": [],
        "skipped_ids": [],
    }


def make_plan_legacy(saved, available, control):
    """Preserve the original all-model public-read + eight-step slider policy."""
    saved_by_id, available_by_id = saved_and_available(saved, available)
    plan = empty_plan(saved_by_id, available_by_id)
    all_ids = set(saved_by_id)
    all_ids.update(
        model_id for model_id, model in available_by_id.items()
        if model_id not in saved_by_id
        and supports_controls(model)
        and not model.get("preset")
    )

    for model_id in sorted(all_ids):
        record = make_record(model_id, saved_by_id, available_by_id)
        old = saved_by_id.get(model_id)
        changes = ["register"] if old is None else []
        if not public_read_enabled(record.get("access_grants")):
            plan["public_ids"].append(model_id)
            changes.append("public-read")

        upstream_id = record.get("base_model_id") or model_id
        if supports_controls(available_by_id.get(upstream_id)):
            plan["controlled_ids"].append(model_id)
            params = deepcopy(record.get("params") or {})
            controls = deepcopy(params.get("model_controls") or {})
            if not controls_equal(controls.get("thinking"), control):
                controls["thinking"] = deepcopy(control)
                params["model_controls"] = controls
                record["params"] = params
                changes.append("thinking-slider")
                plan["verify_controls"][model_id] = {"thinking": deepcopy(control)}

        if old is None or "thinking-slider" in changes:
            plan["imports"].append(record)
        if changes:
            plan["changes_by_id"][model_id] = changes

    return plan


def parse_metadata(path):
    with Path(path).open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise ValueError("Metadata JSON must contain an object: {'models': {model_id: {...}}}")
    for model_id, entry in data["models"].items():
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("Metadata contains an empty model ID")
        if not isinstance(entry, dict):
            raise ValueError(f"{model_id}: model entry must be an object")
        chat = entry.get("chat_supported")
        if chat is not None and type(chat) is not bool:
            raise ValueError(f"{model_id}: chat_supported must be true, false or null")
        thinking = entry.get("thinking") or {}
        effort = entry.get("reasoning_effort") or {}
        if not isinstance(thinking, dict) or not isinstance(effort, dict):
            raise ValueError(f"{model_id}: thinking and reasoning_effort must be objects")
        if thinking.get("supported") is not None and type(thinking["supported"]) is not bool:
            raise ValueError(f"{model_id}: thinking.supported must be true, false or null")
        if thinking.get("default") is not None and type(thinking["default"]) is not bool:
            raise ValueError(f"{model_id}: thinking.default must be true, false or null")
        toggle = thinking.get("toggle")
        if toggle is not None:
            if not isinstance(toggle, dict) or not isinstance(toggle.get("parameter"), str):
                raise ValueError(f"{model_id}: thinking.toggle.parameter must be a string")
            for value in ("on", "off"):
                if value not in toggle:
                    raise ValueError(f"{model_id}: thinking.toggle.{value} is missing")
        options = effort.get("options")
        if options is not None:
            if (
                not isinstance(options, list)
                or any(not isinstance(x, str) or not x for x in options)
                or len(set(options)) != len(options)
            ):
                raise ValueError(f"{model_id}: reasoning_effort.options must be a list of unique strings or null")
            if options and not isinstance(effort.get("parameter"), str):
                raise ValueError(f"{model_id}: reasoning_effort.parameter is required when options exist")
        if options is not None:
            for option in options:
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", option) or option in {"constructor", "prototype"}:
                    raise ValueError(f"{model_id}: invalid model control key: {option!r}")
        if effort.get("default") is not None and effort["default"] not in (options or []):
            raise ValueError(f"{model_id}: reasoning_effort.default must appear in options")
    return data["models"]


def parameter_tree(path, value):
    """Turn metadata's dotted name into nested Custom Params for Open WebUI.

    An unqualified thinking_budget is interpreted as the chat-template option
    used by Seed-OSS; no new top-level vLLM parameter is invented.
    """
    if path == "thinking_budget":
        path = "chat_template_kwargs.thinking_budget"
    if not isinstance(path, str) or not path:
        raise ValueError("A control parameter must be a non-empty string")
    parts = path.split(".")
    if any(not part or part in {"__proto__", "prototype", "constructor"} for part in parts):
        raise ValueError(f"Invalid dotted parameter name: {path!r}")
    result = value
    for part in reversed(parts):
        result = {part: result}
    return result


def deep_merge(left, right):
    out = deepcopy(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def is_legacy_slider(control):
    return (
        isinstance(control, dict)
        and control.get("label") == "Thinking"
        and control.get("description") == "Reasoning effort"
        and isinstance(control.get("options"), dict)
        and set(control["options"]) == set(LEVELS)
    )


def is_managed_control(control):
    """Recognize only controls created by this script; preserve others."""
    if is_legacy_slider(control):
        return True
    return (
        isinstance(control, dict)
        and (
            (control.get("label") == "Thinking" and control.get("description") in {
                "Thinking ON/OFF and reasoning effort", "Thinking ON/OFF"
            })
            or (control.get("label") == "Reasoning Effort" and control.get("description") == "Reasoning effort")
        )
    )


def build_metadata_controls(entry, model_id, seed_on_budget=2048):
    """Return (managed controls, warnings).

    One combined control is intentional: independent Thinking and Effort
    selectors can produce mutually inconsistent request parameters, so
    Off and On options share one slider with the supported effort levels.
    """
    thinking = entry.get("thinking") or {}
    effort = entry.get("reasoning_effort") or {}
    toggle = thinking.get("toggle")
    options = effort.get("options") or []
    effort_parameter = effort.get("parameter")
    notes = []

    can_toggle = thinking.get("supported") is True and isinstance(toggle, dict)
    if thinking.get("supported") is True and not can_toggle:
        notes.append(f"{model_id}: thinking.supported=true, but toggle definition absent; no ON/OFF option")

    if not can_toggle and not options:
        return {}, notes

    choices = {}
    if can_toggle:
        off_tree = parameter_tree(toggle["parameter"], toggle["off"])
        choices["off"] = {"label": "Off", "params": off_tree}

    if can_toggle and options:
        on_value = toggle["on"]
        if on_value == "positive_integer_budget":
            on_value = seed_on_budget
            notes.append(f"{model_id}: substituted thinking_budget={seed_on_budget} for placeholder 'positive_integer_budget'")
        choices["on"] = {
            "label": "On",
            "params": parameter_tree(toggle["parameter"], on_value),
        }

    if options:
        for level in options:
            key = f"effort_{level}" if can_toggle and level in {"on", "off"} else level
            value = (toggle.get("on") if can_toggle else None)
            if value == "positive_integer_budget":
                value = seed_on_budget
                notes.append(f"{model_id}: substituted thinking_budget={seed_on_budget} for placeholder 'positive_integer_budget'")
            on_tree = (parameter_tree(toggle["parameter"], value) if can_toggle else {})
            # The effort value intentionally takes precedence if both share a
            # parameter path. The Off choice never sends an effort value.
            params = deep_merge(on_tree, parameter_tree(effort_parameter, level))
            choices[key] = {"label": level, "params": params}
    elif can_toggle:
        value = toggle["on"]
        if value == "positive_integer_budget":
            value = seed_on_budget
            notes.append(f"{model_id}: substituted thinking_budget= {seed_on_budget} for placeholder 'positive_integer_budget'")
        choices["on"] = {"label": "On", "params": parameter_tree(toggle["parameter"], value)}

    if not choices:
        return {}, notes

    thinking_default = thinking.get("default")
    effort_default = effort.get("default")
    if can_toggle and thinking_default is False:
        selected = "off"
    elif can_toggle and thinking_default is True:
        chosen = f"effort_{effort_default}" if effort_default in {"on", "off"} else effort_default
        selected = chosen if chosen in choices else "on"
    elif not can_toggle and effort_default in choices:
        selected = effort_default
    else:
        # Unknown native defaults: do not force a new inference parameter.
        selected = None

    return {
        ("thinking" if can_toggle else "reasoning_effort"): {
            "label": "Thinking" if can_toggle else "Reasoning Effort",
            "description": (
                "Thinking ON/OFF and reasoning effort" if can_toggle and options
                else "Thinking ON/OFF" if can_toggle else "Reasoning effort"
            ),
            "display": "slider" if len(choices) >= 3 else "menu",
            "default": selected,
            "options": choices,
        }
    }, notes


def make_plan_metadata(saved, available, metadata, *, unhide_chat=False, seed_on_budget=2048):
    saved_by_id, available_by_id = saved_and_available(saved, available)
    plan = empty_plan(saved_by_id, available_by_id)
    all_ids = set(saved_by_id)
    all_ids.update(
        model_id for model_id, model in available_by_id.items()
        if model_id not in saved_by_id and not model.get("preset")
    )

    for model_id in sorted(all_ids):
        old = saved_by_id.get(model_id)
        record = make_record(model_id, saved_by_id, available_by_id)
        upstream_id = record.get("base_model_id") or model_id
        # A preset can override a model by name; metadata belongs primarily to
        # its upstream vLLM model, not to its Open WebUI preset ID.
        entry = metadata.get(upstream_id) or metadata.get(model_id)
        if entry is None or entry.get("chat_supported") is None:
            plan["skipped_ids"].append(model_id)
            continue
        chat = entry["chat_supported"]
        changes = ["register"] if old is None else []
        meta = deepcopy(record.get("meta") or {})
        params = deepcopy(record.get("params") or {})
        controls = deepcopy(params.get("model_controls") or {})
        existing_control = controls.get("thinking")

        if chat is False:
            if meta.get("hidden") is not True:
                meta["hidden"] = True
                record["meta"] = meta
                changes.append("hide-nonchat")
                plan["verify_hidden"][model_id] = True
            # Never add public-read access to a non-chat model in metadata mode.
            # Existing public grants are intentionally preserved, not revoked.
            for key in MANAGED_CONTROLS:
                if is_managed_control(controls.get(key)):
                    controls.pop(key)
                    params["model_controls"] = controls
                    record["params"] = params
                    changes.append(f"remove-{key}-control")
                    plan["verify_controls"].setdefault(model_id, {})[key] = None
        else:
            if unhide_chat and meta.get("hidden") is True:
                meta["hidden"] = False
                record["meta"] = meta
                changes.append("show-chat")
                plan["verify_hidden"][model_id] = False
            if not public_read_enabled(record.get("access_grants")):
                plan["public_ids"].append(model_id)
                changes.append("public-read")

            if supports_controls(available_by_id.get(upstream_id)):
                target, notes = build_metadata_controls(
                    entry, model_id, seed_on_budget=seed_on_budget
                )
                plan["warnings"].extend(notes)
                plan["controlled_ids"].append(model_id)
                for key, desired in target.items():
                    if not controls_equal(controls.get(key), desired):
                        controls[key] = desired
                        params["model_controls"] = controls
                        record["params"] = params
                        changes.append(f"{key}-options")
                        plan["verify_controls"].setdefault(model_id, {})[key] = desired
                for key in MANAGED_CONTROLS:
                    if key not in target and is_managed_control(controls.get(key)):
                        controls.pop(key)
                        params["model_controls"] = controls
                        record["params"] = params
                        changes.append(f"remove-{key}-control")
                        plan["verify_controls"].setdefault(model_id, {})[key] = None
            elif entry.get("thinking", {}).get("supported") is True or (
                entry.get("reasoning_effort", {}).get("options") or []
            ):
                plan["warnings"].append(f"{model_id}: cannot configure model controls (unsupported or missing upstream model)")

        if old is None or any(
            change in changes for change in
            ("hide-nonchat", "show-chat", "thinking-options", "reasoning_effort-options",
             "remove-thinking-control", "remove-reasoning_effort-control")
        ):
            plan["imports"].append(record)
        if changes:
            plan["changes_by_id"][model_id] = changes

    return plan


def verify_config(records, plan):
    by_id = {m["id"]: m for m in records if isinstance(m, dict) and m.get("id")}
    errors = []
    for record in plan["imports"]:
        model_id = record["id"]
        current = by_id.get(model_id)
        if current is None:
            errors.append(f"{model_id}: missing after model import")
            continue
        if model_id in plan["verify_hidden"]:
            if (current.get("meta") or {}).get("hidden") is not plan["verify_hidden"][model_id]:
                errors.append(f"{model_id}: meta.hidden was not saved")
        # Hidden is a UI setting. Never silently disable the model.
        if current.get("is_active") != record.get("is_active"):
            errors.append(f"{model_id}: is_active unexpectedly changed")
        for key, expected in plan["verify_controls"].get(model_id, {}).items():
            actual = ((current.get("params") or {}).get("model_controls") or {}).get(key)
            if expected is None:
                if actual is not None:
                    errors.append(f"{model_id}: {key} control was not removed")
            elif not controls_equal(actual, expected):
                errors.append(f"{model_id}: {key} control not saved correctly")
    return errors


def execute_plan(session, base_url, plan):
    if plan["imports"]:
        print(f"Importing {len(plan['imports'])} model configurations ...")
        result = request_json(
            session, base_url, "POST", "/api/v1/models/import",
            json={"models": plan["imports"]},
        )
        if result is not True:
            raise RuntimeError(f"Model import returned unexpected response: {result!r}")

    after_import = request_json(session, base_url, "GET", "/api/v1/models/all")
    errors = verify_config(after_import, plan)
    if errors:
        raise RuntimeError("Model import verification failed:\n - " + "\n - ".join(errors))

    after_by_id = {m["id"]: m for m in after_import}
    updated_grants = 0
    for model_id in plan["public_ids"]:
        current = after_by_id.get(model_id)
        if current is None:
            errors.append(f"{model_id}: model missing; cannot publish")
            continue
        current_grants = deepcopy(current.get("access_grants") or [])
        if public_read_enabled(current_grants):
            continue
        current_grants.append(deepcopy(PUBLIC_READ))
        try:
            result = request_json(
                session, base_url, "POST", "/api/v1/models/model/access/update",
                json={
                    "id": model_id,
                    "name": current.get("name") or model_id,
                    "access_grants": current_grants,
                },
            )
            if not isinstance(result, dict) or not public_read_enabled(result.get("access_grants")):
                errors.append(f"{model_id}: public access not confirmed by update response")
                continue
            updated_grants += 1
            print(f"  published: {model_id}")
        except (RuntimeError, requests.RequestException, ValueError) as exc:
            errors.append(f"{model_id}: {exc}")

    final_records = request_json(session, base_url, "GET", "/api/v1/models/all")
    errors.extend(verify_config(final_records, plan))
    final_by_id = {m["id"]: m for m in final_records}
    for model_id in plan["public_ids"]:
        if not public_read_enabled((final_by_id.get(model_id) or {}).get("access_grants")):
            errors.append(f"{model_id}: public read not saved")
    if errors:
        raise RuntimeError("Verification issues:\n - " + "\n - ".join(errors))

    print(
        f"Success. Public grants updated: {updated_grants}; "
        f"Model configurations verified: {len(plan['imports'])}."
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Manage Open WebUI models: original eight-level slider or metadata-aware configuration."
    )
    parser.add_argument(
        "--mode", choices=("legacy", "metadata"), default="legacy",
        help="legacy is the original behavior (default); metadata reads --metadata-json.",
    )
    parser.add_argument(
        "--metadata-json", "--metadata", dest="metadata_json", metavar="PATH",
        help="Model capabilities JSON, required in --mode metadata.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply changes. Without this option, only display the plan and save backups.",
    )
    parser.add_argument(
        "--ultra-value", choices=("ultra", "max"), default="ultra",
        help="Legacy only: value sent for Ultra (default: ultra).",
    )
    parser.add_argument(
        "--unhide-chat-models", action="store_true",
        help="Metadata only: explicitly unhide known chat models (otherwise preserve manual hiding).",
    )
    parser.add_argument(
        "--seed-on-budget", type=int, default=2048,
        help="Metadata only: positive thinking budget when an older JSON uses the placeholder positive_integer_budget.",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("."),
        help="Directory for plan and timestamped backup (default: working directory).",
    )
    args = parser.parse_args(argv)

    if args.mode == "metadata" and not args.metadata_json:
        parser.error("--mode metadata requires --metadata-json PATH")
    if args.mode == "legacy" and args.metadata_json:
        parser.error("--metadata-json is only valid with --mode metadata")
    if args.seed_on_budget <= 0:
        parser.error("--seed-on-budget must be positive")

    metadata = parse_metadata(args.metadata_json) if args.mode == "metadata" else None
    base_url = os.environ.get("OPEN_WEBUI_URL", "").rstrip("/")
    token = os.environ.get("OPEN_WEBUI_API_KEY", "")
    if not base_url or not token:
        parser.error("Set OPEN_WEBUI_URL and OPEN_WEBUI_API_KEY")

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    saved = request_json(session, base_url, "GET", "/api/v1/models/all")
    raw_available = request_json(session, base_url, "GET", "/api/models")
    available = raw_available.get("data") if isinstance(raw_available, dict) else raw_available
    if not isinstance(saved, list) or not isinstance(available, list):
        raise RuntimeError("Unexpected model list response format")

    if args.mode == "legacy":
        plan = make_plan_legacy(saved, available, make_legacy_control(args.ultra_value))
    else:
        plan = make_plan_metadata(
            saved, available, metadata,
            unhide_chat=args.unhide_chat_models,
            seed_on_budget=args.seed_on_budget,
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f-UTC")
    backup_path = args.output_dir / f"models-backup-{stamp}.json"
    plan_path = args.output_dir / "model-controls-bulk.json"
    write_json(backup_path, saved)
    write_json(plan_path, {"models": plan["imports"]})
    print(f"Mode: {args.mode}")
    print(f"Current model records backed up to: {backup_path}")
    print(f"Model import plan saved to: {plan_path}")
    print(f"Saved model records: {len(plan['saved_by_id'])}")
    print(f"Upstream/available models: {len(plan['available_by_id'])}")
    print(f"Models requiring changes: {len(plan['changes_by_id'])}")
    print(f"Model imports required: {len(plan['imports'])}")
    print(f"Public grants required: {len(plan['public_ids'])}")
    if args.mode == "legacy":
        print("Thinking steps: " + " → ".join(LEVELS))
    else:
        print(f"Unmatched/unknown models left unchanged: {len(plan['skipped_ids'])}")
        for model_id in plan["skipped_ids"]:
            print(f"  skipped: {model_id}")
    for warning in plan["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    for model_id, changes in plan["changes_by_id"].items():
        print(f"  {model_id}: {', '.join(changes)}")

    if not args.apply:
        print("DRY RUN: no changes submitted. Use --apply to update Open WebUI.")
        return
    if not plan["changes_by_id"]:
        print("No changes necessary.")
        return
    execute_plan(session, base_url, plan)


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
