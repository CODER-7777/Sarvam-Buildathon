# Sarvam-Buildathon

**Trace** is a local bug-investigation workspace powered by Sarvam. Submit a report, collect relevant source and observability evidence, inspect a grounded diagnosis or an explicit abstention, and optionally create a Linear issue after reviewing it.

This project retains the original Python SDK starter (`chat.py`). The application uses Python, the Sarvam SDK, Pydantic validation, HTTPX integration clients, and a buildless HTML/CSS/JavaScript frontend. No database, Node package installation, or frontend build is required.

## Run the local demo

From this directory on Windows:

```powershell
py -m pip install -r requirements.txt
py -m investigator serve
```

Open **http://127.0.0.1:8000**. On macOS/Linux, replace `py` with `python3`.

1. Keep **Sample investigation** selected.
2. Choose **Checkout spinner never stops**, then click **Start investigation**.
3. Inspect the diagnosis, source excerpt, synthetic event trail, citations, and tool history.
4. Try **Not enough evidence**, **Session not found**, and **A message that isn't a bug**.
5. Open **History** to revisit saved investigations.

The demo is deterministic and offline. It never calls Sarvam, PostHog, Sentry, or Linear automatically. Its source file and events are explicitly synthetic, and confidence is a fixed fixture value. Only the exact bundled checkout report receives the sample diagnosis; custom reports require connected mode.

CLI equivalents:

```powershell
py -m investigator investigate --demo
py -m investigator investigate --demo --sample insufficient
py -m investigator investigate --demo --sample no_session
py -m investigator investigate --demo --sample non_bug
```

## Configuration

Run the interactive wizard:

```powershell
py -m investigator setup
```

It preserves existing values on Enter, hides secret input, writes `.env`, and makes no API requests. Alternatively, use `.env.example` as a reference and edit `.env` directly. Do not overwrite an existing `.env` containing your keys. Environment variables take precedence over `.env`. Restart the server after changing configuration.

| Setting | Purpose |
| --- | --- |
| `SARVAM_API_KEY` | Server-side Sarvam credential; never exposed in the frontend |
| `SARVAM_ENABLED` | Defaults to `false`; set `true` when ready to authorize live analysis |
| `SARVAM_MODEL` | Defaults to `sarvam-105b` for coding and reasoning |
| `REPO_ROOT` | Absolute path to the repository the application is authorized to inspect |
| `POSTHOG_API_KEY` | Optional personal key with query read access |
| `POSTHOG_PROJECT_ID` | PostHog project ID |
| `POSTHOG_HOST` | Defaults to `https://us.posthog.com`; use your deployment's HTTPS host |
| `SENTRY_TOKEN` | Optional token with project read access |
| `SENTRY_ORG`, `SENTRY_PROJECT` | Organization and project slugs |
| `SENTRY_HOST` | Defaults to `https://sentry.io`; region-specific HTTPS hosts are supported |
| `LINEAR_API_KEY` | Optional Linear personal API key |
| `LINEAR_TEAM_ID` | Team ID; may be omitted only when the account has exactly one available team |
| `CONFIDENCE_THRESHOLD` | Minimum model confidence estimate, defaults to `0.75` |
| `REQUEST_TIMEOUT` | Upper bound on an HTTP phase timeout, defaults to 20 seconds |
| `INVESTIGATION_TIMEOUT` | Investigation time budget, defaults to 90 seconds |
| `PORT` | Local dashboard port, defaults to 8000 |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID` | Reserved; Slack is not implemented in this MVP |

There was no pre-existing Slack adapter to retain. The website and `process-once` command provide report ingestion. OpenRouter, Ollama, autonomous code changes, and Slack thread reporting are outside this MVP.

### Your first Sarvam call

`chat.py` is still the short starter using `sarvam-105b-conversations`. Put your key after `SARVAM_API_KEY=` in `.env` and run:

```powershell
py chat.py
```

That script makes a live request when a key is present. The investigator uses a separate `SARVAM_ENABLED` opt-in. During development, live Sarvam testing remains pending confirmation that the key is ready.

### Investigate your own application

1. Set `REPO_ROOT` to your application's repository.
2. Configure Sarvam, set `SARVAM_ENABLED=true`, and optionally configure Sentry/PostHog.
3. Restart the server and select **Your connected data**.
4. Describe the failure, optionally specify the exact user ID and a time window, and add relevant diagnostics if observability integrations are absent.
5. Start the investigation. Relevant redacted source excerpts and diagnostics are sent to Sarvam for analysis.

The repository field selects a relative subfolder of `REPO_ROOT`, such as `packages/checkout`. It cannot select an arbitrary filesystem location. The source browser is read-only. A missing time window defaults to the last 24 hours; the dashboard converts local date/time inputs to UTC.

```powershell
py -m investigator investigate --text "Checkout keeps spinning after clicking pay" --user-id customer-42 --repository packages/checkout --diagnostics error.log
py -m investigator process-once --text "Checkout fails with PaymentTimeoutError" --diagnostics error.log
py -m investigator --text "Checkout fails with PaymentTimeoutError" --diagnostics error.log
```

`process-once` consumes the supplied text once; it does not poll Slack. `--start` and `--end` accept timezone-qualified ISO timestamps and must be supplied together.

## Evidence and diagnosis rules

The pipeline parses the report, collects observability evidence, searches the repository using report terms and failure symbols, synthesizes a candidate report, and validates it before saving the result.

- Evidence IDs are hashes of the collected source, origin, location, and content.
- Every cited ID must exist in that investigation, and every quoted excerpt must be an exact substring of the corresponding evidence.
- The **Observed excerpts** section is built by the server from validated source text, not model-written factual claims. Citations from AI analysis must reference the evidence actually sent to that model call. Unknown fields such as model-written `facts` are rejected by the response schema.
- A diagnosis requires at least two distinct evidence records, including repository source and failure evidence from Sentry or user-supplied diagnostics, plus the configured confidence threshold.
- Invalid citations reject the entire candidate diagnosis. Source search alone cannot establish a cause.
- Model conclusions are displayed as **inferences**. Verifying a citation proves the excerpt exists; it does not mathematically prove the model's causal interpretation. Confidence is a model estimate, not a calibrated probability.
- User-provided logs are marked **unverified user input**. Demo data is marked **synthetic**. Integration results are marked **retrieved**.
- Missing configuration, service failures, malformed model output, or inadequate evidence produce an explicit abstention with available evidence retained.

| Outcome | Meaning |
| --- | --- |
| `DIAGNOSED` | A candidate cause passed schema, citation, evidence-source, and confidence checks |
| `NO_SESSION` | A successful targeted lookup returned no matching events/session and no other matching failure evidence was found |
| `NO_DIAGNOSIS` | Available evidence or analysis could not establish a reliable cause |
| `SKIPPED` | An obvious greeting or social message was not an actionable bug report |

Non-bug filtering deliberately recognizes only obvious social messages; an unfamiliar bug description is still investigated. Classification does not rely on an unrestricted model instruction.

## Integrations and limits

- **Sarvam:** official Python SDK, V1 chat completion, JSON response mode, Pydantic schema validation, and application-side citation validation. `sarvam-105b` is the default; `sarvam-105b-conversations` remains configurable. Evidence input is bounded and diagnostics are not duplicated in the model prompt.
- **PostHog:** a bounded, user/time-scoped HogQL query retrieves at most 20 events, including distinct and session IDs. This is an ad hoc investigation query, not an export or session-replay download.
- **Sentry:** retrieves at most 10 full error events for the time window, including available stack traces and breadcrumbs. The documented project-events endpoint does not expose a user filter; the adapter filters the returned page by exact `user.id` locally and independently checks each event timestamp against the requested window. Events with missing/malformed identity (when a user is requested) or unverifiable timestamps are excluded. It preserves the actual event identity rather than assigning the requested user to an event. A match outside that bounded page can be missed; narrow the time window or supply the relevant diagnostics.
- **Repository:** searches supported source extensions in up to 1,500 files, skips files over 200 KB, excludes hidden/dependency/credential directories and symlinks, and returns at most 10 matching excerpts. Search is lexical, not a full semantic index. No commands from source or model output are executed.
- **Linear:** the dashboard first shows the exact Markdown report, then requires **Create issue in Linear**. No investigation creates an issue automatically. Demo issues receive a `[DEMO]` prefix. Mutations are never retried automatically; an uncertain attempt is recorded to prevent duplicate issues.
- **History:** JSON records are saved atomically under ignored `runs/`; the dashboard lists the latest 100. Records include timestamps, duration, evidence, redacted bounded tool outputs, warnings, abstention reasons, and any Linear issue link. Files remain local and are not a separate database.

External reads retry once for selected transient failures. Per-call timeouts are reduced to fit the remaining investigation budget; no new tool starts after the budget expires. The budget is checked between steps and before accepting the final report; it is not a hard process-kill deadline. Services that continually stream bytes can outlast an HTTP phase timeout.

The server binds to `127.0.0.1`, checks Host and write-request origin/CSRF tokens, serves only the explicit frontend asset allowlist, and does not expose arbitrary files. Keep it local; it is not a multi-user hosted service. Known configured credentials and common credential patterns are redacted from saved/model-bound evidence, but redaction is best-effort and does not anonymize all personal data. Review the report before sending it to Linear.

## Development and verification

```powershell
py -m pip install -r requirements-dev.txt
py -m unittest discover -s tests -v
py -m mypy
py -m compileall -q investigator tests
node --check web/app.js
```

Node is needed only for the optional JavaScript syntax check. Runtime validation uses Pydantic. Python static checking uses mypy. The frontend serves directly without a compilation step.

The automated suite uses deterministic fixtures and mocked service clients, including an in-memory HTTP transport for the actual Sarvam SDK and a local HTTP server for end-to-end API checks. It covers diagnosis, abstention, reference/quote rejection, source boundaries, secret redaction, malformed input, timeout handling, retries, history, explicit Linear actions, duplicate prevention, and local web protections. The symlink test skips on Windows accounts without symlink privileges. No live service validation is claimed by these tests.

Verified locally on October 26, 2026: **44 tests passed, 1 symlink test skipped**; mypy checked all 12 application modules without errors; Python compilation and JavaScript syntax checks passed. Regression coverage includes invented factual fields using real IDs, fabricated quotations, model-visible evidence boundaries, Sentry identity/timestamp mismatches, and offline demos even with every credential configured. Earlier HTTP smoke checks returned 200 for the dashboard and both frontend assets, and the offline CLI saved the expected checkout diagnosis. Visual browser verification was unavailable because no browser was connected. Live Sarvam/PostHog/Sentry/Linear calls have not been tested.

## Structure

```text
chat.py                  Original one-call SDK starter
investigator/
  config.py              .env/environment configuration
  models.py              Request/report schemas and evidence validation
  repository.py          Bounded, read-only source search
  clients.py             PostHog, Sentry, and Linear clients
  ai.py                  Sarvam synthesis adapter
  engine.py              Investigation workflow and deterministic demo
  security.py            Credential redaction
  storage.py             Local records and Markdown reports
  server.py              Local HTTP API and job execution
  setup.py               Interactive configuration wizard
  __main__.py            CLI commands
web/                     Dashboard HTML, CSS, and JavaScript
fixtures/checkout/       Clearly labelled synthetic source and events
tests/                   Automated verification
runs/                    Ignored local investigation history
```

## Troubleshooting

- **Python isn't on PATH:** use `py` on Windows, or `python3` on other systems.
- **Port is busy:** run `py -m investigator serve --port 8001`.
- **Sarvam shows Disabled:** add a key, set `SARVAM_ENABLED=true`, and restart. Presence of credentials is not a verified connection.
- **Repository shows no results:** check `REPO_ROOT`, the relative subfolder, and relevant function/error terms. Secret, hidden, generated, dependency, and oversized files are intentionally excluded.
- **No diagnosis despite evidence:** inspect tool history and missing-information sections. Matching source plus an error, supported exact citations, and sufficient confidence are required.
- **Service returns 403/429/503:** check credential permissions, quota, service availability, and the configured host. The report remains available with a warning.
- **Linear team cannot be selected:** set `LINEAR_TEAM_ID`. After an uncertain creation attempt, check Linear before manually creating another issue.
- **Refresh token error:** reload the browser after restarting the server.

## Official API references consulted

- [Sarvam welcome](https://docs.sarvam.ai/api/getting-started/welcome)
- [Sarvam SDK configuration](https://docs.sarvam.ai/api/getting-started/sdks)
- [Sarvam chat completion V1](https://docs.sarvam.ai/api-reference/chat/chat-completions-v1)
- [Sarvam model choices and limits](https://docs.sarvam.ai/api/getting-started/models/sarvam-105b)
- [PostHog query API](https://posthog.com/docs/api/queries)
- [Sentry project error events](https://docs.sentry.io/api/events/list-a-projects-error-events/)
- [Linear GraphQL API](https://linear.app/developers/graphql)
