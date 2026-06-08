from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def http_form_request(url: str, fields: dict, headers: dict[str, str] | None = None, timeout: int = 60) -> dict:
    body = urlencode(fields).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc)) from exc
    return json.loads(text) if text.strip() else {}


def http_get_request(url: str, fields: dict, headers: dict[str, str] | None = None, timeout: int = 60) -> dict:
    query = urlencode(fields)
    separator = "&" if "?" in url else "?"
    request = Request(
        f"{url}{separator}{query}" if query else url,
        headers=headers or {},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc)) from exc
    return json.loads(text) if text.strip() else {}
