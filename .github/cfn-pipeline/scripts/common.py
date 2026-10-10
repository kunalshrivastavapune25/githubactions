"""Shared helpers for the CloudFormation deployment pipeline.

Everything that talks to GitHub Actions (outputs, summaries, annotations) or creates
AWS clients lives here, so the stage scripts stay focused on CloudFormation logic.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

# --------------------------------------------------------------------------------------
# GitHub Actions plumbing
# --------------------------------------------------------------------------------------

_SUMMARY_LIMIT = 900_000  # GitHub rejects step summaries over 1 MiB


def _escape_data(value: str) -> str:
    return str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_prop(value: str) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def annotate(level: str, message: str, title: str | None = None,
             file: str | None = None, line: int | None = None) -> None:
    """Emit a ::notice/::warning/::error annotation (shows on the run summary page)."""
    props = []
    if title:
        props.append(f"title={_escape_prop(title)}")
    if file:
        props.append(f"file={_escape_prop(file)}")
    if line:
        props.append(f"line={int(line)}")
    prop_str = (" " + ",".join(props)) if props else ""
    print(f"::{level}{prop_str}::{_escape_data(message)}", flush=True)


def notice(message: str, title: str | None = None, **kw: Any) -> None:
    annotate("notice", message, title, **kw)


def warning(message: str, title: str | None = None, **kw: Any) -> None:
    annotate("warning", message, title, **kw)


def error(message: str, title: str | None = None, **kw: Any) -> None:
    annotate("error", message, title, **kw)


def mask(value: Any) -> None:
    """Tell the runner to redact a value from all later log output."""
    if value is None:
        return
    for part in str(value).splitlines():
        if part.strip():
            print(f"::add-mask::{part}", flush=True)


def set_output(name: str, value: Any) -> None:
    """Write a step output (multi-line safe)."""
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, default=json_default)
    else:
        text = str(value)
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"[output] {name}={text}")
        return
    with open(path, "a", encoding="utf-8") as fh:
        if "\n" in text:
            delim = f"EOF_{uuid.uuid4().hex}"
            fh.write(f"{name}<<{delim}\n{text}\n{delim}\n")
        else:
            fh.write(f"{name}={text}\n")


def add_summary(markdown: str) -> None:
    """Append markdown to the job summary (the page you see when you click a job box)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        print(markdown)
        return
    current = os.path.getsize(path) if os.path.exists(path) else 0
    if current + len(markdown.encode("utf-8")) > _SUMMARY_LIMIT:
        markdown = "> Summary truncated — see the uploaded report artifact for full details.\n"
        if current + len(markdown) > _SUMMARY_LIMIT:
            return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(markdown.rstrip() + "\n\n")


class group:
    """Collapsible log group: `with group("Title"): ...`"""

    def __init__(self, title: str):
        self.title = title

    def __enter__(self) -> "group":
        print(f"::group::{self.title}", flush=True)
        return self

    def __exit__(self, *_exc: Any) -> bool:
        print("::endgroup::", flush=True)
        return False


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def run_context() -> dict[str, str]:
    server = env("GITHUB_SERVER_URL", "https://github.com")
    repo = env("GITHUB_REPOSITORY", "local/repo")
    run_id = env("GITHUB_RUN_ID", "0")
    sha = env("GITHUB_SHA")
    return {
        "server": server,
        "repository": repo,
        "run_id": run_id,
        "run_attempt": env("GITHUB_RUN_ATTEMPT", "1"),
        "run_url": f"{server}/{repo}/actions/runs/{run_id}",
        "sha": sha,
        "short_sha": sha[:7],
        "commit_url": f"{server}/{repo}/commit/{sha}" if sha else "",
        "actor": env("GITHUB_ACTOR", "local-user"),
        "triggering_actor": env("GITHUB_TRIGGERING_ACTOR", env("GITHUB_ACTOR", "local-user")),
        "ref": env("GITHUB_REF_NAME"),
        "workflow": env("GITHUB_WORKFLOW", "CloudFormation deploy"),
    }


# --------------------------------------------------------------------------------------
# AWS helpers
# --------------------------------------------------------------------------------------

# Tests replace this with a function returning fake clients.
CLIENT_FACTORY: Callable[[str, str | None], Any] | None = None
_SESSIONS: dict[str, Any] = {}


def _github_oidc_token(audience: str = "sts.amazonaws.com") -> str:
    """Fresh GitHub OIDC token (needs `permissions: id-token: write`)."""
    import urllib.request

    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    joiner = "&" if "?" in url else "?"
    request = urllib.request.Request(f"{url}{joiner}audience={quote(audience, safe='')}",
                                     headers={"Authorization": f"bearer {os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - GitHub-provided URL
        return json.load(response)["value"]


def _refreshing_session(role_arn: str, region: str | None) -> Any:
    """boto3 session whose credentials re-assume the role via OIDC before they expire.

    Used by the execute stage: CloudFormation deployments (Directory Service, RDS, ...) can
    outlive the 1-hour session that configure-aws-credentials hands out.
    """
    import boto3
    import botocore.session
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.credentials import RefreshableCredentials

    session_name = f"cfn-execute-{os.environ.get('GITHUB_RUN_ID', 'local')}"[:64]

    def refresh() -> dict:
        sts = boto3.client("sts", region_name=region, config=Config(signature_version=UNSIGNED))
        creds = sts.assume_role_with_web_identity(RoleArn=role_arn, RoleSessionName=session_name,
                                                  WebIdentityToken=_github_oidc_token(),
                                                  DurationSeconds=3600)["Credentials"]
        return {"access_key": creds["AccessKeyId"], "secret_key": creds["SecretAccessKey"],
                "token": creds["SessionToken"], "expiry_time": creds["Expiration"].isoformat()}

    credentials = RefreshableCredentials.create_from_metadata(
        metadata=refresh(), refresh_using=refresh, method="sts-assume-role-with-web-identity")
    core = botocore.session.get_session()
    core._credentials = credentials  # noqa: SLF001 - documented botocore pattern for custom refreshers
    if region:
        core.set_config_variable("region", region)
    return boto3.Session(botocore_session=core)


def aws_client(service: str, region: str | None = None) -> Any:
    if CLIENT_FACTORY is not None:
        return CLIENT_FACTORY(service, region)
    import boto3  # imported lazily so unit tests run without boto3
    from botocore.config import Config

    config = Config(
        retries={"max_attempts": 10, "mode": "adaptive"},
        user_agent_extra="cfn-gha-pipeline/1.0",
        read_timeout=600 if service == "bedrock-runtime" else 60,
        connect_timeout=10,
    )
    role_arn = os.environ.get("CFN_OIDC_ROLE_ARN")
    if role_arn and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL"):
        key = f"{role_arn}|{region}"
        if key not in _SESSIONS:
            _SESSIONS[key] = _refreshing_session(role_arn, region)
        return _SESSIONS[key].client(service, region_name=region, config=config)
    return boto3.client(service, region_name=region, config=config)


def error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None) or {}
    return str((response.get("Error") or {}).get("Code", ""))


def error_message(exc: BaseException) -> str:
    response = getattr(exc, "response", None) or {}
    return str((response.get("Error") or {}).get("Message", "")) or str(exc)


def paginate(call: Callable[..., dict], result_key: str, **kwargs: Any) -> Iterator[Any]:
    """Generic NextToken pagination (works for every CloudFormation list/describe call)."""
    token = None
    while True:
        args = dict(kwargs)
        if token:
            args["NextToken"] = token
        response = call(**args)
        yield from (response.get(result_key) or [])
        token = response.get("NextToken")
        if not token:
            return


def account_from_arn(arn: str | None) -> str:
    if not arn or not arn.startswith("arn:"):
        return ""
    parts = arn.split(":")
    return parts[4] if len(parts) > 5 else ""


def _console_base(region: str) -> str:
    return f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}"


def stack_console_url(region: str, stack_id: str, tab: str = "stackinfo") -> str:
    return f"{_console_base(region)}#/stacks/{tab}?stackId={quote(stack_id or '', safe='')}"


def drift_console_url(region: str, stack_id: str) -> str:
    return stack_console_url(region, stack_id, "drifts")


def events_console_url(region: str, stack_id: str) -> str:
    return stack_console_url(region, stack_id, "events")


def change_set_console_url(region: str, stack_id: str, change_set_id: str) -> str:
    return (f"{_console_base(region)}#/stacks/changesets/changes?stackId={quote(stack_id or '', safe='')}"
            f"&changeSetId={quote(change_set_id or '', safe='')}")


# --------------------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------------------

def json_default(obj: Any) -> Any:
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)


def write_json(path: str | Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, default=json_default), encoding="utf-8")


def read_json(path: str | Path, default: Any = None) -> Any:
    source = Path(path)
    if not source.is_file():
        return default
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def maybe_json(value: Any) -> Any:
    """CloudFormation returns many values as JSON-encoded strings; decode when possible."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{\"" and not re.fullmatch(r"-?\d+(\.\d+)?|true|false|null", stripped):
        return value
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        return value


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso(ts: Any) -> str:
    """Human-readable UTC timestamp from a datetime or an ISO-8601 string (as stored in reports)."""
    if isinstance(ts, str) and ts:
        try:
            ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return ts
    if isinstance(ts, _dt.datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return ts.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(ts or "")


def duration(seconds: Any) -> str:
    secs = int(seconds or 0)
    if secs < 60:
        return f"{secs} s"
    hours, rest = divmod(secs, 3600)
    minutes, secs = divmod(rest, 60)
    return (f"{hours} h " if hours else "") + f"{minutes} min {secs} s"


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def property_label(detail: dict) -> str:
    """'/Properties/VisibilityTimeout' -> 'VisibilityTimeout' for change set details."""
    label = str(detail.get("path") or detail.get("name") or detail.get("attribute") or "")
    return label[len("/Properties/"):] if label.startswith("/Properties/") else label


# Keys whose *values* must never be written to reports, emails or AI prompts.
_SENSITIVE_KEY = re.compile(
    r"(?i)(passw(or)?d|secret|token|api[-_]?key|private[-_]?key|credential|connection[-_]?string|"
    r"access[-_]?key|auth[-_]?key)"
)
_SAFE_SUFFIX = re.compile(r"(?i)(arn|id|ids|name|names|ref|type|policy|rotation\w*|length|enabled|kmskeyid|template)$")
REDACTED = "***REDACTED***"


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key)) and not _SAFE_SUFFIX.search(key)


def redact(obj: Any) -> Any:
    """Return a copy with values of secret-looking keys replaced.

    Also covers Name/Value and Key/Value pairs (ECS environment variables, tags, SSM-style lists),
    e.g. {"Name": "DB_PASSWORD", "Value": "..."}.
    """
    if isinstance(obj, dict):
        label = obj.get("Name", obj.get("Key", obj.get("name", obj.get("key"))))
        pair_secret = isinstance(label, str) and is_sensitive_key(label)
        clean = {}
        for key, value in obj.items():
            scalar = not isinstance(value, (dict, list))
            if scalar and (is_sensitive_key(str(key)) or (pair_secret and str(key).lower() == "value")):
                clean[key] = REDACTED
            else:
                clean[key] = redact(value)
        return clean
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    return obj


def to_yaml(data: Any) -> str:
    """Readable block-style YAML (multi-line strings as | blocks, no anchors)."""
    import yaml

    class _Dumper(yaml.SafeDumper):
        def ignore_aliases(self, data: Any) -> bool:  # noqa: D401 - PyYAML hook
            return True

    def _str(dumper: yaml.SafeDumper, value: str) -> Any:
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    _Dumper.add_representer(str, _str)
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, default_flow_style=False,
                     allow_unicode=True, width=120)


def short(value: Any, limit: int = 160) -> str:
    """Compact single-line rendering for tables."""
    if value is None:
        return "—"
    text = value if isinstance(value, str) else json.dumps(value, default=json_default, separators=(",", ":"))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def md_escape(value: Any) -> str:
    """Escape a value for a markdown table cell."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(md_escape(cell) for cell in row) + " |")
    return "\n".join(out)


def split_addresses(raw: str) -> list[str]:
    return [addr for addr in re.split(r"[,;\s]+", raw or "") if addr]


RISK_ORDER = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


def max_risk(*levels: str) -> str:
    best = "NONE"
    for level in levels:
        if RISK_ORDER.get(level, 0) > RISK_ORDER[best]:
            best = level
    return best
