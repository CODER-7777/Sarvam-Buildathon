import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone

from . import ai, clients
from .config import ROOT
from .models import BugReport, Citation, Diagnosis, Evidence, abstain, validate_diagnosis, observed_excerpts
from .repository import search_repository
from .security import redact, secret_values

SAMPLES = {
    "checkout": "Checkout keeps spinning after clicking pay. The payment request times out and the spinner never stops.",
    "insufficient": "The dashboard sometimes shows incorrect totals, but I don't have logs or steps to reproduce it.",
    "no_session": "My checkout failed yesterday. Please find the session for demo-user-missing.",
    "non_bug": "Thanks team, have a great weekend!",
}


def is_non_bug(text):
    # Only reject obvious social messages; unfamiliar descriptions still get investigated.
    return bool(re.fullmatch(r"(?:hi|hello|hey|thanks(?: team)?(?:,? have a great weekend)?|thank you|good morning)[.!\s]*", text.strip(), re.I))


def new_run(report):
    return {"id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
            "duration": 0, "status": "running", "stage": "Queued", "report": report.model_dump(),
            "diagnosis": None, "observations": [], "evidence": [], "tools": [], "warnings": [], "linear": None}


def investigate(config, report, run=None, on_update=lambda run: None):
    secrets = secret_values(config)
    report = BugReport.model_validate({key: redact(value, secrets) if isinstance(value, str) else value
                                      for key, value in report.model_dump().items()})
    run = run or new_run(report)
    run["report"] = report.model_dump()
    started = time.monotonic()
    deadline = started + config.number("INVESTIGATION_TIMEOUT", 90, 5, 300)
    evidence: list[Evidence] = []
    validation_evidence = evidence

    def progress(stage):
        run["stage"] = stage
        run["duration"] = round(time.monotonic() - started, 2)
        run["evidence"] = [e.model_dump() for e in evidence]
        on_update(run)

    def add(source, item, origin="retrieved"):
        content = redact(item["content"], secrets)[:9000]
        location = str(item.get("location", ""))
        digest = hashlib.sha256(f"{source}|{origin}|{location}|{content}".encode()).hexdigest()[:12]
        item_id = f"E-{digest}"
        if not any(e.id == item_id for e in evidence):
            evidence.append(Evidence(id=item_id, source=source, origin=origin,
                                     title=redact(item["title"], secrets)[:300], content=content,
                                     location=location, timestamp=str(item.get("timestamp", ""))))

    def tool(name, operation, input_summary):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Investigation time budget exceeded.")
        before = time.monotonic()
        entry = {"tool": name, "input": input_summary, "started_at": datetime.now(timezone.utc).isoformat()}
        try:
            output = operation(min(config.timeout, max(.1, remaining)))
            serialized = redact(json.dumps(output, ensure_ascii=False), secrets)
            entry.update(status="success", output=serialized[:120000], output_truncated=len(serialized) > 120000)
            return output
        except Exception as exc:
            reason = str(exc) if isinstance(exc, (clients.ServiceError, FileNotFoundError, ValueError)) else "The tool failed or timed out."
            entry.update(status="error", output=redact(reason, secrets)[:500])
            run["warnings"].append(f"{name}: {entry['output']}")
            return None
        finally:
            entry["duration"] = round(time.monotonic() - before, 3)
            run["tools"].append(entry)

    try:
        progress("Parsing report")
        run["tools"].append({"tool": "report.parse", "status": "success", "duration": 0,
                             "input": "Bug description", "output": json.dumps({"classification": "social" if is_non_bug(report.text) else "bug_candidate", "user_id": report.user_id, "start": report.start, "end": report.end})})
        if is_non_bug(report.text):
            diagnosis = abstain("This is a social message rather than an actionable bug report.", "SKIPPED", [])
        elif report.mode == "demo":
            progress("Loading synthetic evidence")
            if report.sample == "checkout" and report.text == SAMPLES["checkout"] and report.user_id in ("", "demo-user-42"):
                events = tool("fixtures.events", lambda _: json.loads((ROOT / "fixtures/checkout/events.json").read_text(encoding="utf-8")), "Synthetic checkout scenario; no external service calls")
                for event in events or []:
                    add(event["source"], event, "synthetic")
                progress("Inspecting sample source")
                source = tool("fixtures.repository", lambda _: search_repository(str(ROOT / "fixtures/checkout"), ".", "submitPayment paymentLoading"), "Synthetic checkout.ts")
                for item in source or []:
                    add("repository", item, "synthetic")
                progress("Validating evidence")
                repo = next((e for e in evidence if e.source == "repository"), None)
                error = next((e for e in evidence if e.source == "sentry"), None)
                if repo and error:
                    diagnosis = Diagnosis(outcome="DIAGNOSED", root_cause="The payment failure path never clears the loading state.",
                        summary="In this synthetic scenario, payments.charge times out. submitPayment catches the exception, but resets loading only on success. The later UI snapshot still reports paymentLoading=true. This supports the stuck-spinner cause; it does not explain the upstream gateway timeout.",
                        confidence=.94, basis="deterministic_fixture",
                        citations=[Citation(evidence_id=repo.id, quote="await payments.charge(cartId);"),
                                   Citation(evidence_id=repo.id, quote="// BUG: loading is never reset on the failure path."),
                                   Citation(evidence_id=error.id, quote="PaymentTimeoutError: payments.charge exceeded 10000ms.")],
                        missing=["The upstream payment gateway timeout needs a separate investigation."],
                        next_steps=["Move setPaymentLoading(false) into a finally block so both outcomes clear the spinner.",
                                    "Show a recoverable payment error and avoid retrying charges without an idempotency key.",
                                    "Add a test that rejects payments.charge and verifies that loading resets."])
                else:
                    diagnosis = abstain("The demo fixture could not be loaded completely.")
            elif report.sample == "no_session" and report.text == SAMPLES["no_session"]:
                run["tools"].append({"tool": "fixtures.session_lookup", "input": "demo-user-missing", "status": "success", "output": "[] (synthetic fixture; no service queried)", "duration": 0})
                diagnosis = abstain("The synthetic session lookup returned no matching session.", "NO_SESSION")
            else:
                diagnosis = abstain("No matching sample evidence is available. Demo mode only diagnoses the exact checkout sample; use connected mode for your own report.")
        else:
            progress("Collecting service evidence")
            if report.diagnostics:
                add("diagnostics", {"title": "User-provided diagnostics (unverified)", "content": report.diagnostics}, "user_supplied")
            session_missing = False
            for name, fetch in (("posthog", clients.posthog_events), ("sentry", clients.sentry_events)):
                if config.configured(name):
                    rows = tool(f"{name}.events", lambda timeout, fetch=fetch: fetch(config, report, timeout),
                                {"user_id": report.user_id, "window": list(clients.time_window(report))})
                    if name == "posthog" and rows == [] and report.user_id:
                        session_missing = True
                    for item in rows or []:
                        add(name, item)
                else:
                    run["tools"].append({"tool": f"{name}.events", "status": "skipped", "input": "", "output": "Not configured; no request made.", "duration": 0})
            progress("Searching source code")
            if config.configured("repository"):
                leads = " ".join(e.content[:1200] for e in evidence if e.source in ("sentry", "diagnostics"))
                rows = tool("repository.search", lambda _: search_repository(config.get("REPO_ROOT"), report.repository, report.text + " " + leads),
                            {"subfolder": report.repository, "query": "Report terms and collected failure symbols"})
                for item in rows or []:
                    add("repository", item)
            else:
                run["warnings"].append("Repository not configured; no source files were inspected.")
            progress("Analyzing collected evidence")
            sources = {e.source for e in evidence}
            if session_missing and not sources.intersection({"sentry", "diagnostics"}):
                diagnosis = abstain("PostHog returned no events for the requested user and time window; no matching failure evidence was available.", "NO_SESSION")
            elif "repository" not in sources or not sources.intersection({"sentry", "diagnostics"}):
                diagnosis = abstain("A diagnosis requires relevant source code and matching error evidence. Available integrations did not supply both.")
            elif not config.configured("sarvam") or config.get("SARVAM_ENABLED", "false").lower() != "true":
                diagnosis = abstain("Evidence was collected, but Sarvam analysis is disabled. Configure SARVAM_API_KEY and explicitly enable SARVAM_ENABLED.")
            else:
                # Keep requests bounded even for the smaller conversations model.
                selected, size = [], 0
                for item in sorted(evidence, key=lambda e: {"sentry": 0, "diagnostics": 1, "repository": 2, "posthog": 3}[e.source]):
                    if size + len(item.content) <= 18000:
                        selected.append(item)
                        size += len(item.content)
                raw = tool("sarvam.analysis", lambda timeout: ai.analyze(config, report, selected, timeout).model_dump(),
                           {"model": config.get("SARVAM_MODEL", "sarvam-105b"), "evidence_ids": [e.id for e in selected]})
                diagnosis = Diagnosis.model_validate(raw) if raw else abstain("Sarvam analysis failed, timed out, or did not return a valid report. Collected evidence remains available.")
                validation_evidence = selected
        progress("Validating evidence")
        diagnosis = validate_diagnosis(diagnosis, validation_evidence, config.threshold)
        if time.monotonic() > deadline:
            diagnosis = abstain("The investigation exceeded its time budget. Collected evidence is retained.")
        run["diagnosis"] = diagnosis.model_dump()
        run["observations"] = observed_excerpts(diagnosis, validation_evidence)
        run["status"] = "completed"
    except Exception:
        run["diagnosis"] = abstain("The investigation could not finish. Check the tool history and configuration; collected evidence is retained.").model_dump()
        run["status"] = "failed"
    progress("Complete")
    return run
