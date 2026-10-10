"""
clean_pipeline.py
Wipes all pipeline outputs before a fresh run.
Safe to run from any working directory — paths are always relative to this file.
"""

import os
import subprocess
import sys
from pathlib import Path

# ==================== VIRTUAL ENV AUTO-ACTIVATION ====================
SCRIPT_DIR = Path(__file__).parent.resolve()
VENV_PATH = SCRIPT_DIR / "libraries" / "word_to_audio"

venv_activated = "word_to_audio" in sys.prefix or "VENV_ACTIVATED" in os.environ

if not venv_activated:
    print("🔄 Activating virtual environment...")
    os.environ["VENV_ACTIVATED"] = "1"

    if os.name == 'nt':
        venv_python = VENV_PATH / "Scripts" / "python.exe"
    else:
        venv_python = VENV_PATH / "bin" / "python"

    if venv_python.exists():
        print(f"✓ Using Python: {venv_python}")
        subprocess.call([str(venv_python), str(Path(__file__).resolve())])
    else:
        print(f"❌ Virtual environment not found at: {VENV_PATH}")
        print(f"❌ Expected Python at: {venv_python}")
    sys.exit()
# =====================================================================

try:
    from docx import Document
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

# ─────────────────────────────────────────
#  Targets
# ─────────────────────────────────────────
BASE = SCRIPT_DIR

AUDIO_DIRS = [
    BASE / "Audio" / "output_audio" / "combined",
    BASE / "Audio" / "output_audio" / "enhanced",
    BASE / "Audio" / "output_audio" / "final",
    BASE / "Audio" / "output_audio" / "raw",
]
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".opus"}

SRT_DIRS = [
    BASE / "SRT",
]

VIDEO_DIRS = [
    BASE / "Videos",
    BASE / "Videos" / "Subtitled",
    BASE / "Videos" / "Sub with front",
]
VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".webm", ".ts"}

IMAGE_DIRS = [
    BASE / "thumbnail" / "output_thumbnail",
]
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

DOCX_FILE = BASE / "pod1.docx"

# ─────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────
def delete_files(directory: Path, extensions: set):
    if not directory.exists():
        print(f"  [SKIP]  {directory}  —  directory not found")
        return
    files = [f for f in directory.iterdir() if f.is_file() and f.suffix.lower() in extensions]
    if not files:
        print(f"  [EMPTY] {directory}  —  nothing to delete")
        return
    deleted, skipped = 0, 0
    for f in files:
        try:
            f.unlink()
            print(f"  [DEL]   {f.name}")
            deleted += 1
        except PermissionError:
            print(f"  [LOCK]  {f.name}  —  in use or read-only, skipped")
            skipped += 1
        except OSError as e:
            print(f"  [ERR]   {f.name}  —  {e}")
            skipped += 1
    summary = f"  ✓ {deleted} file(s) removed from {directory}"
    if skipped:
        summary += f"  ({skipped} skipped)"
    print(summary)


def empty_docx(path: Path):
    if not path.exists():
        print(f"  [SKIP]  {path}  —  file not found")
        return
    if not HAS_DOCX:
        print(f"  [WARN]  python-docx not installed  —  run: pip install python-docx")
        return
    doc = Document()
    doc.save(path)
    print(f"  ✓ {path} cleared")


# ─────────────────────────────────────────
#  Run
# ─────────────────────────────────────────
print("\n╔══════════════════════════╗")
print("║    Pipeline Cleaner      ║")
print("╚══════════════════════════╝\n")

print("► Audio")
for d in AUDIO_DIRS:
    delete_files(d, AUDIO_EXTS)

print("\n► SRT")
for d in SRT_DIRS:
    delete_files(d, {".srt"})

print("\n► Video")
for d in VIDEO_DIRS:
    delete_files(d, VIDEO_EXTS)

print("\n► Images")
for d in IMAGE_DIRS:
    delete_files(d, IMAGE_EXTS)

print("\n► Docx reset")
empty_docx(DOCX_FILE)

print("\n══════════════ Done ══════════════\n")