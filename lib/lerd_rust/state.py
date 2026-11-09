"""The dev server's last build, kept in the plugin's data folder so the site
tab, `lerd rust:status` and assistants can read it without parsing logs.

One small JSON file per checkout, keyed by its real path, so a site and each
of its worktrees keep their own."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Mapping, Optional

FORMAT = 1


def folder(env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    env = os.environ if env is None else env
    data = env.get("LERD_PLUGIN_DATA")
    if not data:
        return None
    return Path(data) / "builds"


def path_for(checkout: str, env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    base = folder(env)
    if base is None:
        return None
    key = hashlib.sha256(os.path.realpath(checkout).encode("utf-8")).hexdigest()[:20]
    return base / f"{key}.json"


def write(checkout: str, data: dict, env: Optional[Mapping[str, str]] = None) -> None:
    target = path_for(checkout, env)
    if target is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data, format=FORMAT, path=os.path.realpath(checkout), updated_at=time.time())
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(payload, out)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read(checkout: str, env: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    target = path_for(checkout, env)
    if target is None:
        return None
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    pid = data.get("pid")
    if data.get("phase") != "stopped" and not (isinstance(pid, int) and alive(pid)):
        # The watcher went away without saying so (killed, machine restarted).
        data["phase"] = "stopped"
        data["app"] = None
        data["building_since"] = None
        data["serving_previous"] = False
    return data


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
