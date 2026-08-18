"""Privacy-conscious release discovery for the GPA desktop application.

The updater deliberately stops at discovery: it never installs an unsigned
artifact or grants desktop authority.  Signed builds may add an installer on
top of this contract later without weakening the current trust boundary.
"""
from __future__ import annotations

import json
import os
import platform
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from gpa import __release__
from gpa.runtime_config import user_data_path

RELEASES_API = "https://api.github.com/repos/hanshenmesen/gpa/releases"
RELEASES_PAGE = "https://github.com/hanshenmesen/gpa/releases"
MAX_RESPONSE_BYTES = 512 * 1024
DEFAULT_CACHE_SECONDS = 6 * 60 * 60
_RELEASE_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<channel>preview|beta|rc)\.(?P<serial>\d+))?$",
    re.IGNORECASE,
)


class UpdateCheckError(RuntimeError):
    """A safe, user-presentable update discovery failure."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    byte_size: int


@dataclass(frozen=True)
class ReleaseInfo:
    release: str
    title: str
    page_url: str
    published_at: str
    prerelease: bool
    assets: tuple[ReleaseAsset, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["assets"] = [asdict(asset) for asset in self.assets]
        return value


def release_key(value: str) -> tuple[int, int, int, int, int]:
    """Return an orderable key; stable releases sort after their previews."""
    match = _RELEASE_RE.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError(f"Unsupported GPA release identifier: {value}")
    channel = (match.group("channel") or "stable").casefold()
    channel_rank = {"preview": 0, "beta": 1, "rc": 2, "stable": 3}[channel]
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        channel_rank,
        int(match.group("serial") or 0),
    )


def update_available(current: str, candidate: str) -> bool:
    return release_key(candidate) > release_key(current)


class DesktopUpdateService:
    def __init__(
        self,
        *,
        cache_path: str | Path | None = None,
        cache_seconds: int = DEFAULT_CACHE_SECONDS,
        current_release: str = __release__,
        releases_api: str = RELEASES_API,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path else user_data_path("GPA") / "update-status.json"
        self.cache_seconds = max(60, int(cache_seconds))
        self.current_release = current_release
        self.releases_api = releases_api

    def status(self) -> dict[str, Any]:
        cached = self._read_cache()
        latest = cached.get("latest") if isinstance(cached.get("latest"), Mapping) else None
        return self._payload(latest, checked_at=int(cached.get("checked_at") or 0), error="")

    def check(self, *, force: bool = False) -> dict[str, Any]:
        cached = self._read_cache()
        checked_at = int(cached.get("checked_at") or 0)
        if not force and checked_at and time.time() - checked_at < self.cache_seconds:
            latest = cached.get("latest") if isinstance(cached.get("latest"), Mapping) else None
            return self._payload(latest, checked_at=checked_at, error="", cached=True)
        try:
            releases = self._fetch_releases(etag=str(cached.get("etag") or ""))
        except UpdateCheckError as exc:
            latest = cached.get("latest") if isinstance(cached.get("latest"), Mapping) else None
            return self._payload(latest, checked_at=checked_at, error=str(exc), cached=bool(latest))
        if releases is None:
            cached["checked_at"] = int(time.time())
            self._write_cache(cached)
            latest = cached.get("latest") if isinstance(cached.get("latest"), Mapping) else None
            return self._payload(latest, checked_at=int(cached["checked_at"]), error="", cached=True)
        items, etag = releases
        # Preview users must still advance to a newer stable release.  The list
        # is already ordered across stable and prerelease channels.
        latest = items[0] if items else None
        payload = {
            "checked_at": int(time.time()),
            "etag": etag,
            "latest": latest.to_dict() if latest else None,
        }
        self._write_cache(payload)
        return self._payload(payload["latest"], checked_at=payload["checked_at"], error="")

    def _payload(
        self,
        latest: Mapping[str, Any] | None,
        *,
        checked_at: int,
        error: str,
        cached: bool = False,
    ) -> dict[str, Any]:
        release = str((latest or {}).get("release") or "")
        try:
            available = bool(release and update_available(self.current_release, release))
        except ValueError:
            available = False
        return {
            "schema": "gpa.desktop-update/v1",
            "current_release": self.current_release,
            "latest": dict(latest) if latest else None,
            "update_available": available,
            "checked_at": checked_at,
            "cached": cached,
            "error": str(error or "")[:300],
            "release_page": RELEASES_PAGE,
            "architecture": platform.machine() or "unknown",
            "installation": "manual_preview",
            "desktop_authority_changed": False,
        }

    def _fetch_releases(self, *, etag: str = "") -> tuple[list[ReleaseInfo], str] | None:
        parts = urlsplit(self.releases_api)
        if parts.scheme != "https" or parts.hostname != "api.github.com":
            raise UpdateCheckError("Update source is not an approved HTTPS endpoint.")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"GPA-Desktop/{self.current_release}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if etag:
            headers["If-None-Match"] = etag[:256]
        request = Request(self.releases_api, headers=headers)
        try:
            with urlopen(request, timeout=8) as response:
                final = urlsplit(response.geturl())
                if final.scheme != "https" or final.hostname != "api.github.com":
                    raise UpdateCheckError("Update service redirected outside the approved host.")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise UpdateCheckError("Update response is unexpectedly large.")
                response_etag = str(response.headers.get("ETag") or "")[:256]
        except HTTPError as exc:
            if exc.code == 304:
                return None
            raise UpdateCheckError(f"Update service returned HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise UpdateCheckError("Could not reach the update service.") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateCheckError("Update service returned invalid data.") from exc
        if not isinstance(value, list):
            raise UpdateCheckError("Update service returned invalid data.")
        releases = []
        for item in value[:30]:
            parsed = _parse_release(item)
            if parsed is not None:
                releases.append(parsed)
        releases.sort(key=lambda item: release_key(item.release), reverse=True)
        return releases, response_etag

    def _read_cache(self) -> dict[str, Any]:
        if not self.cache_path.is_file():
            return {}
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}

    def _write_cache(self, payload: Mapping[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".update-status.", dir=self.cache_path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(dict(payload), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.cache_path)
        finally:
            temporary.unlink(missing_ok=True)


def _parse_release(value: object) -> ReleaseInfo | None:
    if not isinstance(value, Mapping) or value.get("draft") is True:
        return None
    release = str(value.get("tag_name") or "").removeprefix("v")
    try:
        release_key(release)
    except ValueError:
        return None
    page = str(value.get("html_url") or "")
    page_parts = urlsplit(page)
    if page_parts.scheme != "https" or page_parts.hostname != "github.com":
        return None
    assets: list[ReleaseAsset] = []
    for raw_asset in value.get("assets") or []:
        if not isinstance(raw_asset, Mapping):
            continue
        url = str(raw_asset.get("browser_download_url") or "")
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "github.com":
            continue
        assets.append(
            ReleaseAsset(
                name=str(raw_asset.get("name") or "")[:180],
                download_url=url,
                byte_size=max(0, int(raw_asset.get("size") or 0)),
            )
        )
    return ReleaseInfo(
        release=release,
        title=str(value.get("name") or release)[:180],
        page_url=page,
        published_at=str(value.get("published_at") or "")[:64],
        prerelease=value.get("prerelease") is True,
        assets=tuple(assets),
    )


__all__ = [
    "DesktopUpdateService",
    "ReleaseAsset",
    "ReleaseInfo",
    "UpdateCheckError",
    "release_key",
    "update_available",
]
