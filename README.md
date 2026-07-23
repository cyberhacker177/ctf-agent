# CTF Agent

Autonomous CTF (Capture The Flag) solver that races multiple AI models against challenges in parallel. Built in a weekend, we used it to solve all 52/52 challenges and win **1st place at BSidesSF 2026 CTF**.

Built by [Veria Labs](https://verialabs.com), founded by members of [.;,;.](https://ctftime.org/team/222911) (smiley), the [#1 US CTF team on CTFTime in 2024 and 2025](https://ctftime.org/stats/2024/US). We build AI agents that find and exploit real security vulnerabilities for large enterprises.

## Results

| Competition | Challenges Solved | Result |
|-------------|:-:|--------|
| **BSidesSF 2026** | 52/52 (100%) | **1st place ($1,500)** |

The agent solves challenges across all categories — pwn, rev, crypto, forensics, web, and misc.

## How It Works

A **coordinator** LLM manages the competition while **solver swarms** attack individual challenges. Each swarm runs multiple models simultaneously — the first to find the flag wins.

```
                        +-----------------+
                        | PlatformClient  |
                        |  CTFd or HTB    |
                        +--------+--------+
                                 |
                        +--------v--------+
                        |  Poller (5s)    |
                        +--------+--------+
                                 |
                        +--------v--------+
                        | Coordinator LLM |
                        | (Claude/Codex)  |
                        +--------+--------+
                                 |
              +------------------+------------------+
              |                  |                  |
     +--------v--------+ +------v---------+ +------v---------+
     | Swarm:          | | Swarm:         | | Swarm:         |
     | challenge-1     | | challenge-2    | | challenge-N    |
     |                 | |                | |                |
     |  GPT-5.5        | |  GPT-5.5       | |                |
     |  GPT-5.4        | |  GPT-5.4       | |                |
     |  GPT-5.4-mini   | |  GPT-5.4-mini  | |                |
     |  GPT-5.6-terra  | |  GPT-5.6-terra | |     ...        |
     +--------+--------+ +--------+-------+ +----------------+
              |                    |
     +--------v--------+  +-------v--------+
     | Docker Sandbox  |  | Docker Sandbox |
     | (isolated)      |  | (isolated)     |
     |                 |  |                |
     | pwntools, r2,   |  | pwntools, r2,  |
     | gdb, python...  |  | gdb, python... |
     +-----------------+  +----------------+
```

Each solver runs in an isolated Docker container with CTF tools pre-installed. Solvers never give up — they keep trying different approaches until the flag is found.

## Quick Start

```bash
# Install
uv sync

# Build sandbox image
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .

# Configure credentials
cp .env.example .env
# Edit .env with your API keys and platform credentials

# Run against a CTFd instance
uv run ctf-solve \
  --platform ctfd \
  --ctfd-url https://ctf.example.com \
  --ctfd-token ctfd_your_token \
  --challenges-dir challenges \
  --max-challenges 10 \
  -v
```

### Hack The Box CTF events

HTB events use the same solver and `metadata.yml` format. Set an event ID such as
1434 and provide an authenticated token or session cookie. HTB support covers
challenge discovery, solved status, downloads, flags, and challenge instances when
the event exposes them. A container-backed challenge is only handed to solvers after
HTB publishes its usable host/port/URL; that endpoint is written to `metadata.yml`
and included in every solver prompt.

```bash
uv run ctf-solve --platform htb --htb-event-id 1434 --htb-token "$HTB_TOKEN" --htb-mode auto --challenges-dir challenges
```

Authentication methods, in recommended order:

- `HTB_TOKEN` / `--htb-token`: bearer token from an authenticated HTB CTF session.
- `HTB_COOKIE` / `--htb-cookie`: a complete authenticated cookie header, useful when the browser flow is protected by CAPTCHA or Cloudflare.
- `HTB_USER` + `HTB_PASS`: experimental login fallback only; it can require an HTB CAPTCHA token and may change without notice.

Never commit credentials, cookies, or tokens. Equivalent settings are available as
`PLATFORM`, `HTB_EVENT_ID`, `HTB_TOKEN`, `HTB_COOKIE`, `HTB_USER`, `HTB_PASS`, and
`HTB_MODE` in `.env`.

| Mode | Behavior |
|---|---|
| `auto` | Prefer official HTB MCP; fall back to the experimental HTTP API when MCP is unavailable. |
| `mcp` | Require MCP; no HTTP fallback. |
| `http` | Use the experimental authenticated HTB web API directly. |

The HTTP API is configurable (`HTB_API_URL`, endpoint-path settings) because HTB
does not publish it as a stable integration contract.

### Platform architecture

`PlatformClient` is the small shared interface used by the poller, coordinator, and
solvers. It provides challenge discovery, pulling/downloading, solved-state checks,
flag submission, and optional instance lifecycle operations. `CTFdClient` preserves
the existing CTFd behavior; `HTBClient` implements the same operations through MCP
or the HTTP fallback. Platform-specific client construction remains at the CLI edge.

The default solver lineup is `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`,
`gpt-5.6-luna`, and `gpt-5.6-terra` through Codex. Use `--models` to override it.

### Excluding challenges from AI

Use `challenge-policy.yml` to prevent the AI from working on selected unsolved
challenges. Team-solved challenges are always skipped, regardless of the policy.

```yaml
unavailable_for_ai:
  - Discord Challenge
```

All other unsolved challenges are delegated to AI. Set a different file with
`CHALLENGE_POLICY_FILE` or `--challenge-policy-file`.

## Coordinator Backends

```bash
# Claude SDK coordinator (default)
uv run ctf-solve --coordinator claude ...

# Codex coordinator (GPT-5.4 via JSON-RPC)
uv run ctf-solve --coordinator codex ...
```

## Solver Models

Default model lineup (configurable in `backend/models.py`):

| Model | Provider | Notes |
|-------|----------|-------|
| GPT-5.5 | Codex | Primary general-purpose solver |
| GPT-5.4 | Codex | Best overall solver |
| GPT-5.4-mini | Codex | Fast, good for easy challenges |
| GPT-5.6-luna | Codex | Complementary solver |
| GPT-5.6-terra | Codex | Medium reasoning effort |
| Gemini 3.6 Flash | Google | Fast API-backed solver |

## Sandbox Tooling

Each solver gets an isolated Docker container pre-loaded with CTF tools:

| Category | Tools |
|----------|-------|
| **Binary** | radare2, GDB, objdump, binwalk, strings, readelf |
| **Pwn** | pwntools, ROPgadget, angr, unicorn, capstone |
| **Crypto** | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs |
| **Forensics** | volatility3, Sleuthkit (mmls/fls/icat), foremost, exiftool |
| **Stego** | steghide, stegseek, zsteg, ImageMagick, tesseract OCR |
| **Web** | curl, nmap, Python requests, flask |
| **Misc** | ffmpeg, sox, Pillow, numpy, scipy, PyTorch, podman |

## Features

- **Multi-model racing** — multiple AI models attack each challenge simultaneously
- **Auto-spawn** — new challenges detected and attacked automatically
- **Coordinator LLM** — reads solver traces, crafts targeted technical guidance
- **Cross-solver insights** — findings shared between models via message bus
- **Docker sandboxes** — isolated containers with full CTF tooling
- **Operator messaging** — send hints to running solvers mid-competition

## Configuration

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

```env
CTFD_URL=https://ctf.example.com
CTFD_TOKEN=ctfd_your_token
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
```

All settings can also be passed as environment variables or CLI flags.

## Example workflows

```bash
# Safely inspect one HTB challenge without submitting a flag.
uv run ctf-solve --platform htb --htb-event-id 1434 --htb-token "$HTB_TOKEN" --htb-mode http --no-submit --max-challenges 1 -v

# Run a CTFd event using its API token.
uv run ctf-solve --platform ctfd --ctfd-url https://ctf.example.com --ctfd-token "$CTFD_TOKEN"
```

## Troubleshooting

- **`0 challenges` or an HTML/JSON decoding error:** verify `HTB_EVENT_ID` and use a real expanded token (`--htb-token "$HTB_TOKEN"`, not a single-quoted literal `$HTB_TOKEN`). Try `--htb-mode http` if MCP is not available.
- **Instance startup timeout:** HTB acknowledged the request but has not published a connection target yet. Retry after a moment; the agent deliberately does not launch solvers without the target.
- **`401`/`403` from HTB:** renew the bearer token or authenticated CTF cookie. Browser session cookies and bearer tokens expire.
- **Team solved a challenge:** the coordinator automatically skips it. Add an unsolved challenge to `unavailable_for_ai` in `challenge-policy.yml` when you want to reserve it for humans.
- **Stopping cleanly:** press `Ctrl+C` once. The coordinator cancels swarms and stops its local Docker sandboxes; HTB remote instances are not stopped automatically unless explicitly requested by platform lifecycle code.

## Requirements

- Python 3.14+
- Docker
- API keys for at least one provider (Anthropic, OpenAI, Google)
- `codex` CLI (for Codex solver/coordinator)
- `claude` CLI (bundled with claude-agent-sdk)

## Acknowledgements

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — CTFd interaction and HTML helpers in `pull_challenges.py`
