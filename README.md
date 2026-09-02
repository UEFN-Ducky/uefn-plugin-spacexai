> **No official token.** The [UEFN-Ducky Contributors graph](https://github.com/UEFN-Ducky/UEFN-Ducky/graphs/contributors) is a commit list, not founders.
> **AnasInno** is not a founder and is not authorized to claim pump.fun / bump.fun fees.
> See [UNOFFICIAL_TOKENS.md](https://github.com/UEFN-Ducky/UEFN-Ducky/blob/main/UNOFFICIAL_TOKENS.md).

# SpaceXAI + Grok Build

SpaceXAI's Grok API provider and the official Grok Build coding agent for UEFN-Ducky.

Desktop plugin for [UEFN-Ducky](https://github.com/UEFN-Ducky/UEFN-Ducky) (`spacexai`).
Install or update from **Settings → Store** in the app — do not install from a zip by hand.

## Grok Build setup

Install the official xAI CLI in Windows PowerShell:

```powershell
irm https://x.ai/cli/install.ps1 | iex
grok login
```

Restart Ducky, then open **Settings → LLMs → Coding Agents** and click **Detect**.
Grok Build can authenticate with that CLI login or with the `spacexai` API key saved under
**Providers & Keys**.

The adapter runs `grok agent stdio` over ACP v1. Ducky passes its UEFN MCP bridge directly to
each Grok session, streams text/thinking/tool activity back to chat, resumes Grok sessions, and
asks for tool permissions in Ducky. It does not edit `.mcp.json` or Grok's user config and it
never places prompts or API keys on the command line.

## Build

```bash
py scripts/build_zip.py
```

Writes `deploy/spacexai-1.1.0.ducky-plugin.zip` (scripts/ and deploy/ are not packed).

## Test

```bash
python -m pytest backend scripts
```

## Secrets

Never commit tokens or keys. The app stores `spacexai` locally (DPAPI), not in this package.
The key is injected only into the Grok child-process environment as `XAI_API_KEY`.
The official Grok CLI may maintain its own login or API-key state under `~/.grok`; this plugin
does not write that state itself.

## License

MIT. Copyright (c) 2026 Mindful Path Company, LLC. See [LICENSE](LICENSE).
