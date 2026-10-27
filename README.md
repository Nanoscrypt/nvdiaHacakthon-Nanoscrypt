<div align="center">

# Nanoscrypt

**Autonomous, Self-Healing Tool Synthesis & Multi-Agent Execution Framework**

[![PyPI Version](https://img.shields.io/pypi/v/nanoscrypt?color=blue&style=flat-square)](https://pypi.org/project/nanoscrypt/)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://pypi.org/project/nanoscrypt/)
[![CI Suite](https://img.shields.io/github/actions/workflow/status/Nanoscrypt/Nanoscrypt/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/Nanoscrypt/Nanoscrypt/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg?style=flat-square)](https://github.com/astral-sh/ruff)

[**Quickstart**](#-quickstart) •
[**Interactive Setup**](#-interactive-setup--provider-configuration) •
[**Architecture**](#-architecture) •
[**CLI & Slash Commands**](#-cli--slash-commands) •
[**REST API**](#-rest-api) •
[**Documentation**](#-core-components)

</div>

---

##  Overview

**Nanoscrypt** is a lightweight, self-healing agentic framework that dynamically synthesizes, validates, sandboxes, versions, and reuses tools on demand. 

Instead of relying on brittle, predefined toolsets, Nanoscrypt:
1. **Plans & Synthesizes**: Evaluates natural language instructions and automatically writes fully functional, typed Python tools or full-stack web applications on the fly.
2. **Multi-Stage Validation**: Statically inspects AST security, typing contracts, import dependencies, and cyclomatic complexity before execution.
3. **Self-Healing Repair Loop**: Detects syntax errors, missing dependencies, or runtime crashes and executes an automated diagnostic repair loop.
4. **Persistent Registry & Memory**: Snapshots verified tools into an immutable SQLite registry for instant cross-session reuse with zero LLM latency.
5. **Universal LLM Support**: Supports any provider (**OpenAI**, **Anthropic**, **Google Gemini**, **Groq**, **DeepSeek**, **OpenRouter**, or **Local Ollama**) out of the box.

---

## Nebius Token Factory (hackathon setup)

Nanoscrypt can send inference requests to Nebius Token Factory using its OpenAI-compatible API and an NVIDIA model enabled for your project. Create a local `.env` from `.env.example`, then set `NEBIUS_API_KEY` and set `NANOSCRYPT_LLM__MODEL` to `nebius/<exact-model-id>` from your Token Factory catalog. The local `.env` file is loaded by Nanoscrypt; the key must not be committed. The default Token Factory endpoint is `https://api.tokenfactory.nebius.com/v1`.

After configuring a model ID, run `python examples/nebius_smoke.py` to make one live inference call. This verifies model access only. Once Nebius Sandboxes beta access is enabled, install the optional integration with `pip install -e ".[nebius]"` on Python 3.12, set `NEBIUS_PROJECT_ID`, and set `NANOSCRYPT_RUNTIME__EXECUTION_BACKEND=nebius_sandbox` to route generated-tool execution through Token Factory Sandboxes. The default backend remains local. Sandbox runs are disposable; the CLI now displays stdout and stderr live from Nebius SSE, then reports the final execution result and exit code. The in-process interactive code-agent loop remains local for now; its RPC-driven process needs a separate streaming/session adapter before it can run remotely.

##  Quickstart

### 1. Installation

```bash
pip install nanoscrypt
```

*Or install locally from source:*
```bash
git clone https://github.com/Nanoscrypt/Nanoscrypt.git
cd Nanoscrypt
pip install -e .
```

### 2. Interactive Setup

Run the built-in wizard to configure your preferred LLM provider, API key, and model:

```bash
nanoscrypt setup
```

```text
Select Provider:
  1. OpenAI          (gpt-4o, gpt-4o-mini, o1, o3-mini)
  2. Anthropic       (claude-3-5-sonnet, claude-3-5-haiku)
  3. Google Gemini   (gemini-1.5-pro, gemini-1.5-flash, gemini-2.0-flash)
  4. Groq            (llama-3.3-70b-versatile, deepseek-r1-distill)
  5. DeepSeek        (deepseek-chat, deepseek-reasoner)
  6. OpenRouter      (openrouter/anthropic/claude-3.5-sonnet)
  7. Ollama (Local)  (qwen2.5-coder, deepseek-r1, llama3.2)
  8. Custom LiteLLM

✓ Credentials and preferences saved to ~/.nanoscrypt/config.toml
```

### 3. Launch Interactive REPL or Run Tasks

#### Direct CLI Task Execution:
```bash
nanoscrypt run "Parse financial_report.pdf and extract all revenue tables into structured JSON"
```

#### Interactive Developer REPL:
```bash
nanoscrypt run
```

```text
Nanoscrypt - Live Execution Runtime v0.2.0
Session: cli_8f21bc90 | Agent: Default Orchestrator | Sandbox: Process Isolation

• Signed in successfully as user!

> Build a full-stack URL shortener with FastAPI and SQLite
```
---

##  CLI & Slash Commands

Nanoscrypt includes a high-performance CLI with autocomplete, file path suggestions, and real-time execution metrics.

### Commands

| Command | Description |
| :--- | :--- |
| `nanoscrypt setup` | Configure LLM provider, API keys, and models stored at user root (`~/.nanoscrypt/config.toml`) |
| `nanoscrypt run [prompt]` | Execute a single task or enter the interactive developer REPL |
| `nanoscrypt init` | Initialize a project-level `nanoscrypt.toml` configuration |
| `nanoscrypt serve` | Launch the FastAPI REST API server |
| `nanoscrypt tools list` | Search and inspect synthesized tools in the local database |
| `nanoscrypt agents list` | Manage and inspect role-based agent profiles |

### REPL Slash Commands

Inside `nanoscrypt run`, use instant slash commands:

- `/setup` — Re-open the interactive provider & API key wizard.
- `/config` — Display active configuration and masked credentials.
- `/model [name]` — Inspect or hot-swap the active model (e.g. `/model gpt-4o-mini`).
- `/cost` — View real-time token counts and estimated USD costs.
- `/memory search <query>` — Perform vector semantic search over persistent memories.
- `/profile` — View or set persistent user traits and preferences.
- `/clear` — Clear current session history.
- `/exit` — Exit REPL cleanly.

---

## 🛠 Core Components

### 1. Multi-Stage Static Validator
Before executing any LLM-synthesized code, Nanoscrypt evaluates it through a strict 11-stage static pipeline:
- **Syntax & Security AST**: Validates Python AST and blocks dangerous constructs (`eval`, `exec`, arbitrary bytecode).
- **Import & Schema Verification**: Auto-resolves missing dependencies, syncs input/output parameter signatures, and prevents undefined symbol crashes (`F821`).
- **Complexity & Quality**: Enforces cyclomatic complexity thresholds and runs automated linting via **Ruff**.

### 2. Self-Healing Repair Loop
When code fails validation or raises a runtime error:
- Nanoscrypt classifies the error (`syntax`, `import`, `timeout`, `schema_mismatch`, `name_error`).
- Selects a progressive strategy (Targeted Patch → Refactor → Clean Synthesis).
- Auto-injects common standard imports (`Optional`, `datetime`, `shutil`, `os`, `sys`, `json`) and missing third-party packages into `requirements.txt`.

### 3. Shared Environment & Versioning
- **Zero-Latency Virtual Environment**: Caches dependencies in a centralized directory, eliminating repeated package installs.
- **Immutable Snapshots**: Stores verified tools with SHA-256 integrity hashes in `generated_tools/{name}/v{n}/`.

---

##  REST API

Start the REST API server:
```bash
nanoscrypt serve --host 127.0.0.1 --port 8000
```
Interactive OpenAPI documentation is hosted at `http://127.0.0.1:8000/docs`.

### Key Endpoints

- `POST /api/v1/tasks?session_id={id}` — Execute orchestrated natural language prompt.
- `POST /api/v1/sessions` — Create an isolated workspace session.
- `GET /api/v1/tools` — Search and filter synthesized tools.
- `GET /api/v1/approvals/pending` — List human-in-the-loop authorization gates.
- `GET /api/v1/audit/summary` — Aggregate cost, token counts, and execution metrics.

### Generated Tool Lifecycle

Newly generated tools begin as **candidates** and are excluded from future agent tool discovery. A clean process exit records execution health, but does not count as evidence that a tool helped complete its task. Existing registry entries without lifecycle records remain available as shared tools.

An API client can submit explicit contribution feedback for a completed execution:

```http
POST /api/v1/tools/outcomes
Content-Type: application/json

{
  "execution_id": 42,
  "task_key": "xml-to-json-conversion",
  "contribution": "helpful",
  "evidence": "The requested XML fields were converted and verified in the output."
}
```

The contribution must be `helpful`, `neutral`, or `harmful`. Use a stable task key for each distinct task family. Candidate versions become shared after at least five reviews across three distinct task keys, with at least 80% marked helpful. Repeated harmful feedback moves a candidate version to quarantine or a shared version to review. Lifecycle evidence and state are stored per tool version; late feedback for an older version cannot promote or quarantine the current version. The current version's state and reason are available from `GET /api/v1/tools/{name}/lifecycle`. Add `?version={n}` to inspect an older version. `PUT` on that path lets an operator quarantine, retire, or return a selected version to candidate review; omit `version` to target the current version. Only accumulated outcome evidence can promote a candidate version to shared. Existing tool-level outcomes are evaluated separately for each version during migration. A legacy shared label without version-specific evidence does not grant shared access; a legacy quarantine or retirement is preserved for the current version.

Task responses include `execution_id` so clients can attach outcome feedback. Feedback is optional; until enough evidence is supplied, a new tool remains available for its generating run but is not offered for reuse in later plans.

For the CLI, `nanoscrypt run` displays the execution ID. Record feedback with:

```bash
nanoscrypt tools outcome 42 --task-key xml-to-json-conversion --contribution helpful --evidence "Verified the converted fields against the requested output."
```

---

##  Configuration

Nanoscrypt merges configurations in a clean hierarchy:
1. **User Root** (`~/.nanoscrypt/config.toml`): Global credentials, API keys, and default provider.
2. **Project Level** (`nanoscrypt.toml`): Project-specific workspace paths, timeouts, and sandbox limits.
3. **Environment Variables**: Overrides prefixed with `NANOSCRYPT_` (e.g. `NANOSCRYPT_LLM__MODEL`).

```toml
[llm]
provider = "openai"
model = "gpt-4o"
temperature = 0.2
max_tokens = 131072
max_output_tokens = 4096

[runtime]
timeout_seconds = 60
max_agent_turns = 12
max_memory_mb = 512
workspace_root = "./workspaces"

[registry]
database_url = "sqlite+aiosqlite:///./registry/tools.db"
tools_dir = "./generated_tools"

[security]
approval_mode = "interactive"
default_risk_threshold = "medium"
max_file_write_mb = 10
```

### Harness sessions

The harness runs bounded turns, carries recent conversation into queued follow-ups, and saves session messages and tool history under `.nanoscrypt_sessions/` inside the configured workspace root. Reuse a session with `nanoscrypt run --session-id <id> "..."`; `/clear` clears its saved conversation and tool history. The default turn limit is 12 and can be changed with `runtime.max_agent_turns`.

Cancellation can stop active generated-tool and code-agent subprocesses; dependency installation and application-launch processes do not yet share this cancellation path. CapSem is optional; when explicitly enabled but unavailable, generated-tool and code-agent execution are blocked instead of silently falling back. CapSem is not currently applied to application launches. Process isolation and a virtual environment alone are not a security sandbox, and the current `max_memory_mb` setting is not enforced by the runtime.

---

##  Testing

Run the unit test suite across Python 3.10+:

```bash
pytest tests/unit/ -v
```

Run test suite with coverage:
```bash
pytest tests/ --cov=nanoscrypt --cov-report=term-missing
```

---

##  Contributing

Contributions are welcome! Please follow these standards:
1. **Python 3.10+ Compatibility**: Ensure code adheres to Python 3.10 typing standards.
2. **Code Style**: Run `ruff check --fix` and `ruff format` before submitting PRs.
3. **Unit Tests**: Add unit tests in `tests/unit/` for all new features.

---

##  License

This project is licensed under the terms of the [MIT License](LICENSE).
