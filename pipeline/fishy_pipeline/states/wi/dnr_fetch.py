# SPDX-License-Identifier: GPL-3.0-only
"""Safe HTTP fetch helpers for apps.dnr.wi.gov document bytes."""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path

from fishy_pipeline.states.wi.media import (
    cache_extension,
    normalize_content_type,
    sniff_media_kind,
)

ALLOWED_HOST = "apps.dnr.wi.gov"


class OffHostRedirectError(RuntimeError):
    pass


@dataclass(frozen=True)
class DnrDocument:
    body: bytes
    content_type: str | None
    media_kind: str


class DnrRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        host = urllib.parse.urlparse(newurl).hostname
        if host != ALLOWED_HOST:
            raise OffHostRedirectError(f"refusing off-host redirect to {host!r}")
        return urllib.request.Request(
            newurl,
            data=req.data,
            headers=dict(req.header_items()),
            method=req.get_method(),
        )


def assert_dnr_apps_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != ALLOWED_HOST:
        raise ValueError(f"URL must be on {ALLOWED_HOST}: {url}")


def build_dnr_opener(cookie_jar: CookieJar | None = None) -> urllib.request.OpenerDirector:
    jar = cookie_jar if cookie_jar is not None else CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        DnrRedirectHandler(),
    )


def fetch_dnr_bytes(
    url: str,
    *,
    opener: urllib.request.OpenerDirector,
    data: bytes | None = None,
    user_agent: str,
    method: str | None = None,
) -> tuple[bytes, str | None]:
    assert_dnr_apps_url(url)
    resolved_method = method or ("POST" if data is not None else "GET")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": user_agent},
        method=resolved_method,
    )
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(request, timeout=120) as response:
            final_host = urllib.parse.urlparse(response.url).hostname
            if final_host != ALLOWED_HOST:
                raise OffHostRedirectError(f"final URL host is {final_host!r}, not {ALLOWED_HOST}")
            content_type = normalize_content_type(response.headers.get("Content-Type"))
            return response.read(), content_type
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"DNR request failed ({error.code}): {url}") from error


def fetch_dnr_document(
    url: str,
    dest: Path,
    *,
    opener: urllib.request.OpenerDirector,
    user_agent: str,
    rate_limit_seconds: float = 0.0,
) -> DnrDocument:
    """Download a document once and write ``dest`` with the correct extension."""
    assert_dnr_apps_url(url)
    dest = Path(dest)
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    stem = dest.stem
    time.sleep(max(0.0, rate_limit_seconds))
    body, content_type = fetch_dnr_bytes(url, opener=opener, user_agent=user_agent)
    media_kind = sniff_media_kind(body, content_type)
    final = parent / f"{stem}.{cache_extension(media_kind)}"
    if final != dest and dest.is_file():
        dest.unlink(missing_ok=True)
    if not final.is_file():
        final.write_bytes(body)
    return DnrDocument(body=body, content_type=content_type, media_kind=media_kind)