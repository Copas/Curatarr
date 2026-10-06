"""Bounded HTTP adapters for the three external applications."""

import base64
import ipaddress
import logging
import time
from urllib.parse import urlparse

import requests

from .observability import increment, log_operation


class IntegrationError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def normalized_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("A valid HTTP(S) integration URL is required")
    if parsed.query or parsed.fragment:
        raise ValueError("Integration URLs may not include a query or fragment")
    # Loopback, LAN, and public hosts are all valid deployment choices. Reject
    # addresses that can never be an ordinary server destination.
    try:
        address = ipaddress.ip_address(parsed.hostname)
        if address.is_multicast or address.is_unspecified or address.is_link_local:
            raise ValueError("Invalid integration address")
    except ValueError as exc:
        if str(exc) == "Invalid integration address":
            raise
    return value.strip().rstrip("/")


class Client:
    def __init__(self, base_url: str, api_key: str, session=None):
        self.base_url = normalized_url(base_url)
        self.api_key = api_key
        self.session = session or requests.Session()

    def request(
        self,
        method: str,
        path: str,
        *,
        params=None,
        json=None,
        data=None,
        headers=None,
        destructive=False,
    ):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Integration path must be relative to configured host")
        timeout = (5, 60 if destructive else 30)
        attempts = 3 if method.upper() == "GET" else 1
        integration = type(self).__name__.removesuffix("Client").lower()
        started = time.monotonic()
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    self.base_url + path,
                    params=params,
                    json=json,
                    data=data,
                    headers=self.headers() | (headers or {}),
                    timeout=timeout,
                )
                response.raise_for_status()
                break
            except requests.RequestException as exc:
                # Requests exception strings can include URLs and credentials.
                status_code = (
                    exc.response.status_code if exc.response is not None else None
                )
                if attempt + 1 == attempts or (
                    status_code and 400 <= status_code < 500
                ):
                    increment(f"external_api_failures.{integration}")
                    log_operation(
                        f"{method.upper()} {path}",
                        "error",
                        level=logging.WARNING,
                        integration=integration,
                        status_code=status_code,
                        duration_ms=round((time.monotonic() - started) * 1000),
                    )
                    raise IntegrationError(
                        f"{type(self).__name__} {method} {path} failed", status_code
                    ) from exc
                time.sleep(0.25 * 2**attempt)
        log_operation(
            f"{method.upper()} {path}",
            "ok",
            level=logging.DEBUG if method.upper() == "GET" else logging.INFO,
            integration=integration,
            status_code=getattr(response, "status_code", None),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        if not response.content:
            return None
        if response.headers.get("Content-Type", "").startswith("image/"):
            return response.content
        try:
            return response.json()
        except ValueError:
            return response.content

    def headers(self) -> dict:
        return {"X-Api-Key": self.api_key}

    def health(self):
        return self.request("GET", self.status_path)

    def version(self):
        status = self.health()
        if not isinstance(status, dict):
            raise IntegrationError("Integration health response is invalid")
        return status.get("Version") or status.get("version")


class JellyfinClient(Client):
    status_path = "/System/Info"

    def headers(self):
        # Jellyfin 12.1 rejects a bare X-Emby-Token header (401 even on
        # /System/Info); the key must be sent in the MediaBrowser Authorization
        # scheme. Sign-in can happen before an API key exists (first run).
        if not self.api_key:
            return {}
        return self._client_header("curatarr-server", self.api_key)

    @staticmethod
    def _client_header(device_id, token=None):
        value = (
            f'MediaBrowser Client="Curatarr", Device="Curatarr", '
            f'DeviceId="{device_id}", Version="0.1.0"'
        )
        if token:
            value += f', Token="{token}"'
        return {"Authorization": value, "X-Emby-Authorization": value}

    def authenticate(self, username, password, device_id):
        """Verify a user's own credentials; returns Jellyfin's user and token."""
        return self.request(
            "POST",
            "/Users/AuthenticateByName",
            json={"Username": username, "Pw": password},
            headers=self._client_header(device_id),
        )

    def end_session(self, token, device_id):
        """Revoke the access token created by authenticate()."""
        return self.request(
            "POST",
            "/Sessions/Logout",
            headers=self._client_header(device_id, token),
        )

    def user(self, user_id):
        return self.request("GET", f"/Users/{user_id}")

    def users(self):
        return self.request("GET", "/Users")

    def libraries(self):
        return self.request("GET", "/Library/VirtualFolders")

    def item(self, item_id):
        return self.request("GET", f"/Items/{item_id}")

    def items(self, parent_id=None, start=0, limit=100):
        params = {
            "Recursive": "true",
            # Jellyfin otherwise lists a collection (e.g. a SmartLists box set)
            # in place of its member movies, hiding most of a movie library.
            "CollapseBoxSetItems": "false",
            "StartIndex": start,
            "Limit": limit,
            "Fields": "ProviderIds,DateCreated,MediaSources",
        }
        if parent_id:
            params["ParentId"] = parent_id
        return self.request("GET", "/Items", params=params)

    def user_items(self, user_id, parent_id=None, start=0, limit=100):
        params = {
            "Recursive": "true",
            "CollapseBoxSetItems": "false",
            "StartIndex": start,
            "Limit": limit,
            "Fields": "ProviderIds,DateCreated",
        }
        if parent_id:
            params["ParentId"] = parent_id
        return self.request("GET", f"/Users/{user_id}/Items", params=params)

    def image(self, item_id):
        return self.request("GET", f"/Items/{item_id}/Images/Primary")

    def put_image(self, item_id, image_bytes):
        return self.request(
            "POST",
            f"/Items/{item_id}/Images/Primary",
            data=base64.b64encode(image_bytes).decode("ascii"),
            headers={"Content-Type": "image/jpeg"},
        )

    def mark_played(self, user_id, item_id):
        return self.request("POST", f"/Users/{user_id}/PlayedItems/{item_id}")


class SonarrClient(Client):
    status_path = "/api/v3/system/status"

    def series(self):
        return self.request("GET", "/api/v3/series")

    def episodes(self, series_id):
        return self.request("GET", "/api/v3/episode", params={"seriesId": series_id})

    def episode_files(self, series_id):
        return self.request(
            "GET", "/api/v3/episodefile", params={"seriesId": series_id}
        )

    def queue(self):
        return self.request("GET", "/api/v3/queue/details")

    def commands(self):
        return self.request("GET", "/api/v3/command")

    def diskspace(self):
        return self.request("GET", "/api/v3/diskspace")

    def monitor_episode(self, episode_id):
        """Monitor one episode; returns True only if it was unmonitored before."""
        episode = self.request("GET", f"/api/v3/episode/{episode_id}")
        if episode["monitored"]:
            return False
        episode["monitored"] = True
        self.request("PUT", f"/api/v3/episode/{episode_id}", json=episode)
        return True

    def monitor_season(self, series_id, season_number):
        """Monitor a whole season; returns True only if it was unmonitored before.

        Sonarr monitors an episode it learns about later (a newly announced one)
        only when its season is monitored, so monitoring the episodes alone misses
        them. Sonarr also monitors the season's existing episodes when the season
        flag changes.
        """
        series = self.request("GET", f"/api/v3/series/{series_id}")
        for season in series.get("seasons", []):
            if season.get("seasonNumber") == season_number:
                if season.get("monitored"):
                    return False
                season["monitored"] = True
                self.request("PUT", f"/api/v3/series/{series_id}", json=series)
                return True
        return False

    def season_search(self, series_id, season_number):
        return self.request(
            "POST",
            "/api/v3/command",
            json={
                "name": "SeasonSearch",
                "seriesId": series_id,
                "seasonNumber": season_number,
            },
        )

    def unmonitor_episode(self, episode_id):
        """Unmonitor one episode; returns True only if it was monitored before."""
        episode = self.request("GET", f"/api/v3/episode/{episode_id}")
        if not episode["monitored"]:
            return False
        episode["monitored"] = False
        self.request("PUT", f"/api/v3/episode/{episode_id}", json=episode)
        return True

    def delete_episode_file(self, file_id):
        return self.request(
            "DELETE", f"/api/v3/episodefile/{file_id}", destructive=True
        )


class RadarrClient(Client):
    status_path = "/api/v3/system/status"

    def movies(self):
        return self.request("GET", "/api/v3/movie")

    def movie(self, movie_id):
        return self.request("GET", f"/api/v3/movie/{movie_id}")

    def queue(self):
        return self.request("GET", "/api/v3/queue/details")

    def diskspace(self):
        return self.request("GET", "/api/v3/diskspace")

    def delete_movie(self, movie_id):
        return self.request(
            "DELETE",
            f"/api/v3/movie/{movie_id}",
            params={"deleteFiles": "true", "addImportExclusion": "false"},
            destructive=True,
        )


CLIENTS = {"jellyfin": JellyfinClient, "sonarr": SonarrClient, "radarr": RadarrClient}
