# Worked-out service configs

`infra/homeassistant/` and `infra/searxng/` are gitignored: the containers fill
them with state, databases and secrets. But the configs themselves encode two
fixes that cost real time to find, so copies live here.

Copy them into place before first use, or after the containers generate their
defaults:

    cp infra/examples/homeassistant-configuration.yaml infra/homeassistant/configuration.yaml
    cp infra/examples/searxng-settings.yml             infra/searxng/settings.yml
    # then put a real random secret_key in the searxng one
    docker compose restart

## What they fix

**Home Assistant.** Template entities must be declared under the top-level
`template:` key. The older `light: - platform: template` form is gone from
current releases and fails with "Configuring the template integration under the
light platform key is not supported". It does not stop startup -- you just
silently get no lights, which is a confusing way to lose an hour. Scene entity
states must also be strings, including numeric ones.

**SearXNG.** JSON output is off by default and the server answers
403 Forbidden without it. The `SEARXNG_SEARCH_FORMATS` environment variable does
NOT enable it; only `search.formats` in settings.yml does.

The Home Assistant file also defines demo lights, a switch, a fan, a thermostat
and a scene with realistic friendly names, which is what `scripts/test_home.py`
exercises. On a real install, delete those blocks and let your Tuya / TP-Link
integrations supply the entities instead.
