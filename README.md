<p align="center">
  <img src="assets/icon.png" alt="Alexa Thermostat" width="160">
</p>

<h1 align="center">Alexa Thermostat for Home Assistant</h1>

<p align="center">
  Control your <b>Amazon Smart Thermostat</b> from Home Assistant as a real climate entity.<br>
  No Homebridge, no bridges, no extra containers.
</p>

<p align="center">
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Default-41BDF5.svg" alt="HACS Default"></a>
  <a href="https://github.com/nikolmedo/AlexaThermostat/releases"><img src="https://img.shields.io/github/v/release/nikolmedo/AlexaThermostat?include_prereleases" alt="Release"></a>
  <a href="https://github.com/nikolmedo/AlexaThermostat/actions/workflows/validate.yml"><img src="https://github.com/nikolmedo/AlexaThermostat/actions/workflows/validate.yml/badge.svg" alt="Validate"></a>
  <a href="https://github.com/nikolmedo/AlexaThermostat/actions/workflows/tests.yml"><img src="https://github.com/nikolmedo/AlexaThermostat/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/nikolmedo/AlexaThermostat" alt="License"></a>
</p>

The Amazon Smart Thermostat only talks to Alexa. There is no local API, no Matter support and no
official Home Assistant integration, so until now the usual workaround was running Homebridge
with the `homebridge-alexa-smarthome` plugin and bridging it back into Home Assistant. That works
until it doesn't: when the plugin broke setpoint changes in 2026, there was nothing you could do
but wait.

This integration talks to the same Alexa web API directly from Home Assistant. You sign in once
with your Amazon account and your thermostat shows up as a native `climate` entity with the
standard thermostat card, automations, voice assistants and history.

## Features

- A native climate entity with the standard Home Assistant thermostat card.
- Heat, Cool, Heat/Cool (auto) and Off, plus an Eco preset.
- Target temperature in heat or cool mode, low and high setpoints in heat/cool mode.
- Current temperature and indoor humidity, as reported by the thermostat.
- Celsius and Fahrenheit. The integration uses the unit your thermostat reports and Home
  Assistant converts the rest.
- If Alexa rejects a command, Home Assistant tells you instead of
  silently snapping the value back.
- You sign in on Amazon's own page, so robot checks, captchas and codes just work. Your password
  is only used to fill in that page and is never stored. Only an Amazon refresh token is kept,
  like the Alexa app does.
- Multiple thermostats on the same account, each as its own device.
- A diagnostics download with secrets redacted, for painless bug reports.

## Requirements

- Home Assistant **2026.3** or newer.
- An Amazon Smart Thermostat (or another Alexa-connected thermostat) that you can control from
  the Alexa app.
- The Amazon account email and password linked to that Alexa app.
- For setup, a browser that can reach your Home Assistant (Amazon's sign-in page is passed
  through it).

## Installation

### HACS (recommended)

Alexa Thermostat is available in the **default HACS store**, so there is no custom repository to
add.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=nikolmedo&repository=AlexaThermostat&category=integration)

1. Click the button above, or open **HACS** in your Home Assistant sidebar and search for
   **Alexa Thermostat**.
2. Click **Download** and confirm.
3. Restart Home Assistant.

### Manual

1. Download the latest release.
2. Copy `custom_components/alexa_thermostat` into the `custom_components` folder of your Home
   Assistant configuration.
3. Restart Home Assistant.

## Setup

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=alexa_thermostat)

1. Go to **Settings > Devices & services > Add integration** and pick **Alexa Thermostat**.
2. Enter your Amazon email, password and region (for example `amazon.com` or `amazon.co.uk`).
   Adding the 2FA app key (see below) is strongly recommended. Check the **Home Assistant URL**:
   it must be the address this browser uses to open Home Assistant (it is prefilled from
   **Settings > System > Network**).
3. Amazon's sign-in page opens in a new browser tab with your details filled in. Finish signing in
   there, including any robot check, captcha or code Amazon asks for. The tab closes by itself
   when you are done. If no tab opens, click the link in the setup dialog.

That's it. Each thermostat on your account becomes a device with one climate entity.

The sign-in page is served through your Home Assistant, so open the integration from a browser
that can reach Home Assistant (the same one you are using for setup works). Allow pop-ups for
Home Assistant if the new tab does not open. Amazon lists the integration as "Alexa Media Player"
in your devices, because it uses the same sign-in library.

### Make sign-in painless with a 2FA app key (recommended)

Amazon often asks for a one-time code when a new device signs in. You can let Home Assistant
generate those codes for you:

1. On Amazon, open **Your Account > Login & security > 2-Step Verification**.
2. Add a new authenticator app and choose **Can't scan the barcode?**.
3. Copy the key Amazon shows (a long string of letters and digits, spaces are fine) and paste it
   in the **2FA app key** field during setup. Use the key, not a 6-digit code. Without a key,
   type the code on Amazon's page when it asks.
4. Finish adding the app on Amazon with the code your phone app (or Home Assistant) shows.

You only need this once. After the first sign-in the integration keeps the session alive with a
refresh token.

## Options

Open the integration and click **Configure** to change the **update interval** (default 60
seconds, minimum 30). Lower values react faster but make more requests to Amazon.

## How it works

```
Home Assistant ──▶ climate entity ──▶ coordinator ──▶ Alexa GraphQL API (alexa.amazon.com)
                                                         ▲
                     Amazon sign-in (refresh token) ─────┘
```

The integration uses the same unofficial endpoint the Alexa web app uses
(`/nexus/v1/graphql`) to list your thermostats, read their state and send commands. Amazon login
is handled by [alexapy](https://pypi.org/project/alexapy/), the library behind Alexa Media
Player: you sign in on Amazon's own pages, passed through Home Assistant, and the integration keeps
only the refresh token Amazon issues.

When you change something, the new value shows up right away. Alexa can take a few seconds to
report it back, so the integration keeps your value until Alexa catches up instead of flickering
back to the old one.

## Troubleshooting

**The Amazon tab does not open, or shows "Not Found".**
Allow pop-ups for Home Assistant. The tab only works while the setup dialog is open; if you closed
the dialog, start the setup again.

**The Amazon tab shows an error, a blank page, or never loads.**
Check the **Home Assistant URL** in the setup form: it must be the exact address in this browser's
address bar when you use Home Assistant (for example your local address at home, or your remote
address when you are away). Start the setup again with the right one.

**"Amazon did not confirm the sign-in."**
Finish the sign-in on Amazon's page with the same email you entered in Home Assistant, then try
again. If Amazon asks you to approve the sign-in in the Amazon app or by email, approve it and
continue on the page.

**"Could not reach Amazon."**
Home Assistant could not connect to Amazon. Check its internet connection and refresh the tab.

**The integration asks me to sign in again.**
Amazon revoked the session (password change, too many devices, or a security check). Click
**Reconfigure** on the notification, enter your password and finish signing in on Amazon's page.
Your thermostat keeps its entity and history.

**A temperature change is rejected.**
You'll see the error Alexa returned. In heat/cool mode, set the low and high temperatures instead
of a single target.

**Something else.**
Enable debug logging from the integration page, reproduce the problem, and
[open an issue](https://github.com/nikolmedo/AlexaThermostat/issues/new/choose) with the
logs and the diagnostics file.

## Known limitations

- This uses an **unofficial** Amazon API. Amazon can change it at any time, and sign-in pages
  change more often than the thermostat API itself.
- State is polled (every 60 seconds by default). Changes made in the Alexa app show up on the
  next poll.
- The thermostat does not report whether it is currently heating or cooling, so there is no
  `hvac_action`.
- Signing in needs a browser that can reach your Home Assistant, because Amazon's pages are
  passed through it.

## Contributing

Pull requests are welcome. Start with [AGENTS.md](AGENTS.md): it explains the architecture, the
exact API payloads, the known gotchas and how to run the tests, for humans and AI assistants
alike.

## Credits

The Alexa API details come from the excellent
[homebridge-alexa-smarthome](https://github.com/joeyhage/homebridge-alexa-smarthome) by
@joeyhage, including the setpoint fix from its open pull request #285. Sign-in is powered by
[alexapy](https://gitlab.com/keatontaylor/alexapy).

## Disclaimer

This project is not affiliated with, endorsed by, or connected to Amazon. Amazon, Alexa and all
related marks are trademarks of Amazon.com, Inc. or its affiliates. Use at your own risk.
