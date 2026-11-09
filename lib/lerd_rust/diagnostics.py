"""Read cargo's --message-format=json output into a compact list of errors
and warnings: what an assistant, the site tab and the logs need, and no more."""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# rustc's closing summaries carry no location and repeat what came before.
SUMMARY = re.compile(
    r"^(aborting due to |\d+ warnings? emitted|.* generated \d+ warnings?)", re.IGNORECASE
)
LIMIT = 50
RENDERED_LIMIT = 4000


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


class Collector:
    """Feed it cargo's stdout line by line."""

    def __init__(self) -> None:
        self._seen = set()  # type: set
        self.items = []  # type: List[Dict]
        self.success = None  # type: Optional[bool]
        self.executables = []  # type: List[Dict]

    def feed(self, line: str) -> Tuple[bool, Optional[str]]:
        """Returns (was_json, text_for_the_log). A line that is not one of
        cargo's JSON messages is returned unchanged for the log."""
        stripped = line.strip()
        if not stripped.startswith("{"):
            return False, line
        try:
            message = json.loads(stripped)
        except ValueError:
            return False, line
        if not isinstance(message, dict):
            return False, line
        reason = message.get("reason")
        if reason == "build-finished":
            self.success = bool(message.get("success"))
            return True, None
        target = message.get("target")
        target = target if isinstance(target, dict) else {}
        if reason == "compiler-artifact":
            executable = message.get("executable")
            kinds = target.get("kind") or []
            if isinstance(executable, str) and executable and ("bin" in kinds or "example" in kinds):
                self.executables.append({
                    "executable": executable,
                    "name": target.get("name"),
                    "package_id": message.get("package_id"),
                    "manifest_path": message.get("manifest_path"),
                })
            return True, None
        if reason != "compiler-message":
            return True, None
        diagnostic = message.get("message")
        if not isinstance(diagnostic, dict):
            # Not one of cargo's diagnostics: something a build script or
            # proc macro printed that happens to look like one.
            return False, line
        rendered = diagnostic.get("rendered")
        rendered = rendered if isinstance(rendered, str) else ""
        item = _item(diagnostic, target)
        if item is not None:
            key = (item["level"], item["message"], item["file"], item["line"], item["column"])
            if key in self._seen:
                return True, None
            self._seen.add(key)
            self.items.append(item)
        return True, rendered or None

    def summary(self, limit: int = LIMIT) -> Dict:
        errors = [d for d in self.items if d["level"] == "error"]
        warnings = [d for d in self.items if d["level"] == "warning"]
        ordered = errors + warnings
        return {
            "errors": len(errors),
            "warnings": len(warnings),
            "diagnostics": ordered[:limit],
            "truncated": len(ordered) > limit,
        }


def _item(diagnostic: Dict, target: Dict) -> Optional[Dict]:
    level = diagnostic.get("level")
    level = level if isinstance(level, str) else ""
    if level.startswith("error"):
        level = "error"  # includes "error: internal compiler error"
    elif level != "warning":
        return None  # notes, help and failure-notes belong to another message
    text = diagnostic.get("message")
    text = text if isinstance(text, str) else ""
    spans = [s for s in (diagnostic.get("spans") or []) if isinstance(s, dict)] \
        if isinstance(diagnostic.get("spans"), list) else []
    if not spans and SUMMARY.match(text):
        return None
    primary = next((s for s in spans if s.get("is_primary")), spans[0] if spans else None)
    code = diagnostic.get("code") or {}
    rendered = diagnostic.get("rendered")
    rendered = strip_ansi(rendered if isinstance(rendered, str) else text).rstrip()
    if len(rendered) > RENDERED_LIMIT:
        rendered = rendered[:RENDERED_LIMIT] + "\n..."
    return {
        "level": level,
        "message": text,
        "code": code.get("code") if isinstance(code, dict) else None,
        "file": primary.get("file_name") if primary else None,
        "line": primary.get("line_start") if primary else None,
        "column": primary.get("column_start") if primary else None,
        "target": target.get("name"),
        "rendered": rendered,
    }
