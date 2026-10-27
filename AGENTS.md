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
    auth.py            alexapy wrapper: login, challenges, refresh token, session, csrf
    client.py          GraphQL client: discovery, state, commands, HTTP error handling
    parser.py          Pure functions: GraphQL JSON -> models, error detection
    queries.py         The three GraphQL documents
    models.py          Dataclasses and enums (Temperature, ThermostatState, ...)
    exceptions.py      AlexaError hierarchy
  __init__.py          async_setup_entry / async_unload_entry
  config_flow.py       user, security_code, captcha, reauth_confirm steps + options flow
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

- **Setpoint values must be JSON numbers.** Strings like `"73"` get `BAD_REQUEST`. This is the
  bug in homebridge-alexa-smarthome issue #284 (fixed in its PR #285). `client.setpoint_payload`
  and `tests/test_client.py::test_set_target_sends_numeric_value` guard it. Never format a
  setpoint with `str()`.
- **The csrf header is required** and alexapy only sets it inside `AlexaAPI.__init__`, which we
  do not use. `AlexaAuth.csrf` reads it from the cookie jar.
- **Do not use `AlexaAPI._static_request`.** It returns `None` for most 4xx responses, which hides
  the exact errors this integration needs to surface.
- **alexapy hardcodes the `alexa_media.` cookie file prefix.** `auth.isolate_storage` rewrites it to
  `alexa_thermostat.` so we never overwrite Alexa Media Player's cookies for the same account.
- **The refresh token is bound to the device uuid.** Both are stored in the entry
  (`tokens`, `device_uuid`). Reauth reuses the stored uuid. Losing the uuid invalidates the token.
- **Mode OFF:** we send `setThermostatMode OFF` first and fall back to the `power` feature's
  `turnOff` only when Alexa rejects it (homebridge issue #260 reports OFF is unreliable).
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
- **Config flow sessions** are closed on failed logins, on discovery failure (abort) and in
  `async_remove` when the user abandons the dialog.

## Unverified against a real device

Confirm these on hardware before relying on them, and update this list when you do:

- `setThermostatMode` with `ECO` (the Homebridge plugin never sends ECO).
- `setThermostatMode` with `OFF` versus `turnOff` on the Amazon Smart Thermostat.
- The `Power` fragment inside the state query (the plugin only uses it in discovery).
- Login challenges other than OTP and captcha (claims picker, app approval) are reported as
  `unsupported_challenge`. A browser-proxy login like Alexa Media Player's is the planned fix.

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
  `exceptions` for `HomeAssistantError`/`ServiceValidationError` translation keys).
- Every behavior change needs a test. API changes start with a fixture in `tests/fixtures/` and a
  parser test, then the client test, then the entity test.
- Keep `api/` free of Home Assistant imports and alexapy confined to `api/auth.py`.

## Where to make common changes

| Change | Files |
|---|---|
| Amazon login breaks | `api/auth.py`, `tests/test_auth.py` |
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

## Getting into the default HACS catalog

Requirements already met by the repository: public, `hacs.json`, `manifest.json` with `domain`,
`documentation`, `issue_tracker`, `codeowners`, `name`, `version`; brand icon;
MIT license; HACS action and hassfest workflows. Still needed: repository description and topics
on GitHub, issues enabled, both workflows green, and at least one full GitHub release. Then open a
pull request to `hacs/default` adding `nikolmedo/HACS-AlexaThermostat` to the `integration` file.
