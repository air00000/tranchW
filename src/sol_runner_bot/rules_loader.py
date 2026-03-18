from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


class RulesetError(RuntimeError):
    pass


class RulesetLoader:
    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        path = Path(path)
        if not path.exists():
            raise RulesetError(f"Ruleset not found: {path}")

        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            data = yaml.safe_load(text)
        elif path.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = yaml.safe_load(text)

        if not isinstance(data, dict):
            raise RulesetError("Ruleset root must be a mapping/object")
        required = ["ruleset_version", "hard_veto", "candidate_prefilters", "score_model", "operational_policy"]
        missing = [key for key in required if key not in data]
        if missing:
            raise RulesetError(f"Ruleset missing keys: {', '.join(missing)}")
        return data
