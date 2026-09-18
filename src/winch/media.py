"""Media handling and rasterisation.

Provides PDF rasterisation via pdftoppm and the shared MediaError exception.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import tempfile


class MediaError(Exception):
    """Raised on media processing or LLM extraction failures."""


def pdf_to_pngs(pdf_bytes: bytes, dpi: int = 150, max_pages: int = 5) -> list[bytes]:
    """Rasterise via the pdftoppm binary. Raises MediaError on failure.
    Caps at max_pages — a 40-page spec attached to a quote must not be sent."""
    if not pdf_bytes:
        raise MediaError("Cannot rasterise empty PDF bytes.")
    if max_pages < 1:
        raise MediaError(f"max_pages must be at least 1, got {max_pages}.")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        input_pdf = tmp_path / "input.pdf"
        input_pdf.write_bytes(pdf_bytes)
        prefix = tmp_path / "page"

        cmd = [
            "pdftoppm",
            "-png",
            "-r",
            str(dpi),
            "-l",
            str(max_pages),
            str(input_pdf),
            str(prefix),
        ]

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as exc:
            raise MediaError(f"pdftoppm binary not found: {exc}") from None
        except Exception as exc:
            raise MediaError(f"Failed to run pdftoppm: {exc}") from None

        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            raise MediaError(f"pdftoppm failed (exit code {proc.returncode}): {err}")

        png_files = list(tmp_path.glob("page*.png"))
        if not png_files:
            raise MediaError("pdftoppm completed but produced no image output.")

        def _page_number(path: Path) -> int:
            match = re.search(r"-(\d+)\.png$", path.name)
            return int(match.group(1)) if match else 0

        png_files.sort(key=_page_number)
        png_files = png_files[:max_pages]

        result: list[bytes] = []
        for path in png_files:
            data = path.read_bytes()
            if not data:
                raise MediaError(f"pdftoppm produced empty image: {path.name}")
            result.append(data)

        return result
