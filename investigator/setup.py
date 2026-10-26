from getpass import getpass

from .config import ROOT, read_env


def wizard():
    path = ROOT / ".env"
    existing = read_env(path)
    fields = [
        ("SARVAM_API_KEY", "Sarvam API key", True),
        ("SARVAM_ENABLED", "Enable live Sarvam requests? true/false", False),
        ("SARVAM_MODEL", "Sarvam model", False), ("REPO_ROOT", "Authorized repository absolute path", False),
        ("POSTHOG_API_KEY", "PostHog personal API key (optional)", True),
        ("POSTHOG_PROJECT_ID", "PostHog project ID", False), ("POSTHOG_HOST", "PostHog HTTPS host", False),
        ("SENTRY_TOKEN", "Sentry token (optional)", True), ("SENTRY_ORG", "Sentry organization slug", False),
        ("SENTRY_PROJECT", "Sentry project slug", False), ("SENTRY_HOST", "Sentry HTTPS host", False),
        ("LINEAR_API_KEY", "Linear API key (optional)", True), ("LINEAR_TEAM_ID", "Linear team ID (optional for single-team workspaces)", False),
        ("CONFIDENCE_THRESHOLD", "Diagnosis threshold 0.5–1", False),
        ("REQUEST_TIMEOUT", "Request timeout in seconds", False), ("INVESTIGATION_TIMEOUT", "Investigation budget in seconds", False),
    ]
    defaults = read_env(ROOT / ".env.example")
    print("Local setup. Enter keeps an existing value. Secrets are hidden. No API requests are made.")
    for key, label, secret in fields:
        current = existing.get(key, defaults.get(key, ""))
        suffix = " [set]" if secret and current else (f" [{current}]" if current and not secret else "")
        value = (getpass if secret else input)(label + suffix + ": ").strip()
        if "\n" in value or "\r" in value:
            raise ValueError("Values must be a single line.")
        existing[key] = value or current
    path.write_text("\n".join(f"{key}={value}" for key, value in existing.items()) + "\n", encoding="utf-8")
    print("Saved .env. Start the dashboard with: py -m investigator serve")
