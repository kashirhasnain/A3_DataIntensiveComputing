#!/usr/bin/env python3
"""
zip_lambda.py -- Windows-compatible zip helper for Lambda packaging.
Called by run.sh instead of the 'zip' command (not available on Windows Git Bash).

Usage:
    python /path/to/zip_lambda.py create <output.zip> <file>
    python /path/to/zip_lambda.py add    <output.zip> <directory>
"""
import os
import sys
import zipfile

SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
SKIP_EXTS = {".pyc", ".pyo"}


def cmd_create(zip_path: str, *files: str) -> None:
    """Create a new zip file containing the listed files at the root level."""
    # Resolve paths relative to CWD (important: script may be called from a subdir)
    zip_path = os.path.abspath(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            f = os.path.abspath(f)
            arcname = os.path.basename(f)
            zf.write(f, arcname)
            print(f"  added: {arcname}")
    print(f"  => created {zip_path}")


def cmd_add(zip_path: str, directory: str) -> None:
    """Add all files from a directory (recursively) into an existing zip at root level."""
    zip_path  = os.path.abspath(zip_path)
    directory = os.path.abspath(directory)

    if not os.path.isdir(directory):
        print(f"  WARNING: directory not found, skipping: {directory}")
        return

    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
        file_count = 0
        for root, dirs, files in os.walk(directory):
            # Skip unneeded directories to keep the zip small
            dirs[:] = [
                d for d in dirs
                if d not in SKIP_DIRS and not d.endswith(".dist-info")
            ]
            for filename in files:
                if os.path.splitext(filename)[1] in SKIP_EXTS:
                    continue
                filepath = os.path.join(root, filename)
                # arcname is relative to the directory root (no 'package/' prefix)
                arcname = os.path.relpath(filepath, directory)
                zf.write(filepath, arcname)
                file_count += 1
    print(f"  => added {file_count} files from {directory} into {zip_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    command  = sys.argv[1]
    zip_arg  = sys.argv[2]

    if command == "create":
        cmd_create(zip_arg, *sys.argv[3:])
    elif command == "add":
        directory = sys.argv[3] if len(sys.argv) > 3 else "."
        cmd_add(zip_arg, directory)
    else:
        print(f"Unknown command: {command!r}. Use 'create' or 'add'.")
        sys.exit(1)
