"""OCR backend for mark-all-the-stuff-down.

Self-contained: depends only on stdlib, the bundled tessdata/, the system
`tesseract` and `pdftoppm` binaries, and (for .webp) a runtime-imported PIL.
Does NOT import from convert.py to keep the module reusable and to avoid
class-identity issues when convert.py runs as __main__.
"""
from __future__ import annotations

import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

TESSDATA_DIR = Path(__file__).parent / "tessdata"
_CONF_FLOOR = 30
_LOW_CONF_THRESHOLD = 60
_TESS_LANGS = "rus+eng"
_PSM = "1"


class OcrError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _require_tesseract() -> str:
    exe = shutil.which("tesseract")
    if not exe:
        raise OcrError("ocr-failed", "tesseract binary not found on PATH")
    if not TESSDATA_DIR.is_dir():
        raise OcrError(
            "ocr-failed",
            f"bundled tessdata directory missing: {TESSDATA_DIR}",
        )
    return exe


def _require_pdftoppm() -> str:
    exe = shutil.which("pdftoppm")
    if not exe:
        raise OcrError("ocr-failed", "pdftoppm binary not found on PATH")
    return exe


def _run_tesseract_tsv(exe: str, input_path: Path) -> str:
    try:
        res = subprocess.run(
            [
                exe,
                str(input_path),
                "stdout",
                "--tessdata-dir",
                str(TESSDATA_DIR),
                "-l",
                _TESS_LANGS,
                "--psm",
                _PSM,
                "-c",
                "tessedit_create_tsv=1",
                "tsv",
            ],
            capture_output=True,
            text=True,
            # tesseract emits UTF-8; the locale codec (cp1251 on Russian
            # Windows) cannot decode Cyrillic and the page comes out empty.
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as e:
        raise OcrError("ocr-failed", f"tesseract invocation failed: {e}") from e
    if res.returncode != 0:
        raise OcrError(
            "ocr-failed",
            f"tesseract exited {res.returncode}: {res.stderr.strip()[:500]}",
        )
    return res.stdout


def _parse_tsv_page(tsv_text: str) -> tuple[str, float, int]:
    """Group words into lines/paragraphs and compute post-filter stats.

    Returns (page_text, median_conf, word_count).
    Words with conf < 30 are dropped. Median is computed over surviving
    words only. Empty input or fully filtered -> ('', 0.0, 0).
    """
    if not tsv_text:
        return ("", 0.0, 0)
    lines = tsv_text.splitlines()
    if len(lines) <= 1:
        return ("", 0.0, 0)
    header = lines[0].split("\t")
    try:
        i_block = header.index("block_num")
        i_par = header.index("par_num")
        i_line = header.index("line_num")
        i_conf = header.index("conf")
        i_text = header.index("text")
    except ValueError:
        return ("", 0.0, 0)

    paragraphs: dict[tuple[int, int], dict[int, list[str]]] = {}
    confs: list[float] = []
    for raw in lines[1:]:
        if not raw.strip():
            continue
        cols = raw.split("\t")
        if len(cols) <= max(i_block, i_par, i_line, i_conf, i_text):
            continue
        try:
            conf = float(cols[i_conf])
        except ValueError:
            continue
        word = cols[i_text]
        if not word or not word.strip():
            continue
        if conf < _CONF_FLOOR:
            continue
        try:
            b = int(cols[i_block])
            p = int(cols[i_par])
            ln = int(cols[i_line])
        except ValueError:
            continue
        confs.append(conf)
        par = paragraphs.setdefault((b, p), {})
        par.setdefault(ln, []).append(word)

    if not confs:
        return ("", 0.0, 0)

    out_paragraphs: list[str] = []
    for (_b, _p), lines_dict in sorted(paragraphs.items()):
        rendered_lines = [
            " ".join(words)
            for _ln, words in sorted(lines_dict.items())
            if words
        ]
        if rendered_lines:
            out_paragraphs.append("\n".join(rendered_lines))
    text = "\n\n".join(out_paragraphs)
    median_conf = float(statistics.median(confs))
    return (text, median_conf, len(confs))


def _materialize_for_tesseract(src: Path, tmpdir: Path) -> Path:
    if src.suffix.lower() != ".webp":
        return src
    try:
        from PIL import Image  # runtime import
    except ImportError as e:
        raise OcrError(
            "install-failed",
            "webp requires Pillow; not available in this environment",
        ) from e
    out = tmpdir / (src.stem + ".png")
    with Image.open(src) as im:
        im.convert("RGB").save(out, format="PNG")
    return out


def _warn(msg: str) -> None:
    sys.stderr.write(f"WARN ocr-low-confidence: {msg}\n")


def convert_image(src: Path) -> tuple[str, str]:
    exe = _require_tesseract()
    with tempfile.TemporaryDirectory(prefix="matsd-ocr-") as td:
        tmp = Path(td)
        target = _materialize_for_tesseract(src, tmp)
        tsv = _run_tesseract_tsv(exe, target)
    text, conf, words = _parse_tsv_page(tsv)
    conf_int = int(round(conf))
    if conf < _LOW_CONF_THRESHOLD:
        _warn(f"conf={conf_int}")
    body_lines = [f"<!-- ocr conf={conf_int} words={words} -->"]
    if text:
        body_lines.append(text)
    body = "\n".join(body_lines) + "\n"
    return body, "tesseract-ocr"


def convert_pdf_scanned(src: Path, total_pages: int) -> tuple[str, str]:
    tess = _require_tesseract()
    ppm = _require_pdftoppm()
    if total_pages <= 0:
        raise OcrError("ocr-failed", f"invalid total_pages={total_pages}")
    pages_md: list[str] = []
    with tempfile.TemporaryDirectory(prefix="matsd-pdf-") as td:
        tmp = Path(td)
        for n in range(1, total_pages + 1):
            stem = tmp / "page"
            try:
                res = subprocess.run(
                    [
                        ppm,
                        "-f",
                        str(n),
                        "-l",
                        str(n),
                        "-r",
                        "300",
                        "-gray",
                        str(src),
                        str(stem),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            except OSError as e:
                raise OcrError(
                    "ocr-failed", f"pdftoppm failed on page {n}: {e}"
                ) from e
            if res.returncode != 0:
                raise OcrError(
                    "ocr-failed",
                    f"pdftoppm exited {res.returncode} on page {n}: "
                    f"{res.stderr.strip()[:300]}",
                )
            matches = sorted(tmp.glob("page-*.pgm"))
            if len(matches) != 1:
                raise OcrError(
                    "ocr-failed",
                    f"expected 1 page-*.pgm in tmpdir, found {len(matches)}",
                )
            pgm = matches[0]
            try:
                tsv = _run_tesseract_tsv(tess, pgm)
            finally:
                try:
                    pgm.unlink()
                except OSError:
                    pass
            text, conf, words = _parse_tsv_page(tsv)
            conf_int = int(round(conf))
            if conf < _LOW_CONF_THRESHOLD:
                _warn(f"page {n} conf={conf_int}")
            block = [
                f"## Page {n}",
                f"<!-- ocr page={n} conf={conf_int} words={words} -->",
            ]
            if text:
                block.append(text)
            pages_md.append("\n".join(block))
    return ("\n\n".join(pages_md) + "\n", "tesseract-ocr")
