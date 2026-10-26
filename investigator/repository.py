import os
import re
from pathlib import Path

SKIP = {"node_modules", ".git", ".venv", "venv", "dist", "build", "runs", "__pycache__", "fixtures", ".aws", ".ssh", ".codex", ".agents"}
EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".cs", ".rb", ".php", ".vue", ".sql"}


def resolve_repository(root, relative="."):
    if not root:
        raise ValueError("Configure REPO_ROOT to authorize repository access.")
    base = Path(root).resolve(strict=True)
    requested = Path(relative)
    if requested.is_absolute() or ".." in requested.parts or ":" in relative or "\\" in relative:
        raise ValueError("Select a relative subfolder within the configured repository.")
    target = (base / requested).resolve(strict=True)
    if not target.is_dir() or not target.is_relative_to(base):
        raise ValueError("Repository selection escapes the configured root.")
    return target


def search_repository(root, relative, query, limit=10):
    target = resolve_repository(root, relative)
    words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", query.lower()))
    words -= {"the", "and", "with", "after", "when", "this", "that", "from", "user", "error", "keeps"}
    matches, scanned = [], 0
    for directory, folders, files in os.walk(target, followlinks=False):
        folders[:] = sorted(d for d in folders if d not in SKIP and not d.startswith(".")
                            and not (Path(directory) / d).is_symlink()
                            and (Path(directory) / d).resolve().is_relative_to(target))
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or path.suffix.lower() not in EXTENSIONS or name.startswith(".") or any(s in name.lower() for s in ("secret", "credential", "private", "token")):
                continue
            if not path.resolve().is_relative_to(target):
                continue
            scanned += 1
            if scanned > 1500:
                break
            try:
                if path.stat().st_size > 200000:
                    continue
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            scores = [(sum(word in line.lower() for word in words), i) for i, line in enumerate(lines)]
            score, index = max(scores, default=(0, 0))
            if score:
                start, end = max(0, index - 5), min(len(lines), index + 12)
                content = "\n".join(f"{i + 1}: {lines[i]}" for i in range(start, end))
                location = f"{path.relative_to(target).as_posix()}:{start + 1}-{end}"
                matches.append((score, {"title": name, "content": content[:9000], "location": location}))
        if scanned > 1500:
            break
    return [item for _, item in sorted(matches, key=lambda pair: (-pair[0], pair[1]["location"]))[:limit]]
