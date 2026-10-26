import re


def redact(text, secrets=()):
    text = str(text)
    for secret in sorted((s for s in secrets if len(s) >= 5), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[\w.\-/+=]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token|password|secret|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s,\"'}]+", r"\1[REDACTED]", text)
    text = re.sub(r"\b(?:sk_|sk-|phx_|lin_api_|xoxb-)[A-Za-z0-9_-]+", "[REDACTED]", text)
    return text


def secret_values(config):
    return [str(value) for key, value in config.values.items()
            if any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")) and value]
