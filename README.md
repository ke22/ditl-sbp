# ditl_sbp — DITL service design workbench

A research assistant for service designers. It turns qualitative research data into
Coding → JTBD → HMW → Persona → Customer Journey → Service Blueprint,
while keeping every researcher decision explicit and traceable to the original evidence.

**Status: Public Preview 0.1.2.** Runs on your own Mac, single user, no account required. macOS on Apple silicon only. See [CHANGELOG.md](CHANGELOG.md).

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

## 中文安裝說明

**需要什麼**

- Apple 晶片（M1 以後）的 Mac。Intel Mac、Windows 目前不能用。
- 一種 AI 執行方式（只有分析資料時需要，安裝和開啟不需要）：自己安裝並登入 Codex 或 Claude Code，或準備自己的 OpenAI API key。
- 會開「終端機」輸入指令。

**安裝（只要做一次）**

1. 到 [Releases](https://github.com/ke22/ditl-sbp/releases/latest) 下載 `ditl-sbp-0.1.2.zip`，雙擊解壓縮。
2. 在 Finder 對解壓後的 `ditl-sbp-0.1.2` 資料夾按右鍵，選「服務」→「新增位於檔案夾位置的終端機視窗」。（找不到這個選項時，打開「終端機」，輸入 `cd ` 加一個空格，把資料夾拖進視窗後按 Enter。）
3. 輸入下面這行並按 Enter，等到出現「安裝完成」（第一次約需 1–3 分鐘，要連網）：

   ```bash
   ./install.sh
   ```

   如果出現 `permission denied` 或被系統擋下，改輸入 `bash install.sh`。

**開啟**

```bash
ditl-sbp start
```

瀏覽器會自動打開工作台。如果安裝最後提示「目前不在你的 PATH 裡」，請改用它印出的完整路徑（例如 `~/.local/bin/ditl-sbp start`）。要關閉時，在終端機按 `Ctrl-C`。之後每次使用，只要再執行一次 `ditl-sbp start`。

**第一次使用**

右上角「設定」選擇 AI 執行方式（Codex、Claude Code 或 OpenAI API），接著在專案首頁選檔案、寫研究目的、建立專案。要上傳 PDF 需另外安裝 Poppler（`brew install poppler`），沒有的話請改用 TXT 或 MD。

**資料在哪裡**

研究資料只存在你的電腦：`~/Library/Application Support/ditl_sbp/`。重新安裝不會動到它。分析時，資料會送到你在「設定」選的 AI 服務。

**更新與移除**

- 更新：下載新版 ZIP，在新資料夾執行一次 `./install.sh`。研究資料不受影響。
- 移除程式：刪除 `~/.local/share/ditl-sbp` 資料夾與 `~/.local/bin/ditl-sbp`。研究資料不會一起刪除；要刪除資料，請自行刪除上面的資料夾。

**授權**：可以下載、安裝並用於研究、教學與工作；不得修改或轉傳，請他人直接從本頁下載。詳見 [LICENSE](LICENSE)。

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
