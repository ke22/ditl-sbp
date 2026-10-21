# ditl_sbp — DITL service design workbench

A research assistant for service designers. It turns qualitative research data into
Coding → JTBD → HMW → Persona → Customer Journey → Service Blueprint,
while keeping every researcher decision explicit and traceable to the original evidence.

**Status: Public Preview 0.2.1.** Runs on your own Mac, single user, no account required. macOS on Apple silicon only. See [CHANGELOG.md](CHANGELOG.md).

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

`install.sh` fetches its own pinned Python 3.14 (through uv) and the pinned Python packages; you do not need to install Python, uv or Homebrew first. It also downloads the matching Chromium browser component (about 200 MB) that prints the PDF exports; it goes to Playwright's shared cache, `~/Library/Caches/ms-playwright`. The program goes to `~/.local/share/ditl-sbp/` and the `ditl-sbp` command to `~/.local/bin/`. Re-running `./install.sh` is safe: a failed run leaves the previous installation working.

If `~/.local/bin` is not on your `PATH`, the installer says so at the end and prints the full path to run instead, plus a line you can add to your shell settings yourself. It does not edit your shell files.

Optional: to upload PDF files, install Poppler yourself (`brew install poppler`). Without it, the workbench tells you to use TXT or MD instead.

## 中文安裝說明

**需要什麼**

- Apple 晶片（M1 以後）的 Mac。Intel Mac、Windows 目前不能用。
- 一種 AI 執行方式（只有分析資料時需要，安裝和開啟不需要）：自己安裝並登入 Codex 或 Claude Code，或準備自己的 OpenAI API key。
- 會開「終端機」輸入指令。

**安裝（只要做一次）**

1. 到 [Releases](https://github.com/ke22/ditl-sbp/releases/latest) 下載 `ditl-sbp-0.2.1.zip`，雙擊解壓縮。
2. 在 Finder 對解壓後的 `ditl-sbp-0.2.1` 資料夾按右鍵，選「服務」→「新增位於檔案夾位置的終端機視窗」。（找不到這個選項時，打開「終端機」，輸入 `cd ` 加一個空格，把資料夾拖進視窗後按 Enter。）
3. 輸入下面這行並按 Enter，等到出現「安裝完成」（第一次約需 1–3 分鐘，要連網；會下載約 200 MB 的瀏覽器元件，用來輸出 PDF）：

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

在「建立專案」選檔案、寫研究目的，建立專案；接著在左側「AI 設定」選擇 AI 執行方式（Codex、Claude Code 或 OpenAI API）。要上傳 PDF 需另外安裝 Poppler（`brew install poppler`），沒有的話請改用 TXT 或 MD。

**資料在哪裡**

研究資料只存在你的電腦：`~/Library/Application Support/ditl_sbp/`。重新安裝不會動到它。分析時，資料會送到你在「AI 設定」選的 AI 服務。

**更新與移除**

- 更新：下載新版 ZIP，在新資料夾執行一次 `./install.sh`。研究資料不受影響。
- 移除程式：刪除 `~/.local/share/ditl-sbp` 資料夾與 `~/.local/bin/ditl-sbp`。研究資料不會一起刪除；要刪除資料，請自行刪除上面的資料夾。
- 輸出 PDF 用的瀏覽器元件放在 `~/Library/Caches/ms-playwright`，其他使用 Playwright 的程式可能共用這個資料夾，移除時不會自動刪除；確定沒有其他程式需要時，可以只刪除其中 `chromium_headless_shell-*` 開頭的資料夾。

**授權**：可以下載、安裝並用於研究、教學與工作；不得修改或轉傳，請他人直接從本頁下載。詳見 [LICENSE](LICENSE)。

## Configure AI execution

Open **AI 設定** in the left navigation. Choose Codex, Claude Code or OpenAI API; the choice applies to every AI step. (In the previous interface at `/classic`, the same choice is under **設定**, the menu button at the top right.)

### Option A — CLI (no API key)

The workbench hands each AI step to a CLI you are already logged in to.

**Codex**

1. Install the Codex CLI by following OpenAI's instructions, so that `codex` is on your `PATH`.
2. Log in: `codex login`.
3. In AI 設定, choose **Codex**. The page shows whether Codex was found and is logged in.

**Claude Code**

1. Install the Claude Code CLI by following Anthropic's instructions, so that `claude` is on your `PATH`.
2. Log in: run `claude` and use `/login`.
3. Start the workbench with `ditl-sbp start`; it configures the Claude Code runner for you. (Running from source instead? See [Development](#development).)
4. In AI 設定, choose **Claude Code**.

If the CLI is missing or not logged in, the workbench says so when you start an AI step (for example 「找不到 Codex CLI，請先安裝或改用 API 執行。」) and nothing is run.

### Option B — API (your own key)

In AI 設定, choose **OpenAI API**. You paste the key on that page; it is used only for the next submission and cleared afterwards. It is not written to disk.

### Cost

Both modes use model quota or paid API usage. Steps that call a model say so on screen before you start them (they mention CLI 額度). Browsing, editing and confirming do not call a model.

The workbench supports both modes; end-to-end runs have been exercised during development, but this preview has not yet been verified end-to-end from a fresh clone in either mode.

## Example project (MUJI)

To see a finished project before running your own, download `ditl-sbp-muji-demo-0.2.1.zip` from the [release page](https://github.com/ke22/ditl-sbp/releases/latest): 15 customer interviews for Café&Meal MUJI, carried from coding through to the service blueprint and confirmed by the researcher at every step. Unzip it and run:

```bash
ditl-sbp start --data-root ~/Downloads/ditl-sbp-muji-demo
```

On 建立專案, press 打開 next to MUJI-canonical under 接著做既有專案, then go through the steps on the left. The example stays in its own folder, apart from your research data; `ditl-sbp start` without `--data-root` returns to your own projects.

想先看一份完整做完的專案：到 Release 頁下載 `ditl-sbp-muji-demo-0.2.1.zip`，解壓縮後執行上面那行指令（路徑換成資料夾實際位置），在「建立專案」頁的「接著做既有專案」打開 MUJI-canonical，就能逐站看每一步的產出。範例資料和你自己的研究資料分開存放。


The workbench opens on the new interface (0.2.0). The left navigation lists the steps in order; a step you cannot do yet says what it is waiting for. The previous interface is still available at `/classic` and reads the same projects.

1. **建立專案** — choose your files (PDF, DOCX, XLSX, TXT, MD, VTT, SRT), write a one-line **研究目的** (research purpose) and press 建立專案. The research purpose frames the coding and the synthesis.
2. **AI 設定** — choose how AI runs: Codex, Claude Code or OpenAI API. Nothing is chosen for you; only AI steps need a choice. You can also pick a model for each step here; the choice lasts until you reload the page.
3. **資料與編碼** — press 開始編碼. AI organises the evidence and forms open, axial and selective coding, then proposes customer-job (JTBD) candidates and stops.
4. **JTBD** — choose one or more JTBD (or write your own) and confirm. HMW candidates are formed next.
5. **HMW** — choose the HMW to pursue and confirm.
6. **Persona** — confirm the Persona Matrix axes and the target segment, generate the Persona, edit it if needed and confirm.
7. **顧客旅程地圖（CJM）** — build the current journey, edit it, mark the core problems to solve (you can rewrite them) and confirm. Confirming only saves; it does not start AI.
8. **服務藍圖（SBP）** — develop solutions for the core problems (each split into people, physical environment, digital tools and digital environment), adopt the ones to carry forward, then generate the Service Blueprint. Generating also looks up how other services handle similar problems; these appear as external references and are design inspiration, not evidence from your participants. Review and confirm.
9. **輸出** — choose the deliverables and export them.

If an AI step fails, the screen says which step stopped, what was kept, and offers a retry (which uses quota again).

## Data and privacy

- Your research data stays on your computer, in `~/Library/Application Support/ditl_sbp/data/` (or the folder you pass with `ditl-sbp start --data-root /path/to/data`). Installing or re-installing never touches it.
- AI steps send research text to the CLI or API provider you chose. Nothing is sent until you start a step. Do not use a provider your consent agreements do not allow.
- Interview text, quotes, names and places can appear in the outputs. Check consent and anonymisation before sharing anything you export.
- Error reports sent with 回報這個問題 do not attach research data or interview text.

## Outputs

Export from **輸出** in the left navigation (in `/classic`: the project's ⋯ menu on Project Home → 輸出研究產物). The five research deliverables (coding, JTBD + HMW, Persona Matrix + Persona, customer journey map, service blueprint) export as PDF; the coding export also includes an XLSX workbook of the same content. The AI collaboration record exports as Markdown.

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
