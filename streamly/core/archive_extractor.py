"""
Safe Archive Extraction Engine
Supports .zip, .tar, .tar.gz, .tgz, .tar.bz2, .7z, and .rar with Zip-Slip path traversal protection.
"""

from __future__ import annotations

import os
import shutil
import zipfile
import tarfile
import logging

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".7z", ".rar")

def is_archive(filename: str) -> bool:
    name_lower = filename.lower()
    return any(name_lower.endswith(ext) for ext in SUPPORTED_EXTENSIONS)

def get_archive_base_name(filename: str) -> str:
    name = filename
    for ext in sorted(SUPPORTED_EXTENSIONS, key=len, reverse=True):
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
            break
    return name.strip() or "extracted"

def safe_extract_archive(archive_path: str, extract_to: str, delete_archive: bool = True) -> str:
    """
    Extracts archive file safely into target directory.
    Returns path to the extracted destination directory.
    """
    if not os.path.exists(archive_path):
        raise FileNotFoundError(f"Archive not found: {archive_path}")

    filename = os.path.basename(archive_path)
    base_folder_name = get_archive_base_name(filename)
    dest_dir = os.path.join(extract_to, base_folder_name)
    os.makedirs(dest_dir, exist_ok=True)

    dest_dir_real = os.path.realpath(dest_dir)
    filename_lower = filename.lower()

    log.info("Extracting archive %s -> %s", filename, dest_dir)

    try:
        if filename_lower.endswith(".zip"):
            with zipfile.ZipFile(archive_path, 'r') as zf:
                for member in zf.infolist():
                    # Prevent Zip-Slip directory traversal attack
                    target_file_path = os.path.realpath(os.path.join(dest_dir, member.filename))
                    if not target_file_path.startswith(dest_dir_real):
                        log.warning("Zip-Slip security attempt detected for member: %s. Skipping.", member.filename)
                        continue
                    zf.extract(member, dest_dir)

        elif filename_lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
            mode = "r:*"
            with tarfile.open(archive_path, mode) as tf:
                for member in tf.getmembers():
                    target_file_path = os.path.realpath(os.path.join(dest_dir, member.name))
                    if not target_file_path.startswith(dest_dir_real):
                        log.warning("Tar-Slip security attempt detected for member: %s. Skipping.", member.name)
                        continue
                    tf.extract(member, dest_dir)

        elif filename_lower.endswith(".7z"):
            try:
                import py7zr
                with py7zr.SevenZipFile(archive_path, mode='r') as szf:
                    szf.extractall(path=dest_dir)
            except ImportError:
                log.warning("py7zr not installed; trying 7z system cli")
                import subprocess
                subprocess.run(["7z", "x", archive_path, f"-o{dest_dir}", "-y"], check=True)

        elif filename_lower.endswith(".rar"):
            try:
                import rarfile
                with rarfile.RarFile(archive_path) as rf:
                    for member in rf.infolist():
                        target_file_path = os.path.realpath(os.path.join(dest_dir, member.filename))
                        if not target_file_path.startswith(dest_dir_real):
                            continue
                        rf.extract(member, dest_dir)
            except Exception as re:
                log.warning("rarfile extraction failed: %s; trying unrar system cli", re)
                import subprocess
                subprocess.run(["unrar", "x", "-o+", archive_path, dest_dir], check=True)

        else:
            raise ValueError(f"Unsupported archive format: {filename}")

    except Exception as extract_err:
        log.error("Archive extraction failed for %s: %s", archive_path, extract_err)
        # Rollback partial extraction
        if os.path.exists(dest_dir):
            shutil.rmtree(dest_dir, ignore_errors=True)
        raise extract_err

    if delete_archive and os.path.exists(archive_path):
        try:
            os.remove(archive_path)
            log.info("Removed original archive %s after successful extraction", archive_path)
        except Exception as e:
            log.debug("Could not remove archive container: %s", e)

    # Touch all extracted files to current ingestion time
    now = time.time()
    for root, dirs, files in os.walk(dest_dir):
        for f in files:
            try:
                os.utime(os.path.join(root, f), (now, now))
            except Exception:
                pass
        for d in dirs:
            try:
                os.utime(os.path.join(root, d), (now, now))
            except Exception:
                pass

    return dest_dir
