import asyncio
import contextlib
import shutil
import zipfile
from pathlib import Path

import fitz
from PIL import Image

from services.load_control import run_with_limit


def soffice_binary() -> str | None:
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    return None


async def convert_with_soffice(
    input_path: Path, target_ext: str, timeout_seconds: int
) -> Path:
    async def _run() -> Path:
        soffice = soffice_binary()
        if not soffice:
            raise RuntimeError(
                "LibreOffice topilmadi. Word/PDF konvert uchun LibreOffice o'rnatilishi kerak."
            )

        convert_arg = "pdf" if target_ext == "pdf" else "docx"
        process = await asyncio.create_subprocess_exec(
            soffice,
            "--headless",
            "--convert-to",
            convert_arg,
            "--outdir",
            str(input_path.parent),
            str(input_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            process.kill()
            with contextlib.suppress(Exception):
                await process.communicate()
            raise TimeoutError("Konvertatsiya vaqti tugadi (timeout).")

        if process.returncode != 0:
            error_text = (stderr or b"").decode("utf-8", errors="ignore").strip()
            raise RuntimeError(error_text or "LibreOffice konvertatsiya xatosi.")

        output_path = input_path.with_suffix(f".{target_ext}")
        if output_path.exists():
            return output_path

        candidates = sorted(input_path.parent.glob(f"{input_path.stem}*.{target_ext}"))
        if candidates:
            return candidates[0]
        raise RuntimeError("Konvertatsiya fayli topilmadi.")

    return await run_with_limit("converter", _run)


def image_to_pdf_sync(image_path: Path, output_path: Path) -> None:
    with Image.open(image_path) as image:
        converted = image.convert("RGB")
        converted.save(output_path, format="PDF")


def image_format_sync(image_path: Path, output_path: Path, target: str) -> None:
    with Image.open(image_path) as image:
        normalized = target.lower()
        if normalized == "jpg":
            image.convert("RGB").save(
                output_path, format="JPEG", quality=95, optimize=True
            )
            return
        if normalized == "png":
            image.save(output_path, format="PNG", optimize=True)
            return
        if normalized == "webp":
            image.convert("RGB").save(output_path, format="WEBP", quality=92, method=6)
            return
    raise ValueError("Noto'g'ri target format.")


def pdf_to_images_zip_sync(pdf_path: Path, zip_path: Path, max_pages: int) -> int:
    page_count = 0
    with fitz.open(pdf_path) as document:
        if document.page_count <= 0:
            raise ValueError("PDF bo'sh.")
        if document.page_count > max_pages:
            raise ValueError(f"PDF juda katta. Maksimal sahifa soni: {max_pages}.")

        with zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for index in range(document.page_count):
                page = document[index]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                image_name = f"{pdf_path.stem}_page_{index + 1:02d}.png"
                image_path = zip_path.parent / image_name
                pixmap.save(str(image_path))
                archive.write(image_path, arcname=image_name)
                image_path.unlink(missing_ok=True)
                page_count += 1
    return page_count
