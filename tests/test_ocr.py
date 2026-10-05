import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest


SAMPLE_TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
    "left\ttop\twidth\theight\tconf\ttext\n"
    # block 1, par 1, line 1: "Hello world" both high-conf
    "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t90\tHello\n"
    "5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t88\tworld\n"
    # block 1, par 1, line 2: "noise" low-conf must be dropped
    "5\t1\t1\t1\t2\t1\t0\t0\t10\t10\t12\tnoise\n"
    # block 2, par 1, line 1: "second paragraph"
    "5\t1\t2\t1\t1\t1\t0\t0\t10\t10\t80\tsecond\n"
    "5\t1\t2\t1\t1\t2\t0\t0\t10\t10\t70\tparagraph\n"
)


def _single_word_tsv(word="TEXT", conf=90):
    return (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        f"5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t{conf}\t{word}\n"
    )


def test_tsv_parsing_groups_and_filters():
    from ocr import _parse_tsv_page

    text, conf, words = _parse_tsv_page(SAMPLE_TSV)
    assert "Hello world" in text
    assert "second paragraph" in text
    assert "noise" not in text
    # paragraphs separated by blank line
    assert "world\n\nsecond" in text
    assert words == 4  # noise dropped
    # median of [90, 88, 80, 70] = 84.0
    assert conf == pytest.approx(84.0)


def test_tsv_parsing_empty_after_filter():
    from ocr import _parse_tsv_page

    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t5\tjunk\n"
    )
    text, conf, words = _parse_tsv_page(tsv)
    assert text == ""
    assert conf == 0.0
    assert words == 0


def test_tsv_parsing_empty_input():
    from ocr import _parse_tsv_page

    text, conf, words = _parse_tsv_page("")
    assert (text, conf, words) == ("", 0.0, 0)


def test_materialize_passthrough(tmp_path):
    from ocr import _materialize_for_tesseract

    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert _materialize_for_tesseract(p, tmp_path) == p


def test_materialize_webp_without_pillow(tmp_path, monkeypatch):
    import builtins

    from ocr import OcrError, _materialize_for_tesseract

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("PIL not available")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    src = tmp_path / "x.webp"
    src.write_bytes(b"RIFF....WEBP")
    with pytest.raises(OcrError) as ei:
        _materialize_for_tesseract(src, tmp_path)
    assert ei.value.kind == "install-failed"
    assert "Pillow" in ei.value.message


def test_materialize_webp_with_pillow(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    from ocr import _materialize_for_tesseract

    src = tmp_path / "x.webp"
    Image.new("RGB", (8, 8), "white").save(src, format="WEBP")
    out = _materialize_for_tesseract(src, tmp_path)
    assert out.suffix == ".png"
    assert out.exists()


def test_convert_image_low_confidence_warns_once(tmp_path, monkeypatch, capsys):
    import ocr

    src = tmp_path / "low.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(ocr, "_require_tesseract", lambda: "/fake/tesseract")
    monkeypatch.setattr(ocr, "_run_tesseract_tsv", lambda _exe, _src: _single_word_tsv("LOW", 55))

    body, backend = ocr.convert_image(src)

    assert backend == "tesseract-ocr"
    assert "<!-- ocr conf=55 words=1 -->" in body
    assert "LOW" in body
    assert capsys.readouterr().err == "WARN ocr-low-confidence: conf=55\n"


def test_run_tesseract_tsv_decodes_utf8_regardless_of_locale(tmp_path, monkeypatch):
    """tesseract writes TSV as UTF-8. Decoding it with the locale encoding
    (cp1251 on Russian Windows) fails on Cyrillic: "И" is b"\\xd0\\x98" and
    0x98 is undefined in cp1251, so the page came out empty with conf=0."""
    import subprocess

    import ocr

    raw = _single_word_tsv("ПРЕТЕНЗИЯ", 96).encode("utf-8")

    def fake_run(args, **kwargs):
        # Mimic subprocess text mode on a cp1251 locale: without an explicit
        # encoding the output is decoded with the locale codec.
        encoding = kwargs.get("encoding") or "cp1251"
        stdout = raw.decode(encoding, kwargs.get("errors") or "strict")
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)

    tsv = ocr._run_tesseract_tsv("/fake/tesseract", tmp_path / "page.png")
    text, conf, words = ocr._parse_tsv_page(tsv)

    assert text == "ПРЕТЕНЗИЯ"
    assert (conf, words) == (96.0, 1)


def test_convert_pdf_scanned_formats_pages_and_removes_pgm(tmp_path, monkeypatch):
    import subprocess

    import ocr

    created = []
    pdftoppm_calls = []

    def fake_which(name):
        return f"/fake/{name}"

    def fake_run(args, capture_output, text, check, encoding, errors):
        assert (encoding, errors) == ("utf-8", "replace")
        pdftoppm_calls.append(args)
        stem = Path(args[-1])
        page = args[args.index("-f") + 1]
        pgm = stem.parent / f"page-{page}.pgm"
        pgm.write_text("pgm", encoding="utf-8")
        created.append(pgm)
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_tesseract(_exe, input_path):
        assert input_path.exists()
        return _single_word_tsv(f"PAGE{input_path.stem.rsplit('-', 1)[-1]}", 91)

    monkeypatch.setattr(ocr.shutil, "which", fake_which)
    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    monkeypatch.setattr(ocr, "_run_tesseract_tsv", fake_tesseract)

    body, backend = ocr.convert_pdf_scanned(tmp_path / "scan.pdf", 2)

    assert backend == "tesseract-ocr"
    assert "## Page 1\n<!-- ocr page=1 conf=91 words=1 -->\nPAGE1" in body
    assert "## Page 2\n<!-- ocr page=2 conf=91 words=1 -->\nPAGE2" in body
    assert len(pdftoppm_calls) == 2
    assert all(not pgm.exists() for pgm in created)


@pytest.mark.ocr
def test_convert_image_smoke(tmp_path):
    import shutil

    if not shutil.which("tesseract"):
        pytest.skip("tesseract not on PATH")
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1000, 260), "white")
    d = ImageDraw.Draw(img)
    for font_name in (
        "DejaVuSans.ttf",
        "Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ):
        try:
            font = ImageFont.truetype(font_name, 72)
            break
        except OSError:
            font = None
    if font is None:
        font = ImageFont.load_default()
    d.text((80, 80), "HELLO OCR", fill="black", font=font)
    src = tmp_path / "hello.png"
    img.save(src)

    from ocr import convert_image

    body, backend = convert_image(src)
    assert backend == "tesseract-ocr"
    assert "HELLO" in body.upper()
    assert "<!-- ocr conf=" in body
    assert "## Page" not in body  # single image: no page heading
