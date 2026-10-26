import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import ValidationError
from sarvamai import SarvamAI

from investigator import ai, clients
from investigator.config import Config, ROOT
from investigator.engine import SAMPLES, investigate
from investigator.models import BugReport, Citation, Diagnosis, Evidence, abstain, validate_diagnosis, observed_excerpts
from investigator.repository import resolve_repository, search_repository
from investigator.security import redact
from investigator.server import make_server
from investigator.storage import Store, markdown_report
from investigator import setup


def fixture_report(sample="checkout"):
    return BugReport(text=SAMPLES[sample], sample=sample)


class InvestigationTests(unittest.TestCase):
    def test_demo_complete_and_offline(self):
        with patch.object(clients, "request_json", side_effect=AssertionError("No network")), patch.object(ai, "analyze", side_effect=AssertionError("No AI")):
            run = investigate(Config({}), fixture_report())
        self.assertEqual(run["diagnosis"]["outcome"], "DIAGNOSED")
        self.assertEqual(len(run["evidence"]), 4)
        self.assertTrue(all(e["origin"] == "synthetic" for e in run["evidence"]))
        self.assertIn("finally", " ".join(run["diagnosis"]["next_steps"]))
        known = {e["id"]: e for e in run["evidence"]}
        for citation in run["diagnosis"]["citations"]:
            self.assertIn(citation["quote"], known[citation["evidence_id"]]["content"])

    def test_all_abstention_outcomes(self):
        for sample, outcome in [("insufficient", "NO_DIAGNOSIS"), ("no_session", "NO_SESSION"), ("non_bug", "SKIPPED")]:
            with self.subTest(sample=sample):
                run = investigate(Config({}), fixture_report(sample))
                self.assertEqual(run["diagnosis"]["outcome"], outcome)
                self.assertEqual(run["diagnosis"]["confidence"], 0)

    def test_demo_stays_offline_with_all_credentials_and_live_opt_in(self):
        config = Config({"SARVAM_API_KEY": "test-only-token", "SARVAM_ENABLED": "true",
                         "POSTHOG_API_KEY": "test-only-token", "POSTHOG_PROJECT_ID": "123",
                         "SENTRY_TOKEN": "test-only-token", "SENTRY_ORG": "demo", "SENTRY_PROJECT": "app",
                         "LINEAR_API_KEY": "test-only-token"})
        with patch.object(clients, "request_json") as network, patch.object(ai, "analyze") as model:
            for sample in SAMPLES:
                with self.subTest(sample=sample):
                    run = investigate(config, fixture_report(sample))
                    baseline = investigate(Config({}), fixture_report(sample))
                    self.assertEqual(run["diagnosis"], baseline["diagnosis"])
                    self.assertEqual(run["evidence"], baseline["evidence"])
        network.assert_not_called()
        model.assert_not_called()

    def test_observations_are_persisted_and_exported_from_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            run = investigate(Config({}), fixture_report(), on_update=store.save)
            saved = store.get(run["id"])
        known = {item["id"]: item for item in saved["evidence"]}
        self.assertTrue(saved["observations"])
        for observation in saved["observations"]:
            self.assertIn(observation["quote"], known[observation["evidence_id"]]["content"])
            self.assertEqual(observation["origin"], "synthetic")
        self.assertIn("Observed excerpts", markdown_report(saved))

    def test_custom_demo_report_cannot_inherit_fixture_cause(self):
        run = investigate(Config({}), BugReport(text="Login fails for everyone", sample="checkout"))
        self.assertEqual(run["diagnosis"]["outcome"], "NO_DIAGNOSIS")
        self.assertEqual(run["evidence"], [])

    def test_stable_evidence_ids(self):
        one = investigate(Config({}), fixture_report())
        two = investigate(Config({}), fixture_report())
        self.assertEqual([e["id"] for e in one["evidence"]], [e["id"] for e in two["evidence"]])

    def test_missing_config_makes_no_request(self):
        with patch.object(clients, "request_json", side_effect=AssertionError("No network")):
            run = investigate(Config({}), BugReport(text="Checkout failed", mode="connected"))
        self.assertEqual(run["diagnosis"]["outcome"], "NO_DIAGNOSIS")
        self.assertTrue(run["warnings"])

    def test_connected_error_retains_evidence_and_abstains(self):
        config = Config({"SENTRY_TOKEN": "test-only-token", "SENTRY_ORG": "demo", "SENTRY_PROJECT": "demo"})
        with patch.object(clients, "sentry_events", side_effect=clients.ServiceError("Service returned HTTP 503.")):
            run = investigate(config, BugReport(text="Checkout failed", mode="connected", diagnostics="PaymentTimeoutError at submitPayment"))
        self.assertEqual(run["diagnosis"]["outcome"], "NO_DIAGNOSIS")
        self.assertEqual(run["evidence"][0]["origin"], "user_supplied")
        self.assertIn("503", " ".join(run["warnings"]))

    def test_connected_success_uses_collected_references(self):
        config = Config({"REPO_ROOT": str(ROOT / "fixtures/checkout"), "SARVAM_API_KEY": "test-only-token", "SARVAM_ENABLED": "true"})
        def analysis(config, report, evidence, timeout):
            code = next(e for e in evidence if e.source == "repository")
            failure = next(e for e in evidence if e.source == "diagnostics")
            return Diagnosis(outcome="DIAGNOSED", root_cause="Missing loading reset in catch.", summary="The code matches the reported failure.", confidence=.9,
                             citations=[Citation(evidence_id=code.id, quote="await payments.charge(cartId);"), Citation(evidence_id=failure.id, quote="PaymentTimeoutError at submitPayment")], next_steps=["Reset loading in finally."])
        with patch.object(ai, "analyze", side_effect=analysis) as mocked:
            run = investigate(config, BugReport(text="Checkout payment keeps spinning", mode="connected", diagnostics="PaymentTimeoutError at submitPayment"))
        mocked.assert_called_once()
        self.assertEqual(run["diagnosis"]["outcome"], "DIAGNOSED")
        self.assertTrue(all(e["origin"] != "synthetic" for e in run["evidence"]))

    def test_disabled_ai_does_not_call_with_present_key(self):
        config = Config({"REPO_ROOT": str(ROOT / "fixtures/checkout"), "SARVAM_API_KEY": "test-only-token"})
        with patch.object(ai, "analyze") as mocked:
            run = investigate(config, BugReport(text="Checkout payment failed", mode="connected", diagnostics="PaymentTimeoutError at submitPayment"))
        mocked.assert_not_called()
        self.assertIn("disabled", run["diagnosis"]["summary"])

    def test_malformed_model_response_abstains_and_keeps_evidence(self):
        config = Config({"REPO_ROOT": str(ROOT / "fixtures/checkout"), "SARVAM_API_KEY": "test-only-token", "SARVAM_ENABLED": "true"})
        with patch.object(ai, "analyze", side_effect=ValueError("Invalid model JSON")):
            run = investigate(config, BugReport(text="Payment failed at submitPayment", mode="connected", diagnostics="PaymentTimeoutError at submitPayment"))
        self.assertEqual(run["diagnosis"]["outcome"], "NO_DIAGNOSIS")
        self.assertGreaterEqual(len(run["evidence"]), 2)
        self.assertTrue(any(t["tool"] == "sarvam.analysis" and t["status"] == "error" for t in run["tools"]))

    def test_no_session_requires_successful_lookup(self):
        config = Config({"POSTHOG_API_KEY": "test-only-token", "POSTHOG_PROJECT_ID": "123"})
        report = BugReport(text="Checkout failed", mode="connected", user_id="missing-user")
        with patch.object(clients, "posthog_events", return_value=[]):
            self.assertEqual(investigate(config, report)["diagnosis"]["outcome"], "NO_SESSION")
        with patch.object(clients, "posthog_events", side_effect=clients.ServiceError("Unavailable")):
            self.assertEqual(investigate(config, report)["diagnosis"]["outcome"], "NO_DIAGNOSIS")

    def test_secrets_redacted_before_persistence(self):
        secret = "never-save-this-sensitive-value"
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            run = investigate(Config({"SARVAM_API_KEY": secret}), BugReport(text=f"Checkout failed: {secret}", mode="connected", diagnostics=f"api_key={secret}"), on_update=store.save)
            self.assertNotIn(secret, store.path(run["id"]).read_text())
            self.assertIn("REDACTED", json.dumps(run))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.evidence = [Evidence(id="E-code", source="repository", origin="retrieved", title="code", content="await payments.charge(cartId);"),
                         Evidence(id="E-error", source="sentry", origin="retrieved", title="error", content="PaymentTimeoutError at submitPayment")]
        self.valid = Diagnosis(outcome="DIAGNOSED", root_cause="Possible cause", summary="Inference", confidence=.9,
                               citations=[Citation(evidence_id="E-code", quote=self.evidence[0].content), Citation(evidence_id="E-error", quote=self.evidence[1].content)])

    def test_valid_grounding(self):
        self.assertEqual(validate_diagnosis(self.valid, self.evidence, .75).outcome, "DIAGNOSED")

    def test_unknown_id_rejected(self):
        self.valid.citations[0].evidence_id = "E-invented"
        self.assertEqual(validate_diagnosis(self.valid, self.evidence, .75).outcome, "NO_DIAGNOSIS")

    def test_fabricated_quote_rejected(self):
        self.valid.citations[0].quote = "An invented source statement"
        self.assertEqual(validate_diagnosis(self.valid, self.evidence, .75).outcome, "NO_DIAGNOSIS")

    def test_model_written_facts_cannot_bypass_schema_with_real_id(self):
        payload = self.valid.model_dump()
        payload["facts"] = [{"text": "All customer payments were stolen.", "evidence_id": "E-code"}]
        with self.assertRaises(ValidationError):
            Diagnosis.model_validate(payload)

    def test_invented_inference_never_becomes_observed_source_text(self):
        self.valid.summary = "An unsupported theory about the payment gateway."
        observations = observed_excerpts(self.valid, self.evidence)
        self.assertNotIn(self.valid.summary, json.dumps(observations))
        self.assertEqual(observations[0]["quote"], self.evidence[0].content)
        self.assertEqual(observations[0]["origin"], "retrieved")

    def test_observation_builder_rejects_fabricated_quote_on_real_id(self):
        self.valid.citations[0].quote = "All payments were stolen."
        with self.assertRaises(ValueError):
            observed_excerpts(self.valid, self.evidence)

    def test_duplicate_ids_and_blank_cause_abstain(self):
        self.assertEqual(validate_diagnosis(self.valid, self.evidence + self.evidence[:1], .75).outcome, "NO_DIAGNOSIS")
        self.valid.root_cause = " "
        self.assertEqual(validate_diagnosis(self.valid, self.evidence, .75).outcome, "NO_DIAGNOSIS")

    def test_citation_must_belong_to_model_visible_evidence(self):
        visible_to_model = self.evidence[:1]
        self.assertEqual(validate_diagnosis(self.valid, visible_to_model, .75).outcome, "NO_DIAGNOSIS")

    def test_low_confidence_rejected(self):
        self.valid.confidence = .5
        self.assertEqual(validate_diagnosis(self.valid, self.evidence, .75).outcome, "NO_DIAGNOSIS")

    def test_only_code_cannot_support_diagnosis(self):
        self.valid.citations = self.valid.citations[:1]
        self.assertEqual(validate_diagnosis(self.valid, self.evidence, .75).outcome, "NO_DIAGNOSIS")

    def test_schema_rejects_invalid_response(self):
        with self.assertRaises(ValidationError):
            Diagnosis.model_validate({"outcome": "DIAGNOSED", "confidence": 4, "fabricated": True})

    def test_time_window_requires_pair_order_and_zone(self):
        for values in [{"start": "2026-10-20"}, {"start": "2026-10-20T10:00:00", "end": "2026-10-20T11:00:00"},
                       {"start": "2026-10-20T12:00:00Z", "end": "2026-10-20T11:00:00Z"}]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                BugReport(text="An actual bug", **values)


class RepositoryTests(unittest.TestCase):
    def test_boundaries_and_secret_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "checkout.py").write_text("def checkout():\n    raise RuntimeError('payment failed')")
            (root / ".env").write_text("checkout=secret")
            (root / "credentials.py").write_text("checkout_secret = 'sensitive'")
            (root / ".aws").mkdir()
            (root / ".aws/config.py").write_text("checkout_secret = 'sensitive'")
            results = search_repository(tmp, ".", "checkout payment")
            self.assertEqual(len(results), 1)
            self.assertIn("checkout.py:", results[0]["location"])
            for invalid in ["../", "../../Windows", "C:/Windows", "folder\\..\\outside"]:
                with self.subTest(invalid=invalid), self.assertRaises((ValueError, OSError)):
                    resolve_repository(tmp, invalid)

    def test_symlink_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "private.py"
            target.write_text("checkout = 'private'")
            link = Path(tmp) / "checkout.py"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("This Windows account cannot create symlinks.")
            self.assertEqual(search_repository(tmp, ".", "checkout"), [])

    def test_redaction(self):
        output = redact('Authorization: Bearer sensitive_value token="abc123" sk_example-private-token')
        self.assertNotIn("sensitive_value", output)
        self.assertNotIn("abc123", output)
        self.assertNotIn("sk_example", output)

    def test_store_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                Store(tmp).get("../../.env")


class ClientTests(unittest.TestCase):
    def test_sdk_serializes_documented_chat_request(self):
        captured = []
        def handler(request):
            captured.append(json.loads(request.content))
            self.assertEqual(request.url.path, "/v1/chat/completions")
            return httpx.Response(200, json={"id": "test", "created": 0, "model": "sarvam-105b", "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": abstain("Not enough evidence.").model_dump_json()}}]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            client = SarvamAI(api_subscription_key="test-only-token", httpx_client=http)
            with patch.object(ai, "SarvamAI", return_value=client):
                result = ai.analyze(Config({"SARVAM_API_KEY": "test-only-token", "SARVAM_ENABLED": "true"}), BugReport(text="Checkout failed", mode="connected"), [], 3)
        self.assertEqual(result.outcome, "NO_DIAGNOSIS")
        self.assertEqual(captured[0]["response_format"], {"type": "json_object"})
        self.assertEqual(captured[0]["model"], "sarvam-105b")

    def test_provider_itself_blocks_demo_even_with_credentials(self):
        config = Config({"SARVAM_API_KEY": "test-only-token", "SARVAM_ENABLED": "true"})
        with patch.object(ai, "SarvamAI") as sdk:
            with self.assertRaisesRegex(ValueError, "Demo mode"):
                ai.analyze(config, fixture_report(), [], 3)
        sdk.assert_not_called()

    def test_posthog_query_scopes_user_and_time(self):
        config = Config({"POSTHOG_API_KEY": "test-only-token", "POSTHOG_PROJECT_ID": "123"})
        report = BugReport(text="Checkout failed", user_id="user' OR 1=1 --", start="2026-10-20T10:00:00Z", end="2026-10-20T11:00:00Z")
        with patch.object(clients, "request_json", return_value={"results": [["uuid", "payment_failed", "2026-10-20T10:30:00Z", "user", "session", {}]]}) as request:
            rows = clients.posthog_events(config, report, 3)
        query = request.call_args.kwargs["json"]["query"]["query"]
        self.assertIn("LIMIT 20", query)
        self.assertIn("user\\' OR 1=1 --", query)
        self.assertIn("2026-10-20 10:00:00", query)
        self.assertIn("session_id", rows[0]["content"])

    def test_sentry_full_events_and_user_filter(self):
        config = Config({"SENTRY_ORG": "demo", "SENTRY_PROJECT": "app", "SENTRY_TOKEN": "test-only-token"})
        with patch.object(clients, "request_json", return_value=[{"title": "PaymentTimeoutError", "dateCreated": "2026-10-20T10:30:00Z", "user": {"id": "u1"}, "entries": [{"type": "exception"}]}, {"title": "Unrelated error", "dateCreated": "2026-10-20T10:30:00Z", "user": {"id": "u2"}}]) as request:
            rows = clients.sentry_events(config, BugReport(text="Checkout failed", user_id="u1", start="2026-10-20T10:00:00Z", end="2026-10-20T11:00:00Z"), 3)
        self.assertEqual(request.call_args.kwargs["params"]["full"], "true")
        self.assertIn("exception", rows[0]["content"])
        self.assertEqual(len(rows), 1)
        self.assertNotIn("query", request.call_args.kwargs["params"])

    def test_sentry_rejects_wrong_missing_or_malformed_user_identity(self):
        report = BugReport(text="Checkout failed", user_id="u1", start="2026-10-20T10:00:00Z", end="2026-10-20T11:00:00Z")
        config = Config({"SENTRY_ORG": "demo", "SENTRY_PROJECT": "app"})
        for user in ({"id": "u2"}, None, {}, [], "u1", {"id": None}, {"id": True}):
            with self.subTest(user=user), patch.object(clients, "request_json", return_value=[{"user": user, "dateCreated": "2026-10-20T10:30:00Z"}]):
                self.assertEqual(clients.sentry_events(config, report, 3), [])

    def test_sentry_rejects_outside_window_or_unverifiable_timestamps(self):
        report = BugReport(text="Checkout failed", user_id="u1", start="2026-10-20T10:00:00Z", end="2026-10-20T11:00:00Z")
        config = Config({"SENTRY_ORG": "demo", "SENTRY_PROJECT": "app"})
        for timestamp in ("2026-10-19T10:30:00Z", "2026-10-20T11:01:00Z", None, "bad", "2026-10-20T10:30:00"):
            with self.subTest(timestamp=timestamp), patch.object(clients, "request_json", return_value=[{"user": {"id": "u1"}, "dateCreated": timestamp}]):
                self.assertEqual(clients.sentry_events(config, report, 3), [])

    def test_sentry_preserves_actual_event_and_handles_timezone_offsets(self):
        report = BugReport(text="Checkout failed", user_id="42", start="2026-10-20T10:00:00Z", end="2026-10-20T11:00:00Z")
        original = {"eventID": "actual-event", "user": {"id": 42}, "dateCreated": "2026-10-20T16:00:00+05:30"}
        with patch.object(clients, "request_json", return_value=[original]):
            rows = clients.sentry_events(Config({"SENTRY_ORG": "demo", "SENTRY_PROJECT": "app"}), report, 3)
        self.assertEqual(json.loads(rows[0]["content"]), original)

    def test_linear_mutation_is_not_retried(self):
        config = Config({"LINEAR_API_KEY": "test-only-token", "LINEAR_TEAM_ID": "team"})
        run = investigate(Config({}), fixture_report())
        with patch.object(clients, "request_json", return_value={"data": {"issueCreate": {"success": True, "issue": {"identifier": "TEST-1", "url": "https://linear.app/test/issue/TEST-1"}}}}) as request:
            result = clients.create_linear_issue(config, run, markdown_report(run))
        self.assertFalse(request.call_args.kwargs["retry"])
        self.assertTrue(request.call_args.kwargs["json"]["variables"]["input"]["title"].startswith("[DEMO]"))
        self.assertEqual(result["identifier"], "TEST-1")

    def test_linear_graphql_errors_fail(self):
        with patch.object(clients, "request_json", return_value={"errors": [{"message": "secret details"}]}):
            with self.assertRaisesRegex(clients.ServiceError, "Linear rejected"):
                clients.linear_query(Config({}), "query {}")

    def test_linear_requires_explicit_team_when_multiple_exist(self):
        with patch.object(clients, "linear_query", return_value={"teams": {"nodes": [{"id": "a"}, {"id": "b"}]}}) as query:
            with self.assertRaisesRegex(clients.ServiceError, "LINEAR_TEAM_ID"):
                clients.create_linear_issue(Config({"LINEAR_API_KEY": "test-only-token"}), {}, "report")
        query.assert_called_once()

    def test_http_timeout_is_sanitized(self):
        with patch.object(clients.httpx.Client, "request", side_effect=httpx.ReadTimeout("private token text")):
            with self.assertRaisesRegex(clients.ServiceError, "Service unavailable") as caught:
                clients.request_json("GET", "https://example.invalid", timeout=.1, retry=False)
        self.assertNotIn("private", str(caught.exception))

    def test_transient_read_retries(self):
        calls = [httpx.Response(503), httpx.Response(200, json={"ok": True})]
        with patch.object(clients.httpx.Client, "request", side_effect=calls) as request:
            self.assertEqual(clients.request_json("GET", "https://example.invalid", timeout=.1), {"ok": True})
        self.assertEqual(request.call_count, 2)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = make_server(Config({"LINEAR_API_KEY": "test-only-token"}, runs=Path(cls.tmp.name)), 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.http = httpx.Client(base_url=f"http://127.0.0.1:{cls.server.server_port}", timeout=5)
        cls.csrf = cls.http.get("/api/config").json()["csrf"]

    @classmethod
    def tearDownClass(cls):
        cls.http.close()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def test_static_secrets_and_cross_origin(self):
        self.assertEqual(self.http.get("/").status_code, 200)
        self.assertEqual(self.http.get("/.env").status_code, 404)
        self.assertEqual(self.http.get("/api/config", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.http.post("/api/runs", json=fixture_report().model_dump()).status_code, 403)
        self.assertEqual(self.http.post("/api/runs", headers={"X-CSRF-Token": self.csrf, "Origin": "https://evil.example"}, json=fixture_report().model_dump()).status_code, 403)

    def test_invalid_input(self):
        response = self.http.post("/api/runs", headers={"X-CSRF-Token": self.csrf}, json={"text": ""})
        self.assertEqual(response.status_code, 400)

    def test_end_to_end_and_explicit_linear_action(self):
        headers = {"X-CSRF-Token": self.csrf}
        response = self.http.post("/api/runs", headers=headers, json=fixture_report().model_dump())
        self.assertEqual(response.status_code, 202)
        run_id = response.json()["id"]
        for _ in range(100):
            run = self.http.get(f"/api/runs/{run_id}").json()
            if run["status"] != "running":
                break
            time.sleep(.02)
        self.assertEqual(run["diagnosis"]["outcome"], "DIAGNOSED")
        self.assertIn(run_id, [r["id"] for r in self.http.get("/api/runs").json()])
        self.assertIn("SYNTHETIC", self.http.get(f"/api/runs/{run_id}/markdown").text)
        with patch("investigator.server.create_linear_issue", return_value={"id": "test-issue", "identifier": "TEST-1", "url": "https://linear.app/test/issue/TEST-1"}) as create:
            self.assertEqual(self.http.post(f"/api/runs/{run_id}/linear", headers=headers, json={}).status_code, 400)
            create.assert_not_called()
            self.assertEqual(self.http.post(f"/api/runs/{run_id}/linear", headers=headers, json={"confirm": True}).status_code, 201)
            self.assertEqual(self.http.post(f"/api/runs/{run_id}/linear", headers=headers, json={"confirm": True}).status_code, 200)
            create.assert_called_once()


class SetupTests(unittest.TestCase):
    def test_wizard_preserves_values_and_does_not_display_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("SARVAM_API_KEY=hidden-existing-value\nCUSTOM_SETTING=keep-me\n")
            (root / ".env.example").write_text("SARVAM_ENABLED=false\nSARVAM_MODEL=sarvam-105b\n")
            with patch.object(setup, "ROOT", root), patch.object(setup, "getpass", return_value="") as secret_input, patch("builtins.input", return_value="") as regular_input, patch("builtins.print") as output:
                setup.wizard()
            saved = (root / ".env").read_text()
            self.assertIn("SARVAM_API_KEY=hidden-existing-value", saved)
            self.assertIn("CUSTOM_SETTING=keep-me", saved)
            for mocked in (secret_input, regular_input, output):
                self.assertNotIn("hidden-existing-value", str(mocked.call_args_list))


if __name__ == "__main__":
    unittest.main()
