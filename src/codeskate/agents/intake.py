"""Agent 1 — Intake.

Reads whatever you drop into data/inbox/ (resume PDF, brag doc, notes) and
returns one plain-text bundle. Deliberately dumb: no LLM, no cost.
"""

from __future__ import annotations

from pathlib import Path

from ..settings import INBOX_DIR

TEXT_SUFFIXES = {".md", ".txt", ".markdown", ".rst"}


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def collect(inbox: Path | None = None) -> tuple[str, list[str]]:
    """Return (bundled_text, list_of_filenames_read)."""
    inbox = inbox or INBOX_DIR
    if not inbox.exists():
        raise SystemExit(f"inbox not found: {inbox}")

    parts: list[str] = []
    read: list[str] = []

    for path in sorted(inbox.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        suffix = path.suffix.lower()
        try:
            if suffix == ".pdf":
                content = _read_pdf(path)
            elif suffix in TEXT_SUFFIXES:
                content = path.read_text(encoding="utf-8", errors="replace")
            else:
                continue
        except Exception as e:  # noqa: BLE001 - one bad file shouldn't kill intake
            print(f"  ! skipped {path.name}: {e}")
            continue

        content = content.strip()
        if not content:
            continue
        parts.append(f"===== FILE: {path.name} =====\n{content}")
        read.append(path.name)

    if not parts:
        raise SystemExit(
            f"No readable files in {inbox}.\n"
            "Drop your resume (.pdf) and brag document (.md) there, then re-run."
        )

    return "\n\n".join(parts), read
