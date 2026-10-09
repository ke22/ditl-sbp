# ditl_sbp — DITL service design workbench

A research assistant for service designers. It turns qualitative research data into
Coding → JTBD → HMW → Persona → Customer Journey → Service Blueprint,
while keeping every researcher decision explicit and traceable to the original evidence.

**Status: Public Preview 0.1.0.** Runs on your own Mac, single user, no account required. macOS on Apple silicon only. See [CHANGELOG.md](CHANGELOG.md).

## What it does

```
Research data (interviews, field notes)
→ Coding (open → axial → selective)
→ JTBD (customer jobs)
→ HMW (how might we)
→ Persona
→ Customer Journey (with solution moments)
→ Service Blueprint
```

AI proposes and synthesises; the researcher selects, edits and confirms. The workbench stops at each point where a research judgement is needed (for example choosing JTBD, choosing the HMW, confirming the Persona, the journey and the blueprint), and every AI judgement can be traced back to the source quote it came from.

## Install (macOS, Apple silicon)

You need:

- A Mac with Apple silicon (M1 or later). Intel Macs, Windows and Linux are not supported yet.
- A current browser (Safari, Chrome, Edge or Firefox).
- **One way to run AI** — needed to analyse data, not to start the app: the Codex CLI or the Claude Code CLI, installed and logged in by you, or your own OpenAI API key.

Download the release ZIP, unzip it, and in that folder run:

```bash
./install.sh
ditl-sbp start
```

`install.sh` fetches its own pinned Python 3.14 (through uv) and the pinned Python packages; you do not need to install Python, uv or Homebrew first. The program goes to `~/.local/share/ditl-sbp/` and the `ditl-sbp` command to `~/.local/bin/`. Re-running `./install.sh` is safe: a failed run leaves the previous installation working.

If `~/.local/bin` is not on your `PATH`, the installer says so at the end and prints the full path to run instead, plus a line you can add to your shell settings yourself. It does not edit your shell files.

Optional: to upload PDF files, install Poppler yourself (`brew install poppler`). Without it, the workbench tells you to use TXT or MD instead.

## Configure AI execution

Open **設定** (the menu button at the top right). Choose one of the two modes; the choice applies to every AI step.

### Option A — CLI (no API key)

The workbench hands each AI step to a CLI you are already logged in to.

**Codex**

1. Install the Codex CLI by following OpenAI's instructions, so that `codex` is on your `PATH`.
2. Log in: `codex login`.
3. In 設定, choose **CLI** and the **Codex** executor. The panel shows whether Codex was found and is logged in.

**Claude Code**

1. Install the Claude Code CLI by following Anthropic's instructions, so that `claude` is on your `PATH`.
2. Log in: run `claude` and use `/login`.
3. Start the workbench with `ditl-sbp start`; it configures the Claude Code runner for you. (Running from source instead? See [Development](#development).)
4. In 設定, choose **CLI** and the **Claude Code** executor.

If the CLI is missing or not logged in, the workbench says so when you start an AI step (for example 「找不到 Codex CLI，請先安裝或改用 API 執行。」) and nothing is run.

### Option B — API (your own key)

In 設定, choose **API**. The provider is **OpenAI**. You paste the key in the panel; it is used only for the next submission and cleared afterwards. It is not written to disk.

### Cost

Both modes use model quota or paid API usage. Steps that call a model say so on screen before you start them (they mention CLI 額度). Browsing, editing and confirming do not call a model.

The workbench supports both modes; end-to-end runs have been exercised during development, but this preview has not yet been verified end-to-end from a fresh clone in either mode.

## Your first research project

0. **設定** (top right) — choose how AI runs: Codex, Claude Code or OpenAI API. Nothing is chosen for you, and creating a project or viewing results works without a choice; only AI steps need one. The panel shows what it can observe (已安裝／已登入／已設定); 測試連線 is optional and only sends a request when you press it (Codex and Claude Code use a small amount of quota).
1. **專案首頁** — choose your files (PDF, DOCX, XLSX, TXT, MD, VTT, SRT) and press 建立專案.
2. Write a one-line **研究目的** (research purpose) on the same screen. It frames the coding and the synthesis.
3. Press **開始編碼**. AI organises the evidence and forms open, axial and selective coding, then proposes customer-job (JTBD) candidates and stops.
4. **資料與編碼** — choose one or more JTBD (or write your own), then continue. HMW candidates are formed next.
5. **HMW** — choose the HMW to pursue.
6. **Persona** — confirm the Persona Matrix axes and the target segment, generate the Persona, edit it if needed and confirm. The customer journey is built next.
7. **顧客旅程** — edit the journey, tick the moments you want solutions for, develop solutions, and confirm.
8. **服務藍圖** — optionally run 落地研究 (implementation research) first, then build the Service Blueprint, review it, and confirm. Confirming saves it; export it from 輸出.

The research evaluation station that followed the Service Blueprint is deferred in this Public Preview and will return, redesigned, in a later version.

If an AI step fails, the screen says which step stopped, what was kept, and offers a retry (which uses quota again).

## Data and privacy

- Your research data stays on your computer, in `~/Library/Application Support/ditl_sbp/data/` (or the folder you pass with `ditl-sbp start --data-root /path/to/data`). Installing or re-installing never touches it.
- AI steps send research text to the CLI or API provider you chose. Nothing is sent until you start a step. Do not use a provider your consent agreements do not allow.
- Interview text, quotes, names and places can appear in the outputs. Check consent and anonymisation before sharing anything you export.
- Error reports sent with 回報這個問題 do not attach research data or interview text.

## Outputs

Each station can export client-facing deliverables (coding, JTBD + HMW, Persona, customer journey, service blueprint). XLSX export works with the standard install. PDF export is not part of this release's supported scope.

## License

ditl_sbp is not open source. You may install and use unmodified copies for your own research, teaching or professional work; modifying or redistributing it is not permitted. See [LICENSE](LICENSE). Components downloaded by the installer keep their own licenses: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Development

The release ZIP contains only what is needed to install and run the workbench. Developers working from the full source tree can run it without the installer:

```bash
python3.14 -m venv venv
venv/bin/python -m pip install -r requirements.lock
venv/bin/python tools/sbp_review/server.py
```

Then open **http://127.0.0.1:8765**. The server prints the same address when it starts; stop it with Ctrl-C.

Starting the app needs no AI setup and no research data; you will see an empty Project Home.

`requirements.lock` is the full, pinned install. Some packages in it serve the API providers or a server deployment (for example `psycopg`); you do not need to configure those to run the workbench locally.

Useful options:

```bash
venv/bin/python tools/sbp_review/server.py --port 8800              # another port
venv/bin/python tools/sbp_review/server.py --data-root /path/to/data # keep data outside the repo
```

To use Claude Code when running from source, start the server with the runner configured:

```bash
SBP_CLI_RUNNER_COMMAND_CLAUDE="$PWD/venv/bin/python $PWD/tools/cli_runners/claude.py" \
  venv/bin/python tools/sbp_review/server.py
```

Tests, specifications (`openspec/`) and contributor rules (`AGENTS.md`, `CLAUDE.md`) live in the development repository and are not part of the release.
