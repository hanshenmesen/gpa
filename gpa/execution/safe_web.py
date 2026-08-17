"""Read-only public-Web replay without desktop input or browser automation.

This runner intentionally supports only deterministic source-verification
steps.  It never opens an application, sends keyboard/mouse events, touches the
system clipboard, or calls an LLM.  Public-network and redirect checks prevent
workflow URLs from turning the local GPA service into an SSRF client.
"""
from __future__ import annotations

import html.parser
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from http.client import IncompleteRead
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from gpa.execution.safety_gate import check_url_allowed
from gpa.storage.workflow import Workflow, WorkflowStep

SAFE_WEB_ACTIONS = frozenset({
    "open_url",
    "wait",
    "wait_for_text",
    "assert_text",
    "assert_not_text",
    "assert_link",
    "assert_url",
    "set_clipboard",
    "assert_clipboard",
})
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
SAFE_WEB_USER_AGENT = "GPA-SafeWeb/1.0 (+https://github.com/hanshenmesen/gpa)"


class SafeWebStepState(Enum):
    DONE = auto()
    FAILED = auto()


@dataclass
class SafeWebStepResult:
    step_number: int
    state: SafeWebStepState
    retries: int = 0
    error: str = ""
    duration_seconds: float = 0.0
    localization: None = None
    agent_decision_ms: float = 0.0
    agent_decision: dict = field(default_factory=dict)
    corrections: list[dict] = field(default_factory=list)
    observation_metrics: list[dict] = field(default_factory=list)
    postcondition_verified: Optional[bool] = None
    postcondition_reason: str = ""
    evidence_source: str = ""
    postcondition_attempts: int = 0


@dataclass
class SafeWebExecutionResult:
    workflow_name: str
    success: bool
    step_results: list[SafeWebStepResult] = field(default_factory=list)
    error: str = ""
    llm_metrics: list[dict] = field(default_factory=list)
    execution_mode: str = "safe_web"

    @property
    def n_steps(self) -> int:
        return len(self.step_results)

    @property
    def n_failed(self) -> int:
        return sum(item.state == SafeWebStepState.FAILED for item in self.step_results)


def safe_web_compatibility(workflow: Workflow) -> dict:
    unsupported = [
        {
            "step": int(step.step_number),
            "action_type": str(step.action_type or ""),
            "reason": "This action can affect an application or requires live browser state.",
        }
        for step in workflow.steps
        if str(step.action_type or "").strip().casefold() not in SAFE_WEB_ACTIONS
    ]
    urls = [
        str(step.value or "").strip()
        for step in workflow.steps
        if str(step.action_type or "").strip().casefold() == "open_url"
    ]
    url_issues = []
    for index, url in enumerate(urls, 1):
        error = static_public_url_error(url)
        if error:
            url_issues.append({"url_index": index, "error": error})
    runnable = bool(workflow.steps) and not unsupported and not url_issues and bool(urls)
    return {
        "schema": "gpa.safe-web-compatibility/v1",
        "runnable": runnable,
        "mode": "safe_web",
        "read_only": True,
        "uses_desktop_input": False,
        "uses_system_clipboard": False,
        "uses_llm": False,
        "supported_actions": sorted(SAFE_WEB_ACTIONS),
        "unsupported_steps": unsupported,
        "url_issues": url_issues,
        "public_url_count": len(urls),
        "reason": (
            "All steps can run as read-only public-Web verification."
            if runnable
            else "The workflow contains actions or URLs outside the safe Web profile."
        ),
    }


def static_public_url_error(url: str) -> str:
    target = str(url or "").strip()
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "Safe Web Replay requires an http(s) URL with a host."
    if parsed.username or parsed.password:
        return "Safe Web Replay does not allow URL credentials."
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith(".local"):
        return "Safe Web Replay does not allow local hosts."
    try:
        literal_address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal_address = None
    if literal_address is not None and not literal_address.is_global:
        return f"Safe Web Replay blocked non-public address {literal_address}."
    return check_url_allowed(target) or ""


def public_http_url_error(url: str) -> str:
    target = str(url or "").strip()
    static_error = static_public_url_error(target)
    if static_error:
        return static_error
    parsed = urlparse(target)
    host = parsed.hostname.casefold()
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        return f"Safe Web Replay host could not be resolved: {exc}"
    if not addresses:
        return "Safe Web Replay host did not resolve to an address."
    for raw_address in addresses:
        address = ipaddress.ip_address(raw_address.split("%", 1)[0])
        if not address.is_global:
            return f"Safe Web Replay blocked non-public address {address}."
    return ""


class _PublicRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urljoin(req.full_url, newurl)
        error = public_http_url_error(target)
        if error:
            raise ValueError(error)
        return super().redirect_request(req, fp, code, msg, headers, target)


class _VisibleTextParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag == "a":
            href = next(
                (str(value or "").strip() for name, value in attrs if name.casefold() == "href"),
                "",
            )
            if href:
                self.links.append(href)
        if normalized_tag in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())


def fetch_public_page(
    url: str,
    *,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    include_links: bool = False,
) -> tuple[str, str, int] | tuple[str, str, int, list[str]]:
    error = public_http_url_error(url)
    if error:
        raise ValueError(error)
    request = Request(
        url,
        headers={
            # Identify the read-only verifier honestly. Some public sites serve
            # an outage/error shell to browser-looking server clients while
            # allowing ordinary research clients and their public JSON APIs.
            "User-Agent": SAFE_WEB_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        },
    )
    opener = build_opener(_PublicRedirectHandler())
    with opener.open(request, timeout=max(0.5, min(float(timeout), 30.0))) as response:
        final_url = str(response.geturl() or url)
        redirect_error = public_http_url_error(final_url)
        if redirect_error:
            raise ValueError(redirect_error)
        content_type = str(response.headers.get("Content-Type") or "").casefold()
        if content_type and not any(
            token in content_type for token in ("text/", "html", "xml", "json")
        ):
            raise ValueError(f"Safe Web Replay response is not textual: {content_type}")
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError(f"Safe Web Replay response exceeds {max_bytes} bytes.")
        charset = response.headers.get_content_charset() or "utf-8"
        status = int(getattr(response, "status", 200) or 200)
    decoded = payload.decode(charset, errors="replace")
    if "html" not in content_type and "xml" not in content_type:
        return (final_url, decoded, status, []) if include_links else (final_url, decoded, status)
    parser = _VisibleTextParser()
    parser.feed(decoded)
    text = "\n".join(parser.parts)
    links = list(dict.fromkeys(urljoin(final_url, href) for href in parser.links))
    return (final_url, text, status, links) if include_links else (final_url, text, status)


class SafeWebRunner:
    def __init__(
        self,
        workflow: Workflow,
        *,
        variables: Optional[dict[str, str]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        on_step_start: Optional[Callable[[WorkflowStep], None]] = None,
    ) -> None:
        compatibility = safe_web_compatibility(workflow)
        if not compatibility["runnable"]:
            raise ValueError(compatibility["reason"])
        self.workflow = workflow
        self.variables = dict(variables or {})
        self.should_stop = should_stop or (lambda: False)
        self.on_step_start = on_step_start
        self.current_url = ""
        self.page_text = ""
        self.page_links: list[str] = []
        self.memory_clipboard = ""
        self._step_retries = 0

    def run(self) -> SafeWebExecutionResult:
        results: list[SafeWebStepResult] = []
        for step in self.workflow.steps:
            if self.should_stop():
                return SafeWebExecutionResult(
                    self.workflow.workflow_name,
                    False,
                    results,
                    "Safe Web Replay stopped.",
                )
            if self.on_step_start is not None:
                self.on_step_start(step)
            item = self._run_step(step)
            results.append(item)
            if item.state == SafeWebStepState.FAILED:
                return SafeWebExecutionResult(
                    self.workflow.workflow_name,
                    False,
                    results,
                    item.error,
                )
        return SafeWebExecutionResult(self.workflow.workflow_name, True, results)

    def _run_step(self, step: WorkflowStep) -> SafeWebStepResult:
        started = time.monotonic()
        result = SafeWebStepResult(step.step_number, SafeWebStepState.DONE)
        self._step_retries = 0
        try:
            action = str(step.action_type or "").strip().casefold()
            value = self._substitute(step.value)
            if action == "open_url":
                self.current_url, self.page_text, status, self.page_links = self._fetch(value, step)
                result.evidence_source = "public-http"
                result.postcondition_verified = status < 400
                result.postcondition_reason = f"Fetched public source with HTTP {status}."
                result.postcondition_attempts = 1
            elif action == "assert_url":
                if value.casefold() not in self.current_url.casefold():
                    raise AssertionError(
                        f"Expected URL fragment {value!r}; current source is {self.current_url or 'none'!r}."
                    )
                self._verified(result, "Public source URL matched.", "public-http")
            elif action == "wait_for_text":
                self._wait_for_text(value, step)
                self._verified(result, f"Found expected source text: {value}", "public-http")
            elif action == "assert_text":
                if value.casefold() not in self.page_text.casefold():
                    raise AssertionError(f"Expected public source text was not found: {value}")
                self._verified(result, f"Public source text matched: {value}", "public-http")
            elif action == "assert_not_text":
                if value.casefold() in self.page_text.casefold():
                    raise AssertionError(f"Unexpected public source text was found: {value}")
                self._verified(result, f"Public source text is absent as expected: {value}", "public-http")
            elif action == "assert_link":
                if not any(value.casefold() in link.casefold() for link in self.page_links):
                    raise AssertionError(f"Expected public source link was not found: {value}")
                self._verified(result, f"Public source link matched: {value}", "public-http")
            elif action == "set_clipboard":
                self.memory_clipboard = value
                self._verified(result, "Stored result in run-local memory.", "run-memory")
            elif action == "assert_clipboard":
                exact = bool((step.metadata or {}).get("exact", True))
                matches = self.memory_clipboard == value if exact else value.casefold() in self.memory_clipboard.casefold()
                if not matches:
                    raise AssertionError(f"Run-local result assertion failed; expected {value!r}.")
                self._verified(result, "Run-local result matched the expected answer.", "run-memory")
            elif action == "wait":
                seconds = float(value or (step.metadata or {}).get("seconds") or 0.5)
                self._sleep_interruptible(max(0.0, min(seconds, 30.0)))
                result.evidence_source = "timer"
            else:
                raise ValueError(f"Action is outside the Safe Web profile: {action}")
            if float(step.pause_duration or 0) > 0:
                self._sleep_interruptible(min(float(step.pause_duration), 1.0))
        except HTTPError as exc:
            result.state = SafeWebStepState.FAILED
            result.error = f"Public source returned HTTP {exc.code}: {urlparse(str(exc.url or '')).hostname or 'unknown host'}"
            result.evidence_source = "public-http"
        except Exception as exc:
            result.state = SafeWebStepState.FAILED
            result.error = str(exc)
        result.retries = self._step_retries
        result.duration_seconds = round(time.monotonic() - started, 3)
        return result

    def _wait_for_text(self, expected: str, step: WorkflowStep) -> None:
        expected = str(expected or "").strip()
        if not expected:
            raise ValueError("wait_for_text requires non-empty text.")
        if expected.casefold() in self.page_text.casefold():
            return
        metadata = step.metadata or {}
        timeout = max(0.1, min(float(metadata.get("timeout_seconds") or 10), 60.0))
        interval = max(0.1, min(float(metadata.get("poll_interval_seconds") or 1), 5.0))
        deadline = time.monotonic() + timeout
        attempts = 0
        while time.monotonic() < deadline:
            self._sleep_interruptible(min(interval, max(0.0, deadline - time.monotonic())))
            if self.should_stop():
                raise RuntimeError("Safe Web Replay stopped.")
            attempts += 1
            self.current_url, self.page_text, _, self.page_links = self._fetch(
                self.current_url,
                step,
                timeout=min(self._request_timeout(step), max(0.5, deadline - time.monotonic())),
            )
            if expected.casefold() in self.page_text.casefold():
                return
        raise TimeoutError(
            f"Timed out after {timeout:.1f}s waiting for public source text: {expected} "
            f"({attempts} refreshes)."
        )

    def _request_timeout(self, step: WorkflowStep) -> float:
        try:
            value = float((step.metadata or {}).get("request_timeout_seconds") or DEFAULT_REQUEST_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            value = DEFAULT_REQUEST_TIMEOUT_SECONDS
        return max(0.5, min(value, 30.0))

    def _fetch(
        self,
        url: str,
        step: WorkflowStep,
        *,
        timeout: Optional[float] = None,
    ) -> tuple[str, str, int, list[str]]:
        """Fetch with a small, cancellable retry budget for transient transport faults."""
        metadata = step.metadata or {}
        try:
            retry_limit = int(metadata.get("network_retries", 2))
        except (TypeError, ValueError):
            retry_limit = 2
        retry_limit = max(0, min(retry_limit, 3))
        host = urlparse(str(url or "")).hostname or "unknown host"
        for attempt in range(retry_limit + 1):
            if self.should_stop():
                raise RuntimeError("Safe Web Replay stopped.")
            try:
                response = fetch_public_page(
                    url,
                    timeout=timeout if timeout is not None else self._request_timeout(step),
                    include_links=True,
                )
                if len(response) == 3:
                    final_url, text, status = response
                    return final_url, text, status, []
                final_url, text, status, links = response
                return final_url, text, status, list(links)
            except HTTPError:
                # HTTP status failures are source outcomes, not transport noise.
                raise
            except (IncompleteRead, URLError, ConnectionResetError, TimeoutError, socket.timeout) as exc:
                if attempt >= retry_limit:
                    error_name = type(exc).__name__
                    raise RuntimeError(
                        f"Public source connection failed after {attempt + 1} attempts: "
                        f"{host} ({error_name})."
                    ) from None
                self._step_retries += 1
                self._sleep_interruptible(min(0.25 * (2 ** attempt), 1.0))
        raise RuntimeError(f"Public source connection failed: {host}.")

    def _sleep_interruptible(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self.should_stop():
                raise RuntimeError("Safe Web Replay stopped.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.05, remaining))

    def _substitute(self, value: str) -> str:
        output = str(value or "")
        for name, replacement in self.variables.items():
            output = output.replace(f"{{{{{name}}}}}", str(replacement))
        return output

    @staticmethod
    def _verified(result: SafeWebStepResult, reason: str, source: str) -> None:
        result.postcondition_verified = True
        result.postcondition_reason = reason
        result.postcondition_attempts = 1
        result.evidence_source = source
