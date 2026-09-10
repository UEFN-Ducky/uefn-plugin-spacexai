# SpaceXAI

SpaceXAI (Grok) API plus the official Grok Build coding agent. Install from
Store → Gateways, then enable to show SpaceXAI under Providers & Keys and
Grok Build under Coding Agents.

Desktop plugin for [UEFN-Ducky](https://github.com/UEFN-Ducky/UEFN-Ducky) (`spacexai`).
Install or update from **Settings → Store** in the app — do not install from a zip by hand.

## Grok Build

Needs the official `grok` CLI:

```powershell
irm https://x.ai/cli/install.ps1 | iex
grok login
```

Or save a SpaceXAI API key in Ducky. Restart Ducky and click Detect.

Grok extra-args cannot skip Ducky's permission broker
(`--always-approve`, `--permission-mode`, and similar flags are rejected).

## Build

```bash
py scripts/build_zip.py
```

Writes `deploy/spacexai-1.2.0.ducky-plugin.zip` (scripts/ and deploy/ are not packed).

## Secrets

Never commit tokens or keys. The app stores `spacexai` locally (DPAPI), not in this package.

## License

MIT. Copyright (c) 2026 Mindful Path Company, LLC. See [LICENSE](LICENSE).
