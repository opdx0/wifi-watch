"""Config flow for wifi_watch."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client, selector

from .const import (
    CONF_UNIFI_API_KEY,
    CONF_UNIFI_HOST,
    CONF_UNIFI_PASSWORD,
    CONF_UNIFI_SITE_ID,
    CONF_UNIFI_SITE_NAME,
    CONF_UNIFI_USERNAME,
    DEFAULT_AUTO_BLOCK,
    DEFAULT_EXCLUDED_NOTIFY_TARGETS,
    DEFAULT_NOTIFY_DEBOUNCE_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_RETENTION_WINDOW_SECONDS,
    DEFAULT_TOKEN_EXPIRE_SECONDS,
    DEFAULT_UNIFI_SITE_NAME,
    DOMAIN,
    OPT_AUTO_BLOCK,
    OPT_EXCLUDED_NOTIFY_TARGETS,
    OPT_NOTIFY_DEBOUNCE_SECONDS,
    OPT_POLL_INTERVAL_SECONDS,
    OPT_RETENTION_WINDOW_SECONDS,
    OPT_TOKEN_EXPIRE_SECONDS,
)
from .unifi_api import UnifiApiError, UnifiClient

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_UNIFI_HOST): str,
        vol.Required(CONF_UNIFI_API_KEY): str,
        vol.Required(CONF_UNIFI_USERNAME): str,
        vol.Required(CONF_UNIFI_PASSWORD): str,
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_UNIFI_USERNAME): str,
        vol.Required(CONF_UNIFI_PASSWORD): str,
    }
)


def _client_for(hass: HomeAssistant, data: dict, *, site_id: str = "", site_name: str = "") -> UnifiClient:
    """Uses HA's shared cached session, not a dedicated one - no config
    entry exists yet during the config flow, so a dedicated session here
    would only get cleaned up on full HA shutdown, leaking one per form
    submission. Safe to share: UnifiClient never relies on the session's
    cookie jar for correctness (it always sets Cookie:/X-Csrf-Token:
    headers manually per request, never reads response.cookies), so the
    DummyCookieJar the long-lived integration session uses doesn't matter
    here."""
    session = aiohttp_client.async_get_clientsession(hass, verify_ssl=False)
    return UnifiClient(
        session=session,
        host=data[CONF_UNIFI_HOST],
        api_key=data[CONF_UNIFI_API_KEY],
        username=data[CONF_UNIFI_USERNAME],
        password=data[CONF_UNIFI_PASSWORD],
        site_id=site_id,
        site_name=site_name,
    )


async def _validate_credentials(hass: HomeAssistant, data: dict) -> tuple[list[dict], dict[str, str]]:
    """Raises UnifiApiError (or a subclass) if either credential fails.
    Returns the site list plus a site id -> legacy short name map -
    site-agnostic, so this runs before a site has been chosen."""
    unifi = _client_for(hass, data)
    sites = await unifi.get_sites()  # validates the read-only API key
    await unifi.validate_legacy_login()  # validates the legacy account can log in
    legacy_names = await unifi.get_legacy_site_names()
    return sites, legacy_names


async def _validate_site(hass: HomeAssistant, data: dict, site_id: str, site_name: str) -> None:
    """Confirms the API key actually has access to the chosen site - a
    scoped key or a multi-site controller could pass _validate_credentials
    but still not be able to read this particular site's clients."""
    unifi = _client_for(hass, data, site_id=site_id, site_name=site_name)
    await unifi.get_wireless_clients()


class WifiWatchConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        # Accumulates data across this flow's steps (host/api_key/username/
        # password from step "user", then site_id/site_name from step
        # "site" or auto-picked if there's only one site). Fine to keep on
        # self - one flow instance per in-progress config flow, not shared.
        self._data: dict[str, Any] = {}
        self._sites: list[dict] = []
        self._legacy_names: dict[str, str] = {}
        # Set by async_step_reconfigure - lets the shared _pick_site() tail
        # know whether to create a new entry or update/reload the existing
        # one being reconfigured.
        self._reconfigure = False

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                sites, legacy_names = await _validate_credentials(self.hass, user_input)
            except UnifiApiError as err:
                _LOGGER.error("validation failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self._data = dict(user_input)
                self._sites = sites
                self._legacy_names = legacy_names
                if len(sites) == 1:
                    site_id = sites[0].get("id", "")
                    return await self._pick_site(site_id, self._legacy_names.get(site_id, DEFAULT_UNIFI_SITE_NAME))
                if len(sites) > 1:
                    return await self.async_step_site()
                errors["base"] = "no_sites"

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        # Dropdown labels use the official API's display name (e.g.
        # "Default") - only the legacy short name (e.g. "default"), from
        # self._legacy_names, is ever used for the actual legacy API calls.
        options = {s.get("id", ""): s.get("name") or s.get("id", "") for s in self._sites}
        schema = vol.Schema({vol.Required(CONF_UNIFI_SITE_ID): vol.In(options)})

        if user_input is not None:
            site_id = user_input[CONF_UNIFI_SITE_ID]
            site_name = self._legacy_names.get(site_id, DEFAULT_UNIFI_SITE_NAME)
            return await self._pick_site(site_id, site_name, errors)

        return self.async_show_form(step_id="site", data_schema=schema, errors=errors)

    async def _pick_site(self, site_id: str, site_name: str, errors: dict[str, str] | None = None) -> FlowResult:
        errors = errors if errors is not None else {}
        await self.async_set_unique_id(f"{self._data[CONF_UNIFI_HOST]}_{site_id}")
        if self._reconfigure:
            # Allows the host/API key/account to change during reconfigure,
            # but not to silently repoint this entry at a different site -
            # that's a new controller/site, not an update to this one.
            self._abort_if_unique_id_mismatch(reason="wrong_account")
        else:
            self._abort_if_unique_id_configured()
        try:
            await _validate_site(self.hass, self._data, site_id, site_name)
        except UnifiApiError as err:
            _LOGGER.error("site validation failed: %s", err)
            errors["base"] = "cannot_connect"
            if len(self._sites) > 1:
                return await (self.async_step_reconfigure_site() if self._reconfigure else self.async_step_site())
            step_id = "reconfigure" if self._reconfigure else "user"
            return self.async_show_form(step_id=step_id, data_schema=STEP_USER_SCHEMA, errors=errors)

        self._data[CONF_UNIFI_SITE_ID] = site_id
        self._data[CONF_UNIFI_SITE_NAME] = site_name
        # Notify targets are discovered dynamically at send time (see
        # __init__.py's _notify_targets) - persistent_notification plus
        # every paired phone's mobile_app_* service, broadcast to all of
        # them by default. Nothing to ask for here.
        if self._reconfigure:
            return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data=self._data)
        return self.async_create_entry(title=f"Wi-Fi Watch ({self._data[CONF_UNIFI_HOST]})", data=self._data)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Lets an existing entry's connection settings (host/API key/
        dedicated account) be edited any time from Settings -> Devices &
        Services -> Wi-Fi Watch -> Reconfigure, not just after an auth
        failure (that's async_step_reauth_confirm, which only covers the
        password)."""
        self._reconfigure = True
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                sites, legacy_names = await _validate_credentials(self.hass, user_input)
            except UnifiApiError as err:
                _LOGGER.error("reconfigure validation failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self._data = dict(user_input)
                self._sites = sites
                self._legacy_names = legacy_names
                if len(sites) == 1:
                    site_id = sites[0].get("id", "")
                    return await self._pick_site(site_id, self._legacy_names.get(site_id, DEFAULT_UNIFI_SITE_NAME))
                if len(sites) > 1:
                    return await self.async_step_reconfigure_site()
                errors["base"] = "no_sites"

        reconfigure_entry = self._get_reconfigure_entry()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, reconfigure_entry.data),
            errors=errors,
        )

    async def async_step_reconfigure_site(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        options = {s.get("id", ""): s.get("name") or s.get("id", "") for s in self._sites}
        schema = vol.Schema({vol.Required(CONF_UNIFI_SITE_ID): vol.In(options)})

        if user_input is not None:
            site_id = user_input[CONF_UNIFI_SITE_ID]
            site_name = self._legacy_names.get(site_id, DEFAULT_UNIFI_SITE_NAME)
            return await self._pick_site(site_id, site_name, errors)

        return self.async_show_form(step_id="reconfigure_site", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            candidate = {**reauth_entry.data, **user_input}
            try:
                await _validate_credentials(self.hass, candidate)
                await _validate_site(self.hass, candidate, candidate[CONF_UNIFI_SITE_ID], candidate[CONF_UNIFI_SITE_NAME])
            except UnifiApiError as err:
                _LOGGER.error("reauth validation failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(reauth_entry, data_updates=user_input)

        return self.async_show_form(step_id="reauth_confirm", data_schema=REAUTH_SCHEMA, errors=errors)

    @staticmethod
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> "WifiWatchOptionsFlow":
        return WifiWatchOptionsFlow(entry)


class WifiWatchOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self._entry.options
        # Same discovery as __init__.py's _notify_targets, minus the
        # exclusion filter - this needs the full candidate list so a
        # previously-excluded target can be re-included. Labeled so the
        # picker shows something readable instead of raw service names
        # ("mobile_app_johns_iphone" -> "John's iPhone") - best-effort from
        # the string itself, not a device-registry lookup, so it's not
        # necessarily the phone's exact display name.
        all_targets = sorted(
            n
            for n in self.hass.services.async_services().get("notify", {})
            if n == "persistent_notification" or n.startswith("mobile_app_")
        )

        def _label(target: str) -> str:
            if target == "persistent_notification":
                return "Persistent notification (Home Assistant UI)"
            return target.removeprefix("mobile_app_").replace("_", " ").title()

        schema = vol.Schema(
            {
                vol.Optional(
                    OPT_EXCLUDED_NOTIFY_TARGETS,
                    default=opts.get(OPT_EXCLUDED_NOTIFY_TARGETS, DEFAULT_EXCLUDED_NOTIFY_TARGETS),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[selector.SelectOptionDict(value=t, label=_label(t)) for t in all_targets],
                        multiple=True,
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
                # NumberSelector/BooleanSelector, not plain int/bool - the
                # frontend only renders a field's data_description helper
                # text for selector-backed fields, not raw voluptuous types.
                vol.Required(
                    OPT_POLL_INTERVAL_SECONDS, default=opts.get(OPT_POLL_INTERVAL_SECONDS, DEFAULT_POLL_INTERVAL_SECONDS)
                ): selector.NumberSelector(selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, min=1)),
                vol.Required(
                    OPT_NOTIFY_DEBOUNCE_SECONDS, default=opts.get(OPT_NOTIFY_DEBOUNCE_SECONDS, DEFAULT_NOTIFY_DEBOUNCE_SECONDS)
                ): selector.NumberSelector(selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, min=0)),
                vol.Required(
                    OPT_RETENTION_WINDOW_SECONDS, default=opts.get(OPT_RETENTION_WINDOW_SECONDS, DEFAULT_RETENTION_WINDOW_SECONDS)
                ): selector.NumberSelector(selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, min=1)),
                vol.Required(
                    OPT_TOKEN_EXPIRE_SECONDS, default=opts.get(OPT_TOKEN_EXPIRE_SECONDS, DEFAULT_TOKEN_EXPIRE_SECONDS)
                ): selector.NumberSelector(selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, min=1)),
                vol.Required(OPT_AUTO_BLOCK, default=opts.get(OPT_AUTO_BLOCK, DEFAULT_AUTO_BLOCK)): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
