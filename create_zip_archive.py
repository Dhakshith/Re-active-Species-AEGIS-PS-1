#!/usr/bin/env python3
"""
Archive creator for aegis-ps01.
Packages the complete working solution into a clean .zip file,
preserving Unix permissions and excluding virtualenvs, caches, and bytecode.
"""

import os
import stat
import zipfile
import shutil

SOURCE_DIR = "/home/ubuntu/aegis-ps01"
OUTPUT_PATHS = [
    "/home/ubuntu/aegis-ps01.zip",
    "/home/ubuntu/aegis-ps01/aegis-ps01.zip"
]

EXCLUDE_DIRS = {
    "venv",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ipynb_checkpoints"
}

EXCLUDE_EXTS = {
    ".pyc",
    ".pyo",
    ".zip"
}

def create_archive():
    temp_zip = "/tmp/aegis_source_temp.zip"
    if os.path.exists(temp_zip):
        os.remove(temp_zip)

    file_count = 0
    total_bytes = 0

    print(f"Packaging source code from {SOURCE_DIR}...")
    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(SOURCE_DIR):
            # Filter directories in-place to avoid descending into excluded dirs
            dirs[:] = [
                d for d in dirs 
                if d not in EXCLUDE_DIRS and not d.startswith(".venv")
            ]
            dirs.sort()

            # Record directory entries
            rel_dir = os.path.relpath(root, os.path.dirname(SOURCE_DIR))
            if rel_dir != ".":
                dir_info = zipfile.ZipInfo(rel_dir + "/")
                dir_info.external_attr = 0o40755 << 16  # directory permissions
                z.writestr(dir_info, "")

            for f in sorted(files):
                # Skip excluded extensions
                _, ext = os.path.splitext(f)
                if ext in EXCLUDE_EXTS:
                    continue
                
                full_path = os.path.join(root, f)
                arcname = os.path.relpath(full_path, os.path.dirname(SOURCE_DIR))
                
                file_stat = os.stat(full_path)
                info = zipfile.ZipInfo.from_file(full_path, arcname)
                # Preserve file mode/permissions
                info.external_attr = (file_stat.st_mode & 0xFFFF) << 16
                
                with open(full_path, "rb") as fp:
                    data = fp.read()
                    z.writestr(info, data)
                
                file_count += 1
                total_bytes += len(data)

    compressed_size = os.path.getsize(temp_zip)
    print(f"Packaged {file_count} files ({total_bytes / (1024*1024):.2f} MB uncompressed, {compressed_size / (1024*1024):.2f} MB compressed).")

    # Copy to all target destinations
    for out_path in OUTPUT_PATHS:
        shutil.copy2(temp_zip, out_path)
        print(f"Created archive: {out_path} ({os.path.getsize(out_path):,} bytes)")

    os.remove(temp_zip)
    print("Archive creation complete!")

if __name__ == "__main__":
    create_archive()
