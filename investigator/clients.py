import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx


class ServiceError(Exception):
    pass


def request_json(method, url, *, timeout, retry=True, **kwargs):
    """Only retry reads/queries, never issue-creation mutations. No credentials in errors."""
    attempts = 2 if retry else 1
    for attempt in range(attempts):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                response = client.request(method, url, **kwargs)
            if response.status_code in (429, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(.3)
                continue
            if not 200 <= response.status_code < 300:
                raise ServiceError(f"Service returned HTTP {response.status_code}.")
            if len(response.content) > 4_000_000:
                raise ServiceError("Service response exceeded the size limit.")
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if attempt + 1 == attempts:
                raise ServiceError("Service unavailable or returned invalid JSON.") from None
    raise ServiceError("Service unavailable.")


def time_window(report):
    now = datetime.now(timezone.utc)
    return (report.start or (now - timedelta(hours=24)).isoformat(), report.end or now.isoformat())


def sql_string(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def posthog_events(config, report, timeout):
    host = config.host("POSTHOG_HOST", "https://us.posthog.com")
    project = quote(config.get("POSTHOG_PROJECT_ID"), safe="")
    start, end = time_window(report)
    start_sql, end_sql = (datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") for value in (start, end))
    where = f"timestamp >= toDateTime({sql_string(start_sql)}, 'UTC') AND timestamp <= toDateTime({sql_string(end_sql)}, 'UTC')"
    if report.user_id:
        where += f" AND distinct_id = {sql_string(report.user_id)}"
    query = "SELECT uuid, event, timestamp, distinct_id, properties.$session_id, properties FROM events WHERE " + where + " ORDER BY timestamp DESC LIMIT 20"
    data = request_json("POST", f"{host}/api/projects/{project}/query/", timeout=timeout,
                        headers={"Authorization": f"Bearer {config.get('POSTHOG_API_KEY')}"},
                        json={"query": {"kind": "HogQLQuery", "query": query}})
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ServiceError("PostHog returned an unexpected response.")
    return [{"title": str(row[1]), "timestamp": str(row[2]),
             "content": json.dumps(dict(zip(["uuid", "event", "timestamp", "distinct_id", "session_id", "properties"], row)), ensure_ascii=False)}
            for row in data["results"][:20] if isinstance(row, list) and len(row) >= 6]


def sentry_events(config, report, timeout):
    host = config.host("SENTRY_HOST", "https://sentry.io")
    org, project = (quote(config.get(k), safe="") for k in ("SENTRY_ORG", "SENTRY_PROJECT"))
    start, end = time_window(report)
    params = {"full": "true", "start": start, "end": end, "per_page": 10}
    data = request_json("GET", f"{host}/api/0/projects/{org}/{project}/events/", timeout=timeout,
                        headers={"Authorization": f"Bearer {config.get('SENTRY_TOKEN')}"}, params=params)
    if not isinstance(data, list):
        raise ServiceError("Sentry returned an unexpected response.")
    # This documented endpoint has no user query parameter. Filter the bounded
    # result locally and never represent an unrelated user's exception as a match.
    start_at, end_at = (datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (start, end))
    matched = []
    for item in data[:10]:
        if not isinstance(item, dict):
            continue
        if report.user_id:
            user = item.get("user")
            actual_id = user.get("id") if isinstance(user, dict) else None
            if type(actual_id) not in (str, int) or str(actual_id) != report.user_id:
                continue
        timestamp = item.get("dateCreated") or item.get("datetime")
        if not isinstance(timestamp, str):
            continue
        try:
            occurred_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if occurred_at.tzinfo is None or not start_at <= occurred_at <= end_at:
            continue
        matched.append(item)
    return [{"title": str(item.get("title") or item.get("message") or "Exception"),
             "timestamp": str(item.get("dateCreated") or item.get("datetime") or ""),
             "content": json.dumps(item, ensure_ascii=False)} for item in matched]


def linear_query(config, query, variables=None, retry=True):
    data = request_json("POST", "https://api.linear.app/graphql", timeout=config.timeout, retry=retry,
                        headers={"Authorization": config.get("LINEAR_API_KEY")},
                        json={"query": query, "variables": variables or {}})
    if not isinstance(data, dict) or data.get("errors") or not data.get("data"):
        raise ServiceError("Linear rejected the request. Check the token and team permissions.")
    return data["data"]


def create_linear_issue(config, run, markdown):
    if not config.configured("linear"):
        raise ServiceError("Linear is not configured. Add LINEAR_API_KEY in .env.")
    team = config.get("LINEAR_TEAM_ID")
    if not team:
        teams = linear_query(config, "query { teams { nodes { id name } } }")["teams"]["nodes"]
        if len(teams) != 1:
            raise ServiceError("Set LINEAR_TEAM_ID to choose the destination team.")
        team = teams[0]["id"]
    data = linear_query(config,
                        "mutation CreateIssue($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier url } } }",
                        {"input": {"teamId": team, "title": ("[DEMO] " if run["report"]["mode"] == "demo" else "") + run["report"]["text"][:180], "description": markdown}}, retry=False)
    created = data.get("issueCreate", {})
    if not created.get("success") or not created.get("issue"):
        raise ServiceError("Linear did not confirm issue creation.")
    return created["issue"]
