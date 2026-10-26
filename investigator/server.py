import json
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from pydantic import ValidationError

from .clients import ServiceError, create_linear_issue
from .config import ROOT
from .engine import SAMPLES, investigate, new_run
from .models import BugReport, abstain
from .security import redact, secret_values
from .storage import Store, markdown_report


def make_server(config, port=8000):
    store = Store(config.runs)
    csrf = secrets.token_urlsafe(32)
    capacity = threading.BoundedSemaphore(2)
    issue_lock = threading.Lock()
    for old in store.list():
        if old["status"] == "running":
            run = store.get(old["id"])
            run.update(status="failed", stage="Interrupted", diagnosis=abstain("The server restarted before this investigation completed.").model_dump())
            store.save(run)

    class Handler(BaseHTTPRequestHandler):
        server: ThreadingHTTPServer

        def log_message(self, *_):
            pass  # Never log submitted reports, keys, or external responses.

        def allowed_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def send(self, code, body, content_type="application/json; charset=utf-8"):
            payload = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, (dict, list)) else body
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, {"error": "Unrecognized host."})
            path = urlparse(self.path).path
            try:
                if path == "/api/config":
                    return self.send(200, {"csrf": csrf, "integrations": config.statuses(), "samples": SAMPLES,
                                           "model": config.get("SARVAM_MODEL", "sarvam-105b"),
                                           "repository": "Configured root" if config.configured("repository") else "Not configured"})
                if path == "/api/runs":
                    return self.send(200, store.list())
                if path.startswith("/api/runs/"):
                    run_id = path.split("/")[3]
                    run = store.get(run_id)
                    if path == f"/api/runs/{run_id}/markdown":
                        return self.send(200, markdown_report(run).encode(), "text/markdown; charset=utf-8")
                    if path != f"/api/runs/{run_id}":
                        return self.send(404, {"error": "Not found."})
                    return self.send(200, run)
                static = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
                if path not in static:
                    return self.send(404, {"error": "Not found."})
                file = ROOT / "web" / static[path]
                return self.send(200, file.read_bytes(), (mimetypes.guess_type(file.name)[0] or "text/plain") + "; charset=utf-8")
            except (FileNotFoundError, ValueError):
                return self.send(404, {"error": "Record not found or not ready."})

        def do_POST(self):
            if not self.allowed_host() or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf):
                return self.send(403, {"error": "Refresh the page before submitting."})
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"):
                return self.send(403, {"error": "Cross-origin requests are not allowed."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 40000 or self.headers.get_content_type() != "application/json":
                    return self.send(400, {"error": "Send a JSON request under 40 KB."})
                data = json.loads(self.rfile.read(length))
                path = urlparse(self.path).path
                if path == "/api/runs":
                    report = BugReport.model_validate(data)
                    # Redact before the first on-disk save, including the queued record.
                    report = BugReport.model_validate({k: redact(v, secret_values(config)) if isinstance(v, str) else v for k, v in report.model_dump().items()})
                    if not capacity.acquire(blocking=False):
                        return self.send(429, {"error": "Two investigations are already running. Try again shortly."})
                    run = new_run(report)
                    store.save(run)

                    def work():
                        try:
                            investigate(config, report, run, store.save)
                        finally:
                            capacity.release()

                    threading.Thread(target=work, daemon=True).start()
                    return self.send(202, {"id": run["id"]})
                if path.startswith("/api/runs/") and path.endswith("/linear"):
                    if not isinstance(data, dict) or data.get("confirm") is not True:
                        return self.send(400, {"error": "Explicit issue creation confirmation is required."})
                    run_id = path.split("/")[3]
                    if path != f"/api/runs/{run_id}/linear":
                        return self.send(404, {"error": "Not found."})
                    with issue_lock:
                        run = store.get(run_id)
                        if not run["diagnosis"]:
                            return self.send(409, {"error": "Wait for the investigation to complete."})
                        if run.get("linear"):
                            return self.send(200, run["linear"])
                        if run.get("linear_attempted"):
                            return self.send(409, {"error": "An issue creation was already attempted. Check Linear before retrying manually to avoid duplicates."})
                        if not config.configured("linear"):
                            raise ServiceError("Configure LINEAR_API_KEY in .env and restart the server.")
                        run["linear_attempted"] = True
                        store.save(run)
                        run["linear"] = create_linear_issue(config, run, markdown_report(run))
                        store.save(run)
                        return self.send(201, run["linear"])
                return self.send(404, {"error": "Not found."})
            except ValidationError:
                return self.send(400, {"error": "Check the report fields and time window. Use timezone-qualified timestamps and an end after the start."})
            except ServiceError as exc:
                return self.send(502, {"error": str(exc)})
            except (ValueError, FileNotFoundError, KeyError):
                return self.send(400, {"error": "Invalid request or investigation ID."})
            except Exception:
                return self.send(500, {"error": "The request could not be completed."})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(config, port=8000):
    server = make_server(config, port)
    print(f"Bug Investigator: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
