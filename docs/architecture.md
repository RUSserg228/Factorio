# Factorio GPT Assistant Integration Plan

## Overview
This document captures the overall integration approach for the Factorio GPT assistant
mod and the companion desktop utility. The goal is to embed a GPT-powered assistant
inside Factorio (with Space Exploration support) while relying on a lightweight local
service that handles all communication with the OpenAI API. Multiplayer, Russian UI,
and strict data-handling constraints are first-class concerns.

## Components

### 1. Factorio Lua mod
The mod is a "thin client" that:

* Adds GUI entry points (global chat button, selection tool button, chat window).
* Collects data from the player-selected area, including entities, inventories,
  fluids, logic network signals, and power statistics.
* Marshals payloads and forwards them to the local utility over HTTP/WebSocket.
* Receives assistant replies and blueprint strings, renders chat messages, and
  materialises ghosts for blueprint placement in a single tick.
* Manages localised Russian strings for the entire interface.
* Stores chat transcripts and the five most recent area snapshots on the host.

### 2. Companion utility (this repository)
A standalone Python application launched by the server host. Its responsibilities are:

* Provide first-run onboarding: licence/consent notice, API key input, model profile
  selection, and connection diagnostics.
* Persist secrets and configuration in the host's user directory.
* Expose an HTTP API for the Factorio mod (`/status`, `/chat`, `/blueprint`,
  `/config`), proxying traffic to OpenAI's REST endpoints.
* Track rate-limit headers returned by OpenAI (requests remaining, reset time) so the
  mod can display "messages remaining" inside Factorio.
* Optionally enable TLS if the host deploys the utility on another machine.

### 3. OpenAI (or compatible) API
The external model provider. The utility supports multiple profiles (e.g. `gpt-4o`,
`gpt-4.1`, `gpt-4.1-mini`) and stores per-profile temperature, max tokens, and prompt
additions. Profiles can be extended to include alternative providers so long as their
REST API is OpenAI-compatible.

## Multiplayer topology

* The host runs the companion utility and Factorio server.
* Each client mod instance queries the host using deterministic addresses announced via
  mod settings (`host_service_host`, `host_service_port`). Only the host needs to enter
  API credentials.
* Data captured by clients (selection snapshots) is relayed through the host so that
  no player requires direct internet access or API keys.

## Data flow

1. **Selection** – Player activates the "GPT Анализатор" selection tool. The mod
   collects entities, recipes, combinator signals, logistic network states, inventory
   contents, and power usage. It calculates a load score and warns the player if the
   snapshot exceeds thresholds.
2. **Consent check** – On first use, the mod shows a concise consent dialog describing
   the outgoing data categories. Functions remain locked until the user accepts.
3. **Payload** – The mod packages the snapshot together with the player's prompt and
   metadata (mode: logistics analysis, blueprint generation, etc.) and sends it to the
   utility.
4. **Utility processing** – The utility injects the system prompt, forwards the
   request to the configured model, and captures response headers for rate-limit data.
5. **Response** – The utility returns:
   * assistant reply text (Russian by default),
   * optional blueprint string & placement metadata,
   * rate-limit summary (remaining requests, reset timestamp).
6. **Rendering** – The mod displays the chat response, updates the message limit badge,
   and, if required, spawns blueprint ghosts in a single tick.

## Modes of operation
The assistant supports eight specialised modes in addition to free-form chat:

1. **Logistic network analysis** – summarise storage, requests, production deficits.
2. **Power diagnostics** – track generation vs consumption, accumulators, shortages.
3. **Production bottleneck finder** – highlight under-supplied intermediate products.
4. **Defence planner** – estimate required turrets/ammunition for highlighted enemies.
5. **Train schedule generator** – propose stop sequences, wait conditions, and signals.
6. **Logic network inspector** – decode circuit/combination signal flows.
7. **Starter base generator** – craft early-game factory blueprints tuned for enabled
   mods (Space Exploration aware).
8. **Pollution/attack forecast** – predict enemy wave sizes and pollution spread.

Each mode influences the system prompt and post-processing rules inside the utility.

## Selection cache and auto-cleanup

* The mod persists up to five recent area snapshots for quick re-analysis. Entries are
  stored FIFO; once the limit is reached, older ones are discarded.
* Additionally, snapshots exceeding the configurable load score are skipped and the
  mod prompts the player to narrow the selection. Auto-cleanup purges stale snapshots
  after configurable idle time (default 10 minutes).

## Consent workflow

1. First launch shows a modal dialog: summary of collected data, statement about
   external transmission, confirmation question "Продолжить?".
2. Acceptance is stored in the player's save data; revoking consent via settings clears
   stored selections, chats, and disables network calls until re-accepted.

## Utility onboarding

* On first run, the utility prints a short consent notice mirroring the in-game text.
* It prompts for the API key (masked input via `getpass`) and optional organisation ID.
* The utility validates the key by requesting `GET /v1/models`. Success stores the key
  encrypted with a local key derived from the OS keyring when available (fallback to
  XOR obfuscation for portability).
* A summary table lists the available model profiles and their rate limits.

## Rate-limit tracking

* The utility records `x-ratelimit-*` headers from OpenAI responses and keeps a rolling
  window with the remaining requests, tokens, and reset timestamps.
* `/status` exposes the aggregated limit summary. The mod displays it in the chat UI as
  "Осталось X сообщений (сброс в HH:MM)".

## Deployment checklist

1. Install Python 3.10+ and dependencies (`requests`, `cryptography`, `uvicorn` if
   using ASGI mode).
2. Run `python utility/gpt_assistant_service.py --setup` to enter the API key and model
   defaults.
3. Launch the utility: `python utility/gpt_assistant_service.py`.
4. Start Factorio with the mod installed. The mod auto-discovers the host at the
   configured address.

Future revisions may ship platform-specific launchers (Windows `.exe`, macOS app,
Linux AppImage) built from the same Python source via PyInstaller.
