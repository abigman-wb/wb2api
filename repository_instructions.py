"""Preserve repository guidance embedded in runtime reminder messages."""
from __future__ import annotations

import re
from collections.abc import Callable

_REMINDER = re.compile(r"<system-reminder\b[^>]*>(.*?)</system-reminder\s*>", re.I | re.S)
_PRESERVED = re.compile(
    r'<repository-instructions source="AGENTS\.md">(.*?)</repository-instructions>',
    re.S,
)
_SOURCE = re.compile(
    r"^[ \t]*Instructions from:[ \t]*(?P<source>[^\r\n]+?):[ \t]*(?:\r?\n|$)",
    re.I | re.M,
)
_INLINE_AGENTS = re.compile(
    r"^[ \t]*Instructions from:[ \t]*(?P<source>[^\r\n]*?AGENTS\.md[\"'`]?):[ \t]*",
    re.I | re.M,
)


def _is_agents_source(source: str) -> bool:
    name = source.strip().strip('`"\'').replace('\\', '/').rsplit('/', 1)[-1]
    return name.casefold() == 'agents.md'


def _instruction_spans(text: str):
    if '<' not in text:
        return []
    spans = [(m.start(), m.end(), m.group(1)) for m in _PRESERVED.finditer(text)]
    for reminder in _REMINDER.finditer(text):
        if any(start <= reminder.start() < end for start, end, _ in spans):
            continue
        inner = reminder.group(1)
        sources = {m.start(): m for m in _SOURCE.finditer(inner)}
        # Inline bodies can contain colons; prefer the explicit AGENTS.md boundary.
        sources.update({m.start(): m for m in _INLINE_AGENTS.finditer(inner)})
        headers = sorted(sources.values(), key=lambda m: m.start())
        for index, header in enumerate(headers):
            if not _is_agents_source(header.group('source')):
                continue
            end = headers[index + 1].start() if index + 1 < len(headers) else len(inner)
            body = inner[header.end():end]
            if body.strip():
                offset = reminder.start(1)
                spans.append((offset + header.start(), offset + end, body))
    return sorted(spans)


def extract_repository_instructions(text: str) -> tuple[str, list[str]]:
    """Remove only recognized source sections; keep their complete bodies separately."""
    parts, instructions = [], []
    cursor = 0
    for start, end, body in _instruction_spans(text):
        parts.append(text[cursor:start])
        instructions.append(
            '<repository-instructions source="AGENTS.md">' + body + '</repository-instructions>'
        )
        cursor = end
    parts.append(text[cursor:])
    return ''.join(parts), instructions


def rewrite_non_repository_text(text: str, rewrite: Callable[[str], str]) -> str:
    """Run template compatibility rewrites without modifying repository guidance."""
    parts = []
    cursor = 0
    for start, end, _body in _instruction_spans(text):
        parts.extend((rewrite(text[cursor:start]), text[start:end]))
        cursor = end
    parts.append(rewrite(text[cursor:]))
    return ''.join(parts)
