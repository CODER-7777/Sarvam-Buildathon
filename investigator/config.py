import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


def read_env(path=ROOT / ".env"):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() and not key.lstrip().startswith("#"):
                values[key.strip()] = value.strip().strip("\"'")
    return values


@dataclass
class Config:
    values: dict
    runs: Path = ROOT / "runs"

    @classmethod
    def load(cls):
        return cls({**read_env(), **os.environ})

    def get(self, key, default=""):
        return str(self.values.get(key, default)).strip()

    def number(self, key, default, low, high):
        try:
            return min(high, max(low, float(self.get(key, str(default)))))
        except ValueError:
            return default

    @property
    def timeout(self):
        return self.number("REQUEST_TIMEOUT", 20, 1, 60)

    @property
    def threshold(self):
        return self.number("CONFIDENCE_THRESHOLD", .75, .5, 1)

    def configured(self, name):
        required = {
            "sarvam": ["SARVAM_API_KEY"], "repository": ["REPO_ROOT"],
            "posthog": ["POSTHOG_API_KEY", "POSTHOG_PROJECT_ID"],
            "sentry": ["SENTRY_TOKEN", "SENTRY_ORG", "SENTRY_PROJECT"],
            "linear": ["LINEAR_API_KEY"],
        }
        return all(self.get(key) for key in required[name])

    def statuses(self):
        result = {name: {"state": "configured" if self.configured(name) else "not_configured"}
                  for name in ("sarvam", "repository", "posthog", "sentry", "linear")}
        if self.get("SARVAM_ENABLED", "false").lower() != "true":
            result["sarvam"]["state"] = "disabled"
        result["slack"] = {"state": "not_implemented"}
        return result

    def host(self, key, default):
        value = self.get(key, default).rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"{key} must be an HTTPS service URL without credentials.")
        return value
