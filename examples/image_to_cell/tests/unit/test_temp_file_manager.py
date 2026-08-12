"""
Unit tests for TempFileManager path sanitization (SH-221 classification).

Implementation uses ``Path(filename).name`` so traversal segments never reach
the filesystem; they are reduced to a basename under ``temp_dir``. Tests pin
that sanitize-and-accept behaviour (not a raise).
"""

from pathlib import Path

from src.utils.image_utils import TempFileManager


def test_create_temp_file_normal_filename():
    with TempFileManager() as tfm:
        path = tfm.create_temp_file(b"test data", "image.jpg")
        assert path
        assert "image_" in path
        assert path.endswith(".jpg")
        assert Path(path).resolve().is_relative_to(Path(tfm.temp_dir).resolve())


def test_create_temp_file_unix_traversal_uses_basename_only():
    with TempFileManager() as tfm:
        path = tfm.create_temp_file(b"malicious", "../../../etc/passwd.jpg")
        resolved = Path(path).resolve()
        assert resolved.is_relative_to(Path(tfm.temp_dir).resolve())
        assert "passwd_" in path
        assert ".." not in path


def test_create_temp_file_windows_traversal_uses_basename_only():
    with TempFileManager() as tfm:
        path = tfm.create_temp_file(
            b"malicious", "..\\..\\..\\windows\\system32\\config"
        )
        resolved = Path(path).resolve()
        assert resolved.is_relative_to(Path(tfm.temp_dir).resolve())
        assert "config_" in path
        assert ".." not in path


def test_create_temp_file_basename_extracted():
    with TempFileManager() as tfm:
        path = tfm.create_temp_file(b"ok", "subdir/image.jpg")
        assert "image_" in path
        assert "subdir" not in path
