import hashlib
import json
import os
from pathlib import Path

from rassi.paths import CACHE_DIR


def image_sha256(png_bytes: bytes) -> str:
    return hashlib.sha256(png_bytes).hexdigest()


def cache_key(png_bytes: bytes, model: str, prompt_version: str, schema_version: str) -> str:
    material = "|".join([image_sha256(png_bytes), model, prompt_version, schema_version])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, directory: Path = CACHE_DIR):
        self.directory = directory

    def path_for(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def contains(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def load(self, key: str) -> dict | None:
        path = self.path_for(key)
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def store(self, key: str, entry: dict) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(".json.tmp")
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(entry, handle, indent=2)
        os.replace(temporary_path, path)

    def observed_usage_for_model(self, model: str) -> list[dict]:
        if not self.directory.is_dir():
            return []
        usages = []
        for path in self.directory.glob("*/*.json"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    entry = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            if entry.get("model") == model and entry.get("usage"):
                usages.append(entry["usage"])
        return usages
