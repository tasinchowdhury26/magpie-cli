"""Small atomic JSON store for per-track download and metadata state."""
import hashlib
import json
import os
import re
import uuid
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def track_key(raw_input: str) -> str:
    normalized = unicodedata.normalize("NFKC", raw_input).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


class TrackDatabase:
    """Load once, then use a dict for O(1) in-memory lookups per input track."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Could not read database {self.path}: {exc}") from exc
            if not isinstance(self.data, dict) or not isinstance(self.data.get("tracks"), dict):
                raise RuntimeError(f"Database {self.path} has an invalid structure")
        else:
            self.data = {"schema_version": 1, "updated_at": utc_now(), "tracks": {}}
            self.save()

    def get(self, raw_input):
        """O(1) lookup; never scans the tracks collection."""
        return self.data["tracks"].get(track_key(raw_input))

    def put(self, raw_input, record):
        self.data["tracks"][track_key(raw_input)] = record

    def save(self):
        self.data["schema_version"] = 1
        self.data["updated_at"] = utc_now()
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(self.data, ensure_ascii=False, indent=2) + "\n"
        with temporary.open("w", encoding="utf-8") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.path)
