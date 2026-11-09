"""Lerd's own API, reached with the token Lerd gives this plugin. The token is
only ever sent to LERD_API_URL and never printed or written anywhere."""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping, Optional, Tuple

TAG = re.compile(r"[^a-z0-9-]+")


def _request(path: str, env: Mapping[str, str], body: Optional[dict] = None, timeout: float = 5):
    url = env.get("LERD_API_URL")
    token = env.get("LERD_API_TOKEN")
    if not url or not token:
        return None
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url.rstrip("/") + path, data=data, method="POST" if data else "GET")
    request.add_header("X-Lerd-Plugin-Token", token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    # Lerd's API is local: never send the token through a proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def sites(env: Optional[Mapping[str, str]] = None) -> Optional[list]:
    env = os.environ if env is None else env
    try:
        response = _request("/api/sites", env)
        if response is None:
            return None
        with response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        return None
    return data if isinstance(data, list) else None


def notify(title: str, body: str, site: str, env: Optional[Mapping[str, str]] = None) -> Tuple[bool, str]:
    """Send a notification. Returns (sent, reason); a refusal is the user's
    choice or an older Lerd, never an error worth failing over."""
    env = os.environ if env is None else env
    payload = {"title": title[:100], "body": body[:300]}
    if site:
        payload["url"] = "#sites/" + urllib.parse.quote(site, safe="-_.")
        tag = TAG.sub("-", ("build-" + site).lower()).strip("-")[:64]
        if tag:
            payload["tag"] = tag
    try:
        response = _request("/api/notifications/plugin", env, payload)
        if response is None:
            return False, "no Lerd API in this environment"
        with response:
            return True, "sent"
    except urllib.error.HTTPError as error:
        if error.code == 403:
            return False, "notifications are not allowed for this plugin"
        return False, f"Lerd answered {error.code}"
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as error:
        return False, f"Lerd's API is unreachable: {error}"
