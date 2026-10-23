"""Provider-neutral review templates; the GUI settings store owns their configuration."""
import copy
import os
import re

import common

DEFAULT_ACTIONS = [
    {"id": "tests", "key": 1, "label": "Tests", "kind": "edit", "prompt": "Add meaningful tests for {{target}}. Cover relevant normal, boundary and failure cases using the project's existing test conventions. {{note}}"},
    {"id": "check", "key": 2, "label": "Check correctness", "kind": "read", "prompt": "Check {{target}} for correctness, edge cases and interactions with its callers. Explain concrete findings and evidence; do not change files. {{note}}"},
    {"id": "refactor", "key": 3, "label": "Refactor", "kind": "edit", "prompt": "Refactor {{target}} for clarity and maintainability while preserving behavior and the project's architecture. Make the smallest useful change and verify it. {{note}}"},
    {"id": "docs", "key": 4, "label": "Document", "kind": "edit", "prompt": "Update the relevant documentation for {{target}}. Explain its contract, purpose and non-obvious behavior in the project's existing documentation style. {{note}}"},
    {"id": "explain", "key": 5, "label": "Explain", "kind": "read", "prompt": "Explain {{target}}, why it exists, its inputs, outputs and dependencies. Use concrete examples to help the reviewer understand it. Do not change files. {{note}}"},
    {"id": "custom", "key": 6, "label": "Custom instruction", "kind": "edit", "prompt": "Follow the reviewer's specific instruction for {{target}}: {{note}}"},
]
VARIABLES = {"target", "file", "range", "side", "code", "note"}


def validate(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 9:
        raise ValueError("configure 1–9 review actions")
    ids, keys, out = set(), set(), []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"id", "key", "label", "kind", "prompt"}:
            raise ValueError("each action needs id, key, label, kind and prompt")
        ident, key, label, kind, prompt = (row[k] for k in ("id", "key", "label", "kind", "prompt"))
        if not isinstance(ident, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", ident) or ident in ids:
            raise ValueError("action IDs must be unique names")
        if type(key) is not int or not 1 <= key <= 9 or key in keys:
            raise ValueError("action shortcuts must be unique numbers 1–9")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60 or "\n" in label:
            raise ValueError("action name must be one line (1–60 characters)")
        if kind not in ("read", "edit") or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
            raise ValueError("choose read/edit and a prompt of 1–4000 characters")
        if set(re.findall(r"\{\{(\w+)\}\}", prompt)) - VARIABLES:
            raise ValueError("unknown template variable; use target, file, range, side, code or note")
        ids.add(ident)
        keys.add(key)
        out.append(dict(row, label=label.strip(), prompt=prompt.strip()))
    return out


def load():
    doc = common.read_json(os.path.join(common.PIPER_HOME, "review", "config.json"), {})
    return validate(doc.get("actions", copy.deepcopy(DEFAULT_ACTIONS)))
