"""매입 기준표 판독 시험 PDF를 만든다(tests/v3/fixtures/criteria_pdf).

같은 PP 매입 조건(PP 사출 스크랩 A등급, kg당 320원 매입, 최소 월 1,000kg, 이물 3%·수분 5% 이하, 수거 포함)을
양식만 바꿔 적은 문서들이다. src/*.html을 Chrome으로 인쇄하고, 07_scanned.pdf는 04_merged_cells.pdf를
글자층 없는 스캔 그림 PDF로 바꾼다. 모두 가상 업체·가상 수치다.

    uv run python -m scripts.v3.eval.criteria_fixtures            # 다시 만들기
    CHROME=/path/to/chrome uv run python -m scripts.v3.eval.criteria_fixtures
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[3] / "tests" / "v3" / "fixtures" / "criteria_pdf"
CHROME = os.environ.get("CHROME") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def print_pdf(source: Path, target: Path) -> None:
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(
            [
                CHROME,
                "--headless",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--user-data-dir={profile}",
                f"--print-to-pdf={target}",
                source.resolve().as_uri(),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )


def scan(source: Path, target: Path, seed: int = 7) -> None:
    """글자층이 없는 스캔 PDF. 200dpi 회색조, 0.8° 기울기, 옅은 잡음, JPEG 압축."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter

    rng = random.Random(seed)
    document = pdfium.PdfDocument(str(source))
    pages = []
    for index in range(len(document)):
        image = document[index].render(scale=200 / 72).to_pil().convert("L")
        image = image.rotate(0.8, resample=Image.Resampling.BICUBIC, fillcolor=250)
        pixels = image.load()
        width, height = image.size
        for _ in range(width * height // 60):
            x, y = rng.randrange(width), rng.randrange(height)
            pixels[x, y] = max(0, min(255, pixels[x, y] + rng.randint(-60, 25)))
        image = image.filter(ImageFilter.GaussianBlur(0.45))
        with tempfile.NamedTemporaryFile(suffix=".jpg") as jpeg:
            image.save(jpeg.name, "JPEG", quality=70)
            pages.append(Image.open(jpeg.name).copy())
    pages[0].save(target, "PDF", resolution=200, save_all=True, append_images=pages[1:])


def main() -> int:
    import pypdfium2 as pdfium

    for source in sorted((HERE / "src").glob("*.html")):
        target = HERE / f"{source.stem}.pdf"
        print_pdf(source, target)
    scan(HERE / "04_merged_cells.pdf", HERE / "07_scanned.pdf")
    for pdf in sorted(HERE.glob("*.pdf")):
        document = pdfium.PdfDocument(str(pdf))
        texts = [document[i].get_textpage().get_text_range() for i in range(len(document))]
        where = [i + 1 for i, text in enumerate(texts) if "320" in text]
        print(
            f"{pdf.name}: {len(document)}쪽, 글자층 {sum(map(len, texts))}자, '320'이 있는 쪽 {where}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
