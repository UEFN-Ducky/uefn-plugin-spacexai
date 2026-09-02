# SpaceXAI

SpaceXAI (Grok) API for Settings → LLMs. Install from Store → Gateways, then enable to show SpaceXAI under Providers & Keys.

Desktop plugin for [UEFN-Ducky](https://github.com/UEFN-Ducky/UEFN-Ducky) (`spacexai`).
Install or update from **Settings → Store** in the app — do not install from a zip by hand.

## Build

```bash
py scripts/build_zip.py
```

Writes `deploy/spacexai-1.0.1.ducky-plugin.zip` (scripts/ and deploy/ are not packed).

## Secrets

Never commit tokens or keys. The app stores `spacexai` locally (DPAPI), not in this package.

## License

MIT. Copyright (c) 2026 Mindful Path Company, LLC. See [LICENSE](LICENSE).
