# Changelog

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
