# Changelog

## 0.2.1 — Fixes and MUJI example (2026-10-21)

- Fixed: transcripts that use `---` as a divider (common in Markdown) were cut apart when the files were read, so most evidence lost its file name and was attributed to 「原始資料」; 語意分段 then stopped with 「這些證據的來源不在上傳的逐字稿裡」. Files are now joined with a boundary that transcripts cannot contain. Projects whose files contain no `---` are unaffected and keep their evidence; projects whose files do should start a new coding run, since their earlier evidence attribution was unreliable.
- Fixed: an OpenAI API key pasted with invisible characters (such as a zero-width space) failed later with a technical `UnicodeEncodeError`. The key is now checked when you submit it, and the workbench asks you to paste it again.
- New: a finished example project (MUJI, 15 interviews, coding through service blueprint) is available as a separate download on the release page, `ditl-sbp-muji-demo-0.2.1.zip`. Open it with `ditl-sbp start --data-root <folder>`; it stays apart from your own research data.

## 0.2.0 — New interface (2026-10-21)

- New interface: the workbench opens on a step-by-step layout (建立專案 → AI 設定 → 資料與編碼 → JTBD → HMW → Persona → CJM → 服務藍圖 → 輸出). The previous interface stays at `/classic`; both read the same projects, and projects from 0.1.x open in either.
- Solutions are now developed and adopted at the Service Blueprint step, from the core problems you mark in the customer journey. You can rewrite a core problem in your own words.
- AI 設定 can set a model for each step. A model chosen here or through `SBP_CLI_MODEL` now actually reaches Codex and Claude Code, and the run record notes which model was requested.
- Faster recovery when an AI step fails part-way: finished parts of the category-properties step are kept and not redone; open coding fixes only the flagged segments or fields in more cases instead of rewriting the whole document; evidence alignment writes short source codes that the program expands back to the full file names.
- Persona: when a source file holds several people, the speakers you confirm are used so that one person is not counted as several, or several as one. Confirming speakers is a command-line step in this version.
- Fixed: the release package left out a module the workbench imports.

## 0.1.3 — Fix (2026-10-14)

- Fixed: 輸出研究產物 failed for every deliverable in an installed workbench, because the PDF printer's browser component was never installed. `./install.sh` now downloads the matching Chromium component (about 200 MB); if that download fails, the installation stops and the previous one keeps working.
- If the component is missing, the export says so in one plain sentence instead of showing the printer's own error.
- README: outputs are five PDFs (the coding export also includes an XLSX workbook); export is in the project's ⋯ menu.

## 0.1.2 — Fix (2026-10-14)

- Fixed: with the workbench started by `ditl-sbp start`, choosing Codex in 設定 showed 「已設定 ✕」 and AI steps could not run through Codex. The launcher now sets up the Codex runner the same way it sets up Claude Code.

## 0.1.1 — Fix (2026-10-13)

- Fixed: coding stopped at 語意分段 with 「判準來源檔不存在：schema/service-design-judgment.criteria.yaml」. The 0.1.0 package left out the judgment criteria file that every coding step reads; it is now included.
- The release build now loads the judgment criteria from the packaged copy and refuses to build if it cannot.

## 0.1.0 — Public Preview (2026-10-09)

First public preview. macOS on Apple silicon only.

### Research workflow

- From qualitative research data to a confirmed Service Blueprint: Coding (open → axial → selective) → JTBD → HMW → Persona → Customer Journey → Service Blueprint.
- AI drafts each step; the researcher chooses, edits and confirms. Each AI judgement can be traced back to the source quote it came from.
- The research-evaluation station after the Service Blueprint is hidden in this release.

### Setup

- `./install.sh` installs everything the workbench needs (its own Python 3.14 through uv) without Homebrew or a preinstalled Python. `ditl-sbp start` opens the workbench in your browser.
- Choose how AI runs in Settings: Codex, Claude Code, or your own OpenAI API key. A connection test runs only when you press it.
- Research data stays on your computer in `~/Library/Application Support/ditl_sbp/`. Reinstalling never touches it.

### Known limits

- macOS on Apple silicon only. Intel Macs, Windows and Linux are not supported yet.
- Reading PDF files needs Poppler; without it, use TXT or MD files.
- PDF export is not part of this release's supported scope.
