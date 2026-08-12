"""
Image Processing Utilities
==========================

Functions for image conversion, optimization, and validation.
"""

import io
import os
import logging
import tempfile
import uuid
from typing import Optional, Tuple, List
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
import fitz  # PyMuPDF for PDF processing

from src.config.constants import IMAGE_LIMITS, SUPPORTED_FORMATS


log = logging.getLogger(__name__)


class ImageProcessingError(Exception):
    """Raised when image processing fails."""
    pass


class ImageConverter:
    """
    Image conversion and optimization utility.

    Handles:
    - Format conversion (PNG, HEIC, etc. to JPEG)
    - PDF to image conversion
    - Resizing while maintaining aspect ratio
    - JPEG quality optimization
    - EXIF orientation correction
    """

    def __init__(
        self,
        target_max_px: int = IMAGE_LIMITS.TARGET_MAX_PX,
        jpeg_qualities: Tuple[int, ...] = IMAGE_LIMITS.JPEG_QUALITIES,
        pdf_dpi: int = IMAGE_LIMITS.PDF_DPI,
    ):
        """
        Initialize image converter.

        Args:
            target_max_px: Max dimension for output images
            jpeg_qualities: Quality levels to try for JPEG compression
            pdf_dpi: DPI for PDF rasterization
        """
        self.target_max_px = target_max_px
        self.jpeg_qualities = jpeg_qualities
        self.pdf_dpi = pdf_dpi

    def process(
        self,
        data: bytes,
        mime_type: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> bytes:
        """
        Process image data: convert, resize, and optimize.

        Args:
            data: Raw image bytes
            mime_type: MIME type of the image
            filename: Original filename (for format detection)

        Returns:
            Processed JPEG bytes

        Raises:
            ImageProcessingError: If processing fails
        """
        try:
            # Handle PDF files
            if self._is_pdf(mime_type, filename):
                data = self.convert_pdf_to_image(data)

            # Validate image
            self.validate(data)

            # Open and process image
            with Image.open(io.BytesIO(data)) as img:
                # Apply EXIF orientation
                img = self._apply_exif_orientation(img)

                # Convert to RGB if necessary
                if img.mode in ("RGBA", "P", "LA"):
                    img = self._convert_to_rgb(img)
                elif img.mode != "RGB":
                    img = img.convert("RGB")

                # Resize if needed
                img = self._resize(img)

                # Optimize and convert to JPEG
                return self._optimize_jpeg(img)

        except UnidentifiedImageError as e:
            raise ImageProcessingError(f"Cannot identify image format: {e}") from e
        except Exception as e:
            raise ImageProcessingError(f"Image processing failed: {e}") from e

    def validate(self, data: bytes) -> None:
        """
        Validate image data.

        Args:
            data: Image bytes to validate

        Raises:
            ImageProcessingError: If validation fails
        """
        if len(data) < IMAGE_LIMITS.MIN_FILE_SIZE_BYTES:
            raise ImageProcessingError("Image file too small")

        if len(data) > IMAGE_LIMITS.MAX_FILE_SIZE_BYTES:
            raise ImageProcessingError("Image file too large")

        try:
            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size

                if width < IMAGE_LIMITS.MIN_DIMENSION_PX or height < IMAGE_LIMITS.MIN_DIMENSION_PX:
                    raise ImageProcessingError(
                        f"Image dimensions too small: {width}x{height} "
                        f"(min: {IMAGE_LIMITS.MIN_DIMENSION_PX})"
                    )

                if width > IMAGE_LIMITS.MAX_DIMENSION_PX or height > IMAGE_LIMITS.MAX_DIMENSION_PX:
                    raise ImageProcessingError(
                        f"Image dimensions too large: {width}x{height} "
                        f"(max: {IMAGE_LIMITS.MAX_DIMENSION_PX})"
                    )

        except UnidentifiedImageError as e:
            raise ImageProcessingError(f"Cannot identify image format: {e}") from e

    def convert_pdf_to_image(self, data: bytes) -> bytes:
        """
        Convert first page of PDF to image.

        Args:
            data: PDF file bytes

        Returns:
            PNG image bytes of first page
        """
        try:
            doc = fitz.open(stream=data, filetype="pdf")

            if doc.page_count == 0:
                raise ImageProcessingError("PDF has no pages")

            page = doc[0]

            # Render at specified DPI
            mat = fitz.Matrix(self.pdf_dpi / 72, self.pdf_dpi / 72)
            pix = page.get_pixmap(matrix=mat)

            # Convert to PNG bytes
            return pix.tobytes("png")

        except Exception as e:
            raise ImageProcessingError(f"PDF conversion failed: {e}") from e

    def _is_pdf(self, mime_type: Optional[str], filename: Optional[str]) -> bool:
        """Check if content is a PDF."""
        if mime_type and "pdf" in mime_type.lower():
            return True
        if filename and filename.lower().endswith(".pdf"):
            return True
        return False

    def _apply_exif_orientation(self, img: Image.Image) -> Image.Image:
        """Apply EXIF orientation to correct image rotation."""
        try:
            return ImageOps.exif_transpose(img)
        except Exception as e:
            log.debug(f"EXIF orientation handling failed: {e}")
            return img

    def _convert_to_rgb(self, img: Image.Image) -> Image.Image:
        """Convert image with transparency to RGB with white background."""
        if img.mode == "RGBA":
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])  # Use alpha channel as mask
            return background
        elif img.mode == "P":
            return img.convert("RGBA").convert("RGB")
        elif img.mode == "LA":
            return img.convert("RGBA").convert("RGB")
        return img.convert("RGB")

    def _resize(self, img: Image.Image) -> Image.Image:
        """Resize image maintaining aspect ratio."""
        width, height = img.size

        if width <= self.target_max_px and height <= self.target_max_px:
            return img

        # Calculate new dimensions
        if width > height:
            new_width = self.target_max_px
            new_height = int(height * (self.target_max_px / width))
        else:
            new_height = self.target_max_px
            new_width = int(width * (self.target_max_px / height))

        return img.resize((new_width, new_height), Image.Resampling.LANCZOS)

    def _optimize_jpeg(self, img: Image.Image) -> bytes:
        """
        Optimize image as JPEG, trying different quality levels.

        Returns the smallest file that looks acceptable.
        """
        best_data = None

        for quality in self.jpeg_qualities:
            buf = io.BytesIO()
            img.save(
                buf,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            data = buf.getvalue()

            if best_data is None:
                best_data = data
            elif len(data) < len(best_data):
                best_data = data

        return best_data


def optimize_image(
    data: bytes,
    mime_type: Optional[str] = None,
    filename: Optional[str] = None,
) -> bytes:
    """
    Convenience function for image optimization.

    Args:
        data: Raw image bytes
        mime_type: MIME type
        filename: Original filename

    Returns:
        Optimized JPEG bytes
    """
    converter = ImageConverter()
    return converter.process(data, mime_type, filename)


def convert_pdf_to_image(data: bytes) -> bytes:
    """
    Convenience function for PDF to image conversion.

    Args:
        data: PDF bytes

    Returns:
        PNG image bytes
    """
    converter = ImageConverter()
    return converter.convert_pdf_to_image(data)


def get_mime_type(filename: str) -> Optional[str]:
    """
    Get MIME type from filename extension.

    Args:
        filename: Filename with extension

    Returns:
        MIME type or None if unknown
    """
    ext = Path(filename).suffix.lower()
    return SUPPORTED_FORMATS.EXTENSIONS.get(ext)


def is_supported_format(mime_type: Optional[str], filename: Optional[str] = None) -> bool:
    """
    Check if a file format is supported.

    Args:
        mime_type: MIME type
        filename: Filename for extension-based detection

    Returns:
        True if format is supported
    """
    if mime_type and mime_type.lower() in SUPPORTED_FORMATS.ALL:
        return True

    if filename:
        detected_mime = get_mime_type(filename)
        if detected_mime and detected_mime in SUPPORTED_FORMATS.ALL:
            return True

    return False


class TempFileManager:
    """
    Manages temporary files with automatic cleanup.

    Example:
        with TempFileManager() as tfm:
            path = tfm.create_temp_file(data, "image.jpg")
            # Use path...
        # Files automatically cleaned up
    """

    def __init__(self, prefix: str = "i2c_"):
        self.prefix = prefix
        self.temp_dir: Optional[str] = None
        self.files: List[str] = []

    def __enter__(self):
        self.temp_dir = tempfile.mkdtemp(prefix=self.prefix)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False

    def create_temp_file(
        self,
        data: bytes,
        filename: str,
    ) -> str:
        """
        Create a temporary file with the given data.

        SECURITY: Sanitizes filename to prevent path traversal. Uses only the
        basename and validates the final path stays under temp_dir (BATR-aligned).

        Args:
            data: File content
            filename: Desired filename (may come from attachment.name)

        Returns:
            Path to created file

        Raises:
            ValueError: If filename would escape temp directory (path traversal)
        """
        if self.temp_dir is None:
            raise RuntimeError("TempFileManager not initialized (use context manager)")

        # SECURITY: Use only basename to prevent path traversal (e.g. ../../../etc/passwd)
        safe_name = Path(filename).name
        if not safe_name or safe_name in (".", ".."):
            safe_name = "file"

        base, ext = os.path.splitext(safe_name)
        if not base:
            base = "file"
        unique_filename = f"{base}_{uuid.uuid4().hex[:8]}{ext}"
        path = os.path.join(self.temp_dir, unique_filename)

        # SECURITY: Validate resolved path stays under temp_dir (BATR pattern)
        full_path = Path(path).resolve()
        temp_base = Path(self.temp_dir).resolve()
        try:
            full_path.relative_to(temp_base)
        except ValueError:
            raise ValueError(
                f"Path traversal blocked: filename would escape temp directory"
            ) from None

        with open(path, "wb") as f:
            f.write(data)

        self.files.append(path)
        return path

    def cleanup(self) -> None:
        """Remove all temporary files and directory."""
        if self.temp_dir is None:
            return

        import shutil
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            log.debug(f"Cleaned up temp directory: {self.temp_dir}")
        except Exception as e:
            log.warning(f"Failed to cleanup temp directory {self.temp_dir}: {e}")
        finally:
            self.temp_dir = None
            self.files.clear()
