import json
import re
import threading
from pathlib import Path


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def path(self, run_id):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid investigation ID.")
        return self.directory / f"{run_id}.json"

    def save(self, run):
        with self.lock:
            path = self.path(run["id"])
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)

    def get(self, run_id):
        with self.lock:
            return json.loads(self.path(run_id).read_text(encoding="utf-8"))

    def list(self):
        records = []
        with self.lock:
            paths = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
            for path in paths:
                try:
                    run = json.loads(path.read_text(encoding="utf-8"))
                    records.append({key: run.get(key) for key in ("id", "created_at", "duration", "status", "stage", "report", "diagnosis", "linear")})
                except (ValueError, OSError):
                    continue
        return records


def markdown_report(run):
    diagnosis = run["diagnosis"]
    if not diagnosis:
        raise ValueError("The investigation is still running.")
    lines = [f"# {run['report']['text']}", f"Mode: {run['report']['mode']} | Outcome: {diagnosis['outcome']}",
             f"Confidence estimate: {diagnosis['confidence']:.0%}", "", "## Inference", diagnosis["root_cause"], diagnosis["summary"],
             "", "## Evidence"]
    for evidence in run["evidence"]:
        lines += [f"### {evidence['id']} — {evidence['title']} ({evidence['origin']})", evidence["location"], "```", evidence["content"], "```"]
    lines += ["", "## Observed excerpts (verbatim; source truth is not independently verified)"]
    for observation in run.get("observations", []):
        lines += [f"- {observation['evidence_id']} ({observation['source']}, {observation['origin']}): {observation['quote']}"]
    lines += ["", "## Validated citations"]
    lines += [f"- {c['evidence_id']}: {c['quote']}" for c in diagnosis["citations"]]
    lines += ["", "## Recommended next steps"] + [f"- {s}" for s in diagnosis["next_steps"]]
    lines += ["", "## Missing information"] + [f"- {s}" for s in diagnosis["missing"]]
    lines += ["", f"Local investigation: {run['id']}", "Synthetic demo evidence is not evidence from a live service." if run["report"]["mode"] == "demo" else "Model conclusions are inferences. Evidence excerpts are redacted tool outputs."]
    return "\n".join(lines)
