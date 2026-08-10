"""Document-guided execution helpers for GUI replay.

This module implements the useful DocOS-style idea without depending on
DocOS repository code: retrieve or paste procedural documentation, distill it
into compact hints, then expose those hints to the replay agent.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)


DOC_CONTEXT_VARIABLE_NAMES = (
    "documentation_context",
    "doc_context",
    "docs_context",
    "official_documentation",
    "guide_context",
)

_ACTION_TERMS = (
    "click",
    "select",
    "choose",
    "open",
    "navigate",
    "type",
    "enter",
    "press",
    "copy",
    "paste",
    "save",
    "run",
    "enable",
    "disable",
    "install",
    "configure",
    "点击",
    "选择",
    "打开",
    "导航",
    "输入",
    "按",
    "复制",
    "粘贴",
    "保存",
    "运行",
    "启用",
    "禁用",
    "安装",
    "配置",
)


@dataclass(frozen=True)
class DocumentationHint:
    section: str
    instruction: str

    def to_dict(self) -> dict[str, str]:
        return {
            "section": self.section,
            "instruction": self.instruction,
        }


# ──────────────────────────────────────────────────────────────────────────── #
# Retrieval-augmented documentation (RAG-GUI style, arXiv:2509.24183)          #
# ──────────────────────────────────────────────────────────────────────────── #

_doc_retriever: Optional[Callable[[list[str]], str]] = None


def register_doc_retriever(retriever: Optional[Callable[[list[str]], str]]) -> None:
    """Register a callable that maps search queries to documentation text.

    The retriever receives a list of query strings and returns raw doc/tutorial
    text (plain, HTML, or Markdown). None clears it. Disabled by default so the
    core has no network dependency; callers plug in web-tutorial retrieval.
    """
    global _doc_retriever
    if retriever is not None and not callable(retriever):
        raise TypeError("doc retriever must be callable or None.")
    _doc_retriever = retriever


def clear_doc_retriever() -> None:
    global _doc_retriever
    _doc_retriever = None


def retrieve_documentation(queries: Iterable[str], *, max_chars: int = 20000) -> str:
    """Fetch documentation text for queries via the registered retriever.

    Never raises: a failing retriever degrades to an empty string.
    """
    if _doc_retriever is None:
        return ""
    query_list = [str(q).strip() for q in queries if str(q or "").strip()]
    if not query_list:
        return ""
    try:
        text = _doc_retriever(query_list)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Documentation retriever failed: %s", exc, exc_info=True)
        return ""
    return str(text or "")[:max_chars]


def documentation_context_from_variables(variables: Mapping[str, str]) -> tuple[str, str]:
    """Return the first runtime variable that looks like pasted documentation."""
    for name in DOC_CONTEXT_VARIABLE_NAMES:
        value = str(variables.get(name, "") or "").strip()
        if value:
            return name, value
    for name, value in variables.items():
        lowered = str(name or "").casefold()
        if "doc" not in lowered and "guide" not in lowered:
            continue
        text = str(value or "").strip()
        if text:
            return str(name), text
    return "", ""


def build_document_search_queries(
    task_description: str,
    *,
    workflow_title: str = "",
    app_name: str = "",
    max_queries: int = 4,
) -> list[str]:
    """Build compact official-documentation search queries for long-tail tasks."""
    text = " ".join([workflow_title or "", task_description or ""]).strip()
    terms = _important_terms(text)
    app = _normalise_app_name(app_name)
    queries: list[str] = []
    if app and terms:
        queries.append(f"{app} official documentation {' '.join(terms[:6])}")
        queries.append(f"{app} guide {' '.join(terms[:6])}")
    if terms:
        queries.append(f"official documentation {' '.join(terms[:8])}")
        queries.append(f"how to {' '.join(terms[:8])}")
    elif app:
        queries.append(f"{app} official documentation")
    return _dedupe(queries)[:max_queries]


def extract_documentation_hints(text: str, *, max_hints: int = 12) -> list[DocumentationHint]:
    """Extract header-aware procedural hints from pasted docs or HTML/Markdown."""
    clean = _normalise_document_text(text)
    if not clean:
        return []

    hints: list[DocumentationHint] = []
    section = ""
    pending = ""
    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = _heading_text(line)
        if heading:
            section = heading
            pending = ""
            continue
        if _looks_like_prerequisite(line):
            pending = line
            continue
        instruction = _instruction_text(line)
        if instruction:
            if pending and len(instruction) < 80:
                instruction = f"{pending} {instruction}"
            hints.append(DocumentationHint(section=section, instruction=instruction))
            pending = ""
        if len(hints) >= max_hints:
            break
    return hints


def document_guidance_payload(
    workflow,
    variables: Mapping[str, str],
    *,
    current_step=None,
    max_hints: int = 12,
) -> dict:
    """Build the structured payload consumed by the replay decision prompt."""
    source_name, doc_text = documentation_context_from_variables(variables)
    task_description = str(getattr(workflow, "task_description", "") or "")
    workflow_title = str(getattr(workflow, "workflow_title", "") or "")
    app_name = str(getattr(current_step, "active_app_name", "") or "")
    search_queries = build_document_search_queries(
        task_description,
        workflow_title=workflow_title,
        app_name=app_name,
    )
    hints = extract_documentation_hints(doc_text, max_hints=max_hints)

    # Retrieval-augmented fallback: when the user pasted no documentation but a
    # retriever is registered, fetch tutorial text for the search queries and
    # distill hints from it. Disabled by default (no retriever => no change).
    retrieved = False
    if not hints:
        fetched = retrieve_documentation(search_queries)
        if fetched.strip():
            retrieved_hints = extract_documentation_hints(fetched, max_hints=max_hints)
            if retrieved_hints:
                hints = retrieved_hints
                retrieved = True

    return {
        "available": bool(doc_text.strip()) or retrieved,
        "source_variable": source_name or ("retriever" if retrieved else ""),
        "retrieved": retrieved,
        "search_queries": search_queries,
        "hints": [hint.to_dict() for hint in hints],
    }


def _normalise_document_text(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|li|h[1-6]|div|section|article)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _heading_text(line: str) -> str:
    markdown = re.match(r"^(#{1,6})\s+(.+)$", line)
    if markdown:
        return markdown.group(2).strip(" #:")
    if len(line) <= 80 and not _instruction_text(line):
        if line.endswith(":") or line.istitle():
            return line.strip(" :")
    return ""


def _instruction_text(line: str) -> str:
    match = re.match(r"^(?:[-*+]|\d+[.)])\s+(.+)$", line)
    candidate = match.group(1).strip() if match else line
    lowered = candidate.casefold()
    if any(term in lowered for term in _ACTION_TERMS):
        return candidate
    return ""


def _looks_like_prerequisite(line: str) -> bool:
    lowered = line.casefold()
    return any(token in lowered for token in ("before", "prerequisite", "required", "先", "前提", "需要"))


def _important_terms(text: str) -> list[str]:
    quoted = re.findall(r"['\"]([^'\"]{3,80})['\"]", text)
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+.-]{2,}|[\u4e00-\u9fff]{2,}", text)
    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "then",
        "this",
        "that",
        "打开",
        "然后",
        "使用",
    }
    values: list[str] = []
    for item in [*quoted, *tokens]:
        item = item.strip()
        if not item or item.casefold() in stop:
            continue
        values.append(item)
    return _dedupe(values)


def _normalise_app_name(app_name: str) -> str:
    app = str(app_name or "").strip()
    if app.casefold() in {"google chrome", "chrome"}:
        return "Chrome"
    return app


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
