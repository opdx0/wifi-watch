"""UniFi Network controller client. No Home Assistant imports beyond the
aiohttp ClientSession itself, which the caller creates and owns (see
__init__.py: a DEDICATED session with a DummyCookieJar and verify_ssl=False
- not HA's shared session).

Two UniFi credentials: the official integration API key is read-only and
cannot block/unblock a client or read its SSID (confirmed by probing it
directly - it only supports guest-portal authorize/unauthorize). Blocking,
unblocking, and reading SSID all require the legacy cookie-session
controller API instead, authenticated with a dedicated least-privilege
local account.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)


class UnifiApiError(Exception):
    """Base error for any UniFi API failure."""


class UnifiDegradedResponseError(UnifiApiError):
    """The controller answered with valid-looking JSON but a genuinely
    untrustworthy client list - a paginated/truncated response (count !=
    totalCount, seen for real during a controller's post-reboot recovery
    window, where every client's connectedAt had just changed) or a
    record missing its MAC entirely. Does NOT cover a missing ipAddress
    on an otherwise-valid record - that's routine on networks where a
    third-party DHCP server (not UniFi itself) hands out leases, and
    ipAddress is only ever used for notification/history display
    (coordinator.py falls back to "?"), never for detection logic."""


class UnifiAuthError(UnifiApiError):
    """Login, or a request retried once after a fresh login, still failed
    authentication. Distinct from UnifiApiError so the coordinator can raise
    ConfigEntryAuthFailed specifically for this case, not any transient
    network failure."""


def _raise_for_status(resp: aiohttp.ClientResponse) -> None:
    """resp.raise_for_status() alone raises aiohttp.ClientResponseError,
    which is neither UnifiAuthError nor UnifiApiError - it used to escape
    both except branches in the coordinator entirely (raw traceback,
    "Unexpected error fetching wifi_watch data", no reauth prompt). Route
    401/403 to UnifiAuthError (so ConfigEntryAuthFailed/reauth actually
    fires) and everything else to UnifiApiError, right at the boundary
    where aiohttp's exception is still in scope."""
    try:
        resp.raise_for_status()
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            raise UnifiAuthError(f"{err.status} {err.message}") from err
        raise UnifiApiError(f"{err.status} {err.message}") from err


class UnifiClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        api_key: str,
        username: str,
        password: str,
        site_id: str = "",
        site_name: str = "",
    ) -> None:
        self._session = session
        self._host = host
        self._site_id = site_id
        self._site_name = site_name
        self._api_key = api_key
        self._username = username
        self._password = password
        self._token: str | None = None
        self._csrf: str | None = None
        # Guards the login/refresh path specifically - a single event loop
        # doesn't mean a single in-flight request; without this, several
        # concurrent calls all seeing an expired session could each trigger
        # their own re-login (a stampede) instead of one refresh they share.
        self._session_lock = asyncio.Lock()

    async def get_sites(self) -> list[dict]:
        """Lists sites via the official integration API (api_key auth, not
        site-scoped) - used by the config flow to let the user pick a site
        from a dropdown instead of typing its UUID by hand."""
        url = f"https://{self._host}/proxy/network/integration/v1/sites?limit=200"
        try:
            async with self._session.get(
                url, headers={"X-API-KEY": self._api_key, "Accept": "application/json"}
            ) as resp:
                _raise_for_status(resp)
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UnifiApiError(f"error fetching sites: {err}") from err
        return data.get("data", [])

    async def get_legacy_site_names(self) -> dict[str, str]:
        """Maps site id -> the legacy API's own lowercase short name (e.g.
        "default"). Keyed by "external_id", not the legacy record's own
        "_id" - confirmed live: the official v1 API's site "id" field
        (what the rest of this integration calls site_id) matches the
        legacy record's "external_id", not its "_id". NOT the same string
        as the official API's "name" field either, which is actually a
        display name (e.g. "Default") - confirmed by a live 401 when the
        two were conflated: every legacy-API call (block/unblock, SSID/
        vendor lookup) paths on this short name, and it's case-sensitive."""
        data = await self._legacy_request("GET", "/proxy/network/api/self/sites")
        return {s["external_id"]: s["name"] for s in data.get("data", []) if s.get("external_id") and s.get("name")}

    async def get_wireless_clients(self) -> list[dict]:
        """Polls the official integration API (read-only, api_key auth) for
        the site's client list. Called every poll cycle - kept separate from
        the legacy-session path below, which is only hit on an actual new-
        client event, to avoid hammering the legacy login endpoint."""
        url = f"https://{self._host}/proxy/network/integration/v1/sites/{self._site_id}/clients?limit=200"
        try:
            async with self._session.get(
                url, headers={"X-API-KEY": self._api_key, "Accept": "application/json"}
            ) as resp:
                _raise_for_status(resp)
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UnifiApiError(f"error fetching clients: {err}") from err

        if "data" not in data or data.get("count") != data.get("totalCount"):
            raise UnifiDegradedResponseError(
                f"degraded/paginated client list response: count={data.get('count')} totalCount={data.get('totalCount')}"
            )

        clients = [c for c in data["data"] if c.get("type") == "WIRELESS"]
        for c in clients:
            if not c.get("macAddress"):
                raise UnifiDegradedResponseError(f"client record missing macAddress: {c!r}")
        return clients

    async def validate_legacy_login(self) -> None:
        """Confirms the dedicated account's username/password work, without
        needing a site yet - login itself is site-agnostic. Used by the
        config flow before a site has been chosen."""
        async with self._session_lock:
            await self._login()

    async def _login(self) -> None:
        """Logs in with the dedicated legacy account. Must be called with
        _session_lock held."""
        body = {"username": self._username, "password": self._password, "rememberMe": False}
        try:
            async with self._session.post(f"https://{self._host}/api/auth/login", json=body) as resp:
                _raise_for_status(resp)
                set_cookie = resp.headers.get("Set-Cookie", "")
                csrf = resp.headers.get("x-csrf-token")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UnifiApiError(f"error logging in: {err}") from err

        if "TOKEN=" not in set_cookie:
            raise UnifiAuthError("login response had no TOKEN cookie")
        token = set_cookie.split("TOKEN=")[1].split(";")[0]
        if not csrf:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            csrf = json.loads(base64.urlsafe_b64decode(payload))["csrfToken"]

        self._token = token
        self._csrf = csrf

    async def _legacy_request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        """Runs one legacy-API call, refreshing the session once on an
        actual 401/403 (not on every call - that's what tripped UniFi's own
        login-endpoint rate limit under a burst of approval actions)."""
        async with self._session_lock:
            if not self._token:
                await self._login()

        for attempt in range(2):
            token_before_request = self._token
            headers = {"X-Csrf-Token": self._csrf, "Cookie": f"TOKEN={self._token}"}
            try:
                async with self._session.request(
                    method, f"https://{self._host}{path}", json=json_body, headers=headers
                ) as resp:
                    if resp.status in (401, 403):
                        if attempt == 0:
                            # Only relogin if nobody else already refreshed
                            # the token out from under us while we waited for
                            # the lock - two concurrent 401s should share one
                            # relogin, not each trigger their own.
                            async with self._session_lock:
                                if self._token == token_before_request:
                                    await self._login()
                            continue
                        raise UnifiAuthError(f"{method} {path} still unauthorized after a fresh login")
                    _raise_for_status(resp)
                    data = await resp.json()
                    # UniFi OS rotates the CSRF token on responses, not just
                    # at login - re-read it every call, or a controller
                    # upgrade that starts enforcing rotation would break this
                    # silently.
                    new_csrf = resp.headers.get("x-csrf-token")
                    if new_csrf:
                        self._csrf = new_csrf
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise UnifiApiError(f"error on {method} {path}: {err}") from err

        raise UnifiAuthError(f"{method} {path} failed after retry")  # pragma: no cover - unreachable

    async def _legacy_cmd(self, cmd: str, mac: str) -> bool:
        result = await self._legacy_request(
            "POST",
            f"/proxy/network/api/s/{self._site_name}/cmd/stamgr",
            json_body={"cmd": cmd, "mac": mac},
        )
        ok = result.get("meta", {}).get("rc") == "ok"
        _LOGGER.info("legacy %s mac=%s result=%s", cmd, mac, result.get("meta"))
        return ok

    async def block_client(self, mac: str) -> bool:
        return await self._legacy_cmd("block-sta", mac)

    async def unblock_client(self, mac: str) -> bool:
        return await self._legacy_cmd("unblock-sta", mac)

    async def list_blocked(self) -> list[dict]:
        """All currently-blocked clients per UniFi itself - ground truth,
        not our own state, since an auto-block on detection with an
        unanswered/expired token would otherwise be invisible to us.

        Uses stat/alluser, not stat/sta - a hard-blocked client that can't
        even associate has no live session, so it silently disappears from
        stat/sta entirely. stat/alluser is the persistent per-client record
        and correctly reports blocked=true regardless of current connection
        state."""
        data = await self._legacy_request("GET", f"/proxy/network/api/s/{self._site_name}/stat/alluser?within=8760")
        return [c for c in data.get("data", []) if c.get("blocked")]

    async def get_client_essid_and_vendor(self, mac: str) -> tuple[str | None, str | None]:
        """Legacy-API-only lookup (essid isn't in the official v1 API at
        all; vendor is technically on the v1 client record too, but this
        one legacy call already covers both) - only called for an actual
        new-client event, not every poll cycle. "oui" is UniFi's own field
        name for the MAC-vendor lookup it already does for you.

        One retry after a short delay: this fires right as a client is
        first detected via the official API's client list, but that list
        and the legacy stat/sta live-session table aren't the same data
        source - a freshly-reconnecting device can be visible in one and
        not yet in the other for a second or two. Confirmed live: a MAC
        that came back null on the first pass was present with valid
        essid/oui moments later on manual recheck."""
        for attempt in range(2):
            try:
                data = await self._legacy_request("GET", f"/proxy/network/api/s/{self._site_name}/stat/sta")
            except UnifiApiError as err:
                _LOGGER.error("essid/vendor lookup failed mac=%s: %s", mac, err)
                return None, None
            for c in data.get("data", []):
                if c.get("mac", "").lower() == mac.lower():
                    return c.get("essid"), c.get("oui")
            if attempt == 0:
                await asyncio.sleep(2)
        return None, None
