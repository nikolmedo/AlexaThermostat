# AGENTS.md

Guide for AI assistants and contributors. Read this before exploring the repository: it should
answer where things live, how the Alexa API behaves, and how to verify a change, without reading
every file.

## What this is

A Home Assistant custom integration (domain `alexa_thermostat`) that exposes thermostats linked
to an Alexa account, mainly the Amazon Smart Thermostat, as `climate` entities. It talks to the
unofficial Alexa web API over GraphQL. Distributed through HACS / the Home Assistant Marketplace.

## Repository map

```
custom_components/alexa_thermostat/
  api/                 Pure Python. NO Home Assistant imports (see Layering rules).
    auth.py            alexapy wrapper: sign-in proxy, refresh token, session, csrf
    client.py          GraphQL client: discovery, state, commands, HTTP error handling
    parser.py          Pure functions: GraphQL JSON -> models, error detection
    queries.py         The three GraphQL documents
    models.py          Dataclasses and enums (Temperature, ThermostatState, ...)
    exceptions.py      AlexaError hierarchy
  __init__.py          async_setup_entry / async_unload_entry
  config_flow.py       user / reauth_confirm -> sign_in (external step) -> finish + options flow
  proxy_view.py        Unauthenticated proxy and callback views for the browser sign-in
  coordinator.py       DataUpdateCoordinator + optimistic overlay (apply_optimistic)
  climate.py           The climate entity (HA <-> Alexa mode/setpoint mapping)
  diagnostics.py       Redacted diagnostics
  const.py             Config keys, defaults, Amazon domains
  manifest.json        Keep "domain", "name" first, then alphabetical (hassfest enforces it)
  translations/en.json All UI strings. There is no strings.json on purpose (Marketplace rule).
  brand/icon.png       256px icon, icon@2x.png 512px. Generated, see scripts/generate_icon.py
tests/                 pytest + pytest-homeassistant-custom-component
  fixtures/*.json      Real-shaped GraphQL responses used by every test layer
scripts/generate_icon.py  Regenerates brand/ and assets/ PNGs with Pillow
.github/workflows/     validate.yml (HACS action + hassfest), tests.yml (ruff + pytest)
```

## Layering rules

1. `api/` must never import `homeassistant`. It is written so it can be extracted to a PyPI
   library, which Home Assistant core requires before accepting an integration.
2. Only `api/auth.py` imports `alexapy`. Amazon login breaks more often than anything else;
   keeping it in one file means a fix or a library swap touches nothing else.
3. Home Assistant code depends on `api/` through its public names in `api/__init__.py`.
4. Entities never call the network directly except through `coordinator.client` commands, and
   they report results through `coordinator.apply_optimistic(...)`.

## Data flow

```
sign-in: config flow form -> proxy_view.async_start_sign_in -> external step opens
         /auth/alexa_thermostat/proxy/<flow_id> -> AlexaSignInProxy forwards to Amazon
         -> Amazon lands on /ap/maplanding -> redirect to /auth/alexa_thermostat/callback
         -> flow resumes -> AlexaAuth.async_finish_proxy_login() (code -> refresh token)
setup:   AlexaAuth.async_resume() (refresh token) -> client.async_get_thermostats()
         -> coordinator first refresh -> one AlexaThermostatClimate per device
poll:    coordinator._async_update_data -> client.async_get_state(endpoint_id) per device
         (a failing device is left out and goes unavailable; only all-failed is UpdateFailed)
command: climate.async_set_* -> client.async_set_* -> raise_for_command_errors
         -> coordinator.apply_optimistic(endpoint_id, field=value)
```

`apply_optimistic` stores the confirmed change for `COMMAND_SETTLE_DELAY` (15 s). Polls inside
that window keep the new value until Alexa reports it, because Alexa's state API lags a few
seconds behind accepted commands.

## Alexa API contract

All calls are `POST https://alexa.<amazon_domain>/nexus/v1/graphql` with a JSON body
`{"query": ..., "variables": {...}}`.

Headers sent by `client.py`: `Accept` and `Content-Type: application/json; charset=utf-8`,
`Origin: https://alexa.<domain>`, `Referer: https://alexa.<domain>/spa/index.html`,
`csrf: <value of the csrf cookie>`, and alexapy's `User-Agent`. Cookies travel in the alexapy
session cookie jar.

### Discovery (`ENDPOINTS_QUERY`)

Response path `data.endpoints.items[]`. A thermostat is an item where
`displayCategories.primary.value == "THERMOSTAT"`, `enablement` is `ENABLED` (or missing), and
some `features[].operations[].name == "setTargetSetpoint"`. Use `id`
(`amzn1.alexa.endpoint.<uuid>`) as `endpointId`.

### State (`THERMOSTAT_STATE_QUERY`, variables `{"endpointId": ...}`)

Response path `data.endpoint.features[]`, each with `name` and `properties[]`:

| Feature / property | Field | Model field |
|---|---|---|
| feature `temperatureSensor` | `value: {value, scale}` | `current_temperature` |
| property `targetSetpoint` | `value: {value, scale}` | `target_setpoint` |
| property `lowerSetpoint` / `upperSetpoint` | `value: {value, scale}` | `lower_setpoint` / `upper_setpoint` |
| property `thermostatMode` | `thermostatModeValue` (`HEAT`, `COOL`, `AUTO`, `ECO`, `OFF`) | `mode` |
| feature `power` | `powerStateValue` (`ON`/`OFF`) | `power_on` |
| feature `range` with `configuration.friendlyName.value.text == "Indoor humidity"` | `rangeValue.value` | `humidity` |

`value` can arrive as a string; the parser converts it. An empty or null `features` means
"State not available" and raises `AlexaApiError`.

### Commands (`SET_ENDPOINT_FEATURES_MUTATION`)

Variables: `{"featureControlRequests": [request]}`. Exact requests:

```json
{"endpointId": "amzn1.alexa.endpoint.<id>", "featureName": "thermostat",
 "featureOperationName": "setTargetSetpoint",
 "payload": {"targetSetpoint": {"value": 73, "scale": "FAHRENHEIT"}}}

{"endpointId": "amzn1.alexa.endpoint.<id>", "featureName": "thermostat",
 "featureOperationName": "setTargetSetpoint",
 "payload": {"lowerSetpoint": {"value": 20.5, "scale": "CELSIUS"},
             "upperSetpoint": {"value": 24, "scale": "CELSIUS"}}}

{"endpointId": "amzn1.alexa.endpoint.<id>", "featureName": "thermostat",
 "featureOperationName": "setThermostatMode", "payload": {"thermostatMode": "HEAT"}}

{"endpointId": "amzn1.alexa.endpoint.<id>", "featureName": "power",
 "featureOperationName": "turnOff"}
```

Success means `data.setEndpointFeatures.errors` is empty. Alexa answers **HTTP 200 even when it
rejects a command** (for example `{"errors": [{"code": "BAD_REQUEST"}]}`), so
`raise_for_command_errors` must run on every command response.

## Gotchas (learned the hard way)

- **Setpoint values must be JSON numbers.** Strings like `"73"` get `BAD_REQUEST`.
  `client.setpoint_payload` and `tests/test_client.py::test_set_target_sends_numeric_value`
  guard it. Never format a setpoint with `str()`.
- **The csrf header is required** and alexapy only sets it inside `AlexaAPI.__init__`, which we
  do not use. `AlexaAuth.csrf` reads it from the cookie jar.
- **Do not use `AlexaAPI._static_request`.** It returns `None` for most 4xx responses, which hides
  the exact errors this integration needs to surface.
- **alexapy hardcodes the `alexa_media.` cookie file prefix.** `auth.isolate_storage` rewrites it to
  `alexa_thermostat.` so we never overwrite Alexa Media Player's cookies for the same account.
- **The refresh token is bound to the device uuid.** Both are stored in the entry
  (`tokens`, `device_uuid`). Reauth reuses the stored uuid. Losing the uuid invalidates the token.
- **Mode OFF:** we send `setThermostatMode OFF` first and fall back to the `power` feature's
  `turnOff` only when Alexa rejects it (OFF through the mode is unreliable on some thermostats).
- **AUTO is HA's `heat_cool`**, with low/high setpoints. A single `temperature` in AUTO raises
  `ServiceValidationError` (`single_setpoint_in_auto`). ECO is a preset, not an HVAC mode.
- **Rounding:** Fahrenheit setpoints are rounded to whole degrees, Celsius to 0.5.
- **Validate before sending.** `async_set_temperature` checks the arguments (including an
  `hvac_mode` change) before any command, so a rejected call never half-applies.
- **ECO with no known previous mode** (first poll is ECO): leaving the preset raises
  `unknown_previous_mode`; `turn_on` falls back to AUTO (heat/cool).
- **Amazon region is validated** against `AMAZON_DOMAIN_PATTERN` in `config_flow.py` because it
  becomes the login and API host.
- **Humidity** is a generic `range` feature found by a localized friendly name; the parser matches
  keywords (`HUMIDITY_KEYWORDS`). Add a keyword when a region's name is missed.
- **hassfest rules that bit us:** entries under `exceptions` in `translations/en.json` must be
  objects (`{"message": "..."}`), not strings. Do not add `categories` to `manifest.json`: the
  hassfest action rejects it as an unknown key (HACS only warns when it is missing).
- **Sign-in only works through the browser proxy.** alexapy's headless form login broke with
  Amazon's page changes (October 2026: `/ax/claim`) and with Amazon's robot check
  (`/errors_page/validateCaptcha`), which alexapy 1.33/1.34 does not recognize. The proxy
  (alexapy's `AlexaProxy` on top of `authcaptureproxy`) shows Amazon's real pages to the user,
  who solves whatever Amazon asks. Do not bring back a headless login.
- **Proxy views are unauthenticated** (a plain browser tab has no Home Assistant token). They only
  answer for flow ids registered by `async_start_sign_in` that are still in-progress flows of this
  domain, and the callback only resumes a flow after Amazon confirmed the sign-in. Success always
  redirects to our own callback; `AlexaSignInProxy` ignores the `callback_url` query parameter
  alexapy would follow, and only a landing with Amazon's code counts as signed in. The callback
  resumes a flow once. Views are registered once per run (routes cannot be removed).
- **The proxy pins the browser's origin.** authcaptureproxy forwards any URL it cannot map to
  Amazon as is, so a request whose Host header differs from the first one (for example another
  port) would make Home Assistant fetch that server. `_handle` answers 404 instead. A request that
  built the proxy while the sign-in was stopped closes it and answers 404.
- **Never add `authcaptureproxy` to the manifest `loggers`.** At debug level it logs posted form
  fields (the Amazon password) and session cookies, and "Enable debug logging" plus a public
  issue would leak them.
- **The proxy is built on the first browser request**, from that request's origin, because
  authcaptureproxy rewrites Amazon's links with the base URL it was built with.
- **The external step URL must be absolute.** The frontend opens it with `window.open` only after
  `sanitizeHttpUrl` (`new URL(url)`, http/https only), so a relative path silently opens nothing.
  The form asks for the Home Assistant URL (prefilled from `get_url`, origin only), and the
  `sign_in` description repeats the URL as a link in case the browser blocks the pop-up.
- **The 2FA code is refreshed on every proxied request** (`AlexaSignInProxy.async_handle`);
  alexapy's own proxy computes it once, and codes expire every 30 seconds.
- **Amazon shows the device as "Alexa Media Player".** alexapy hardcodes that name in its
  `/auth/register` payload. Cosmetic; changing it would mean patching alexapy.
- **Never read another integration's config entries or credentials** (for example Alexa Media
  Player's `oauth` tokens), not even read-only. The HACS review (hacs/default PR #12832) rejected
  that shortcut. `tests/test_config_flow.py::test_alexa_media_player_entries_are_never_read`
  guards it.
- **Config flow sessions** are closed after a failed sign-in, on discovery failure (abort) and in
  `async_remove` (which also stops the proxy) when the user abandons the dialog.

## Unverified against a real device

Confirm these on hardware before relying on them, and update this list when you do:

- `setThermostatMode` with `ECO`.
- `setThermostatMode` with `OFF` versus `turnOff` on the Amazon Smart Thermostat.
- The `Power` fragment inside the state query (the plugin only uses it in discovery).
- The browser sign-in with Home Assistant behind a reverse proxy or Nabu Casa remote access. It was
  tested end to end only on amazon.com with a local Home Assistant URL (v0.4.0-beta.3).

## Commands

The Home Assistant test harness does not run on native Windows (`fcntl`). Use Linux, macOS, WSL
or CI. Python 3.14 is required by the pinned Home Assistant version.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
python -m pytest tests -q          # full suite, about 3 seconds
ruff check . && ruff format --check .
python scripts/generate_icon.py    # only when changing the icon (needs Pillow)
```

CI runs the same steps plus the HACS action and hassfest on every push and pull request.
Workflow actions are pinned to commit SHAs; Dependabot (`.github/dependabot.yml`) bumps them.

## Conventions

- Everything in the repository is in English: code, comments, docs, UI strings, commits.
- Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`), no AI attribution lines.
- `main` is protected: work on a branch and merge through a pull request.
- New user-facing text goes in `translations/en.json` (config steps, errors, aborts, and
  `exceptions` for `HomeAssistantError`/`ServiceValidationError` translation keys). hassfest
  rejects URLs in those strings: pass them as `description_placeholders` instead
  (`tests/test_translations.py::test_strings_contain_no_urls` guards it).
- Every behavior change needs a test. API changes start with a fixture in `tests/fixtures/` and a
  parser test, then the client test, then the entity test.
- Keep `api/` free of Home Assistant imports and alexapy confined to `api/auth.py`.

## Where to make common changes

| Change | Files |
|---|---|
| Amazon login breaks | `api/auth.py`, `proxy_view.py`, `tests/test_auth.py`, `tests/test_proxy_view.py` |
| Alexa renames a field or feature | `api/queries.py`, `api/parser.py`, fixtures, `tests/test_parser.py` |
| New command (for example fan mode) | `api/models.py` (Operation), `api/client.py`, `climate.py`, tests |
| New entity (for example humidity sensor) | new platform file, add it to `PLATFORMS` in `__init__.py`, translations, tests |
| Polling or optimistic behavior | `coordinator.py`, `const.py`, `tests/test_climate.py` |
| New config option | `config_flow.py` (OptionsFlowHandler), `const.py`, `translations/en.json` |

## Releasing

1. Bump `version` in `manifest.json` (semver).
2. Merge to `main` through a pull request with green checks.
3. Create a GitHub **release** (not just a tag) named `vX.Y.Z`. HACS reads releases.
4. If a release needs a newer Home Assistant, raise `homeassistant` in `hacs.json`.

To let someone test a branch first, publish a **pre-release** from its pushed commit, for example
`gh release create v0.4.0-beta.1 --prerelease --target <full commit SHA>` (a short SHA is
rejected). It appears in HACS under Redownload > Show beta versions and never replaces the
latest stable release.

## Default HACS catalog

The integration is in the default HACS catalog (hacs/default PR #12832, merged), so the README
tells users to search for it in HACS instead of adding a custom repository. Keep it that way.

Requirements met by the repository: public, `hacs.json`, `manifest.json` with `domain`,
`documentation`, `issue_tracker`, `codeowners`, `name`, `version`; brand icon; MIT license;
HACS action and hassfest workflows; description, topics and issues on GitHub; green workflows and
full GitHub releases. Submitted as hacs/default PR #12832, which adds `nikolmedo/AlexaThermostat`
to the `integration` file (alphabetical, case-insensitive). The repository was renamed from
`HACS-AlexaThermostat` because HACS rejects repository names that contain "HACS". The first review
requested removing the reuse of Alexa Media Player's stored session (done in v0.3.0); v0.4.0
replaced the blocked password login with the browser sign-in.
