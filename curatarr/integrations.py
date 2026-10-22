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
        read_timeout=None,
        attempts=None,
    ):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Integration path must be relative to configured host")
        timeout = (5, read_timeout or (60 if destructive else 30))
        attempts = attempts or (3 if method.upper() == "GET" else 1)
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
            f'DeviceId="{device_id}", Version="0.2.0rc5"'
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
        """One item with its provider IDs and image tags.

        Uses the item list filtered by id: since Jellyfin 12.2, GET /Items/{id}
        with an API key and no user fails ("Guid can't be empty"), which blocked
        every deletion check and artwork update.
        """
        data = self.request(
            "GET", "/Items", params={"Ids": item_id, "Fields": "ProviderIds"}
        )
        items = data.get("Items") if isinstance(data, dict) else None
        if not items:
            raise IntegrationError("Jellyfin item not found")
        return items[0]

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

    def items_trickplay(self, item_ids):
        """Items with their scrubbing thumbnail sets ("Trickplay", empty when
        Jellyfin has none)."""
        data = self.request(
            "GET", "/Items", params={"Ids": ",".join(item_ids), "Fields": "Trickplay"}
        )
        return (data or {}).get("Items") or []

    def image(self, item_id, kind="Primary"):
        """One of an item's images: Primary (poster), Thumb (the wide card),
        or Backdrop (its first one)."""
        return self.request("GET", f"/Items/{item_id}/Images/{_image_path(kind)}")

    def put_image(self, item_id, image_bytes, kind="Primary"):
        return self.request(
            "POST",
            f"/Items/{item_id}/Images/{_image_path(kind)}",
            data=base64.b64encode(image_bytes).decode("ascii"),
            headers={"Content-Type": "image/jpeg"},
        )

    def mark_played(self, user_id, item_id):
        return self.request("POST", f"/Users/{user_id}/PlayedItems/{item_id}")

    def sessions(self):
        return self.request("GET", "/Sessions")

    def refresh_images(self, item_id):
        """Download an item's artwork afresh from its metadata providers."""
        return self.request(
            "POST",
            f"/Items/{item_id}/Refresh",
            params={
                "MetadataRefreshMode": "Default",
                "ImageRefreshMode": "FullRefresh",
                "ReplaceAllMetadata": "false",
                "ReplaceAllImages": "true",
            },
        )

    def refresh_item(self, item_id):
        """Re-read one item's file so Jellyfin picks up changed chapters.

        A default refresh re-probes media whose file changed and keeps edited
        metadata and images; it does not touch the rest of the library.
        """
        return self.request(
            "POST",
            f"/Items/{item_id}/Refresh",
            params={
                "MetadataRefreshMode": "Default",
                "ImageRefreshMode": "Default",
                "ReplaceAllMetadata": "false",
                "ReplaceAllImages": "false",
            },
        )


def _image_path(kind):
    return "Backdrop/0" if kind == "Backdrop" else kind


class IntroSkipper:
    """The Intro Skipper plugin's segment API, reached through Jellyfin.

    Recaps Curatarr finds are added as user segments, which the plugin's own
    analysis leaves alone; removing one only hides it (it can be restored).
    """

    def __init__(self, jellyfin: "JellyfinClient"):
        self.jellyfin = jellyfin

    def segments(self, item_id) -> list[dict] | None:
        """The item's segments, or None when the plugin isn't installed."""
        try:
            response = self.jellyfin.request("GET", f"/Episode/{item_id}/Segments")
        except IntegrationError as exc:
            if exc.status_code == 404:
                return None
            raise
        return response or []

    def add(self, item_id, kind: str, start: float, end: float) -> str:
        """Add a user segment ("Recap", "Preview", …); returns its id."""
        created = self.jellyfin.request(
            "POST",
            f"/Episode/{item_id}/Segments",
            json={"Type": kind, "Start": round(start, 3), "End": round(end, 3)},
        )
        return created["Id"]

    def restore(self, item_id, segment_id):
        """Bring back a segment that was hidden."""
        self.jellyfin.request(
            "POST", f"/Episode/{item_id}/Segments/{segment_id}/Restore"
        )

    def hide(self, item_id, segment_id):
        self.jellyfin.request(
            "DELETE", f"/Episode/{item_id}/Segments/{segment_id}", destructive=True
        )


def _ignore_releases(client, item_path, label, pattern, name):
    """Ignore releases matching `pattern` for one series or film only: a release
    profile limited to a tag that only that item gets."""
    tags = client.request("GET", "/api/v3/tag")
    tag = next((t for t in tags if t.get("label") == label), None) or client.request(
        "POST", "/api/v3/tag", json={"label": label}
    )
    profiles = client.request("GET", "/api/v3/releaseprofile")
    profile = next((p for p in profiles if tag["id"] in (p.get("tags") or [])), None)
    if profile is None:
        client.request(
            "POST",
            "/api/v3/releaseprofile",
            json={
                "name": name,
                "enabled": True,
                "required": [],
                "ignored": [pattern],
                "indexerId": 0,
                "tags": [tag["id"]],
            },
        )
    elif pattern not in (profile.get("ignored") or []):
        profile["ignored"] = [*(profile.get("ignored") or []), pattern]
        client.request("PUT", f"/api/v3/releaseprofile/{profile['id']}", json=profile)
    item = client.request("GET", item_path)
    if tag["id"] not in (item.get("tags") or []):
        item["tags"] = [*(item.get("tags") or []), tag["id"]]
        client.request("PUT", item_path, json=item)


class SonarrClient(Client):
    status_path = "/api/v3/system/status"

    def series(self):
        return self.request("GET", "/api/v3/series")

    def import_lists(self):
        return self.request("GET", "/api/v3/importlist")

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

    def set_episodes_monitored(self, episode_ids, monitored):
        """Bulk monitor or unmonitor episodes in one request."""
        if not episode_ids:
            return None
        return self.request(
            "PUT",
            "/api/v3/episode/monitor",
            json={"episodeIds": sorted(episode_ids), "monitored": monitored},
        )

    def releases(self, episode_id):
        """Every release the indexers have for an episode, with Sonarr's verdict
        on each (this asks every indexer, so it can take a minute or two)."""
        return self.request(
            "GET",
            "/api/v3/release",
            params={"episodeId": episode_id},
            read_timeout=300,
            attempts=1,
        )

    def ignore_for_series(self, series_id, label, pattern, name):
        """Make Sonarr ignore releases matching `pattern` for this series only."""
        _ignore_releases(self, f"/api/v3/series/{series_id}", label, pattern, name)

    def series_monitored(self, series_id):
        series = self.request("GET", f"/api/v3/series/{series_id}")
        return bool(series.get("monitored"))

    def monitor_series(self, series_id):
        """Monitor the series itself (not its seasons); True if it was unmonitored.

        Sonarr does not search for an unmonitored series even when its episodes
        are monitored, so a request for a show Sonarr has unmonitored (for example
        by a nightly "unmonitor ended shows" job) needs this first.
        """
        series = self.request("GET", f"/api/v3/series/{series_id}")
        if series.get("monitored"):
            return False
        series["monitored"] = True
        self.request("PUT", f"/api/v3/series/{series_id}", json=series)
        return True

    def unmonitor_seasons(self, series_id):
        """Unmonitor every season of a series; returns the season numbers changed.

        With a season unmonitored, Sonarr adds episodes it learns about later as
        unmonitored too, so nothing new downloads until Curatarr requests it.
        Sonarr also unmonitors each season's episodes when the flag changes.
        """
        series = self.request("GET", f"/api/v3/series/{series_id}")
        changed = [
            season["seasonNumber"]
            for season in series.get("seasons", [])
            if season.get("monitored")
        ]
        if changed:
            for season in series["seasons"]:
                season["monitored"] = False
            self.request("PUT", f"/api/v3/series/{series_id}", json=series)
        return changed

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

    def episode_grabs(self, episode_id):
        """The episode's download history, newest first, grabs only."""
        page = self.request(
            "GET",
            "/api/v3/history",
            params={
                "episodeId": episode_id,
                "eventType": 1,
                "sortKey": "date",
                "sortDirection": "descending",
                "pageSize": 20,
            },
        )
        rows = page.get("records", []) if isinstance(page, dict) else page or []
        return sorted(rows, key=lambda r: r.get("date") or "", reverse=True)

    def mark_failed(self, history_id):
        """Blocklist the release behind a grab, so it isn't downloaded again."""
        return self.request(
            "POST", f"/api/v3/history/failed/{history_id}", destructive=True
        )

    def episode_search(self, episode_ids):
        return self.request(
            "POST",
            "/api/v3/command",
            json={"name": "EpisodeSearch", "episodeIds": list(episode_ids)},
        )

    def delete_episode_file(self, file_id):
        return self.request(
            "DELETE", f"/api/v3/episodefile/{file_id}", destructive=True
        )


class RadarrClient(Client):
    status_path = "/api/v3/system/status"

    def releases(self, movie_id):
        """Every release the indexers have for a film, with Radarr's verdict on
        each (this asks every indexer, so it can take a minute or two)."""
        return self.request(
            "GET",
            "/api/v3/release",
            params={"movieId": movie_id},
            read_timeout=300,
            attempts=1,
        )

    def ignore_for_movie(self, movie_id, label, pattern, name):
        """Make Radarr ignore releases matching `pattern` for this film only."""
        _ignore_releases(self, f"/api/v3/movie/{movie_id}", label, pattern, name)

    def movies(self):
        return self.request("GET", "/api/v3/movie")

    def movie(self, movie_id):
        return self.request("GET", f"/api/v3/movie/{movie_id}")

    def queue(self):
        return self.request("GET", "/api/v3/queue/details")

    def commands(self):
        return self.request("GET", "/api/v3/command")

    def diskspace(self):
        return self.request("GET", "/api/v3/diskspace")

    def grabs(self, movie_id):
        """The movie's download history, newest first, grabs only."""
        rows = self.request(
            "GET",
            "/api/v3/history/movie",
            params={"movieId": movie_id, "eventType": 1},
        )
        return sorted(rows or [], key=lambda r: r.get("date") or "", reverse=True)

    def mark_failed(self, history_id):
        """Blocklist the release behind a grab, so it isn't downloaded again."""
        return self.request(
            "POST", f"/api/v3/history/failed/{history_id}", destructive=True
        )

    def delete_movie_file(self, file_id):
        return self.request("DELETE", f"/api/v3/moviefile/{file_id}", destructive=True)

    def search_movie(self, movie_id):
        return self.request(
            "POST",
            "/api/v3/command",
            json={"name": "MoviesSearch", "movieIds": [movie_id]},
        )

    def rescan_movie(self, movie_id):
        return self.request(
            "POST", "/api/v3/command", json={"name": "RescanMovie", "movieId": movie_id}
        )

    def collections(self):
        return self.request("GET", "/api/v3/collection")

    def collection(self, collection_id):
        return self.request("GET", f"/api/v3/collection/{collection_id}")

    def update_collection(self, collection):
        return self.request(
            "PUT", f"/api/v3/collection/{collection['id']}", json=collection
        )

    def recommendations(self):
        """Films TMDB recommends from the ones in the library."""
        return self.request(
            "GET",
            "/api/v3/importlist/movie",
            params={
                "includeRecommendations": "true",
                "includeTrending": "false",
                "includePopular": "false",
            },
        )

    def lookup_tmdb(self, tmdb_id):
        return self.request(
            "GET", "/api/v3/movie/lookup/tmdb", params={"tmdbId": tmdb_id}
        )

    def add_movie(self, movie):
        return self.request("POST", "/api/v3/movie", json=movie)

    def delete_movie(self, movie_id, *, exclude=False):
        """Delete a film and its files; ``exclude`` keeps Radarr's import
        lists from adding it straight back."""
        return self.request(
            "DELETE",
            f"/api/v3/movie/{movie_id}",
            params={
                "deleteFiles": "true",
                "addImportExclusion": "true" if exclude else "false",
            },
            destructive=True,
        )

    def list_movies(self):
        """Films currently on Radarr's import lists (TMDB ids, cached by Radarr)."""
        return self.request(
            "GET",
            "/api/v3/importlist/movie",
            params={
                "includeRecommendations": "false",
                "includeTrending": "false",
                "includePopular": "false",
            },
        )

    def import_lists(self):
        return self.request("GET", "/api/v3/importlist")

    def quality_profile(self, profile_id):
        return self.request("GET", f"/api/v3/qualityprofile/{profile_id}")

    def set_monitored(self, movie_id, monitored):
        return self.request(
            "PUT",
            "/api/v3/movie/editor",
            json={"movieIds": [movie_id], "monitored": monitored},
        )

    def grab(self, release):
        """Download one release from a search, as Radarr's interactive search does."""
        return self.request(
            "POST",
            "/api/v3/release",
            json={"guid": release["guid"], "indexerId": release["indexerId"]},
        )


class SeerrClient(Client):
    """Seerr (or Jellyseerr/Overseerr): who asked for what. Read-only."""

    status_path = "/api/v1/status"

    def requests(self, limit=2000):
        """Every request, newest first, in pages of 100."""
        rows, skip = [], 0
        while skip < limit:
            page = self.request(
                "GET",
                "/api/v1/request",
                params={"take": 100, "skip": skip, "sort": "added", "filter": "all"},
            )
            results = (page or {}).get("results") or []
            rows += results
            if len(results) < 100:
                break
            skip += 100
        return rows


CLIENTS = {
    "jellyfin": JellyfinClient,
    "sonarr": SonarrClient,
    "radarr": RadarrClient,
    "seerr": SeerrClient,
}
