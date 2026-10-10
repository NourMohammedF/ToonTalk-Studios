import subprocess
import time
from pathlib import Path
from contextlib import contextmanager

# Get the script's directory as the base path
BASE_DIR = Path(__file__).resolve().parent

# Sentinel file: main.py touches this when Video_Maker finishes so that
# transcriber.py in --watch mode knows to do a final sweep and exit.
SENTINEL = BASE_DIR / "vm_done.flag"

# ── PIPELINE ──────────────────────────────────────────────────────────────────
#
# Stage                     | Script             | Notes
# ──────────────────────────┼────────────────────┼──────────────────────────────
# 1  Script generation      | generate_script.py | interactive (user input)
# 2  TTS audio              | word_to_audio.py   |
# 3a Adobe Express renders  | Video_Maker.py     | ─┐ run in PARALLEL
# 3b Transcription          | transcriber.py     | ─┘ transcriber watches for
#    (watch mode)           |  --watch           |    new .mp4 files as they land
# 4  Subtitle animation     | Subtitle_Animator.py|
# 5  Front overlay          | front_adder.py     |
# 6  Combine segments       | combiner.py        |
#
# ── WHY PARALLEL ──────────────────────────────────────────────────────────────
# Video_Maker writes one .mp4 per audio clip as Adobe Express finishes each
# browser tab.  Previously transcriber.py waited for ALL videos before starting.
# Now transcriber.py runs in --watch mode: it polls the Videos/ folder and
# transcribes each clip immediately after it appears.  By the time Video_Maker
# finishes its last clip, most (or all) transcriptions are already done.
# ─────────────────────────────────────────────────────────────────────────────

# Sequential stages (run one after another)
sequential_before = [
    'clean_pipeline.py',
    'trend_idea.py',
    'thumbnail_generator.py',
    'generate_script.py',
    'word_to_audio.py',
]

# Stages that run concurrently
parallel_stage = {
    'video_maker':  'Video_Maker_V2.py',
    'transcriber':  'transcriber.py',   # launched with --watch flag
}

# Sequential stages after the parallel pair finishes
sequential_after = [
    'Subtitle_Animator.py',
    'front_adder.py',
    'combiner.py',
    'upload_youtube.js',
    'shutdown.py',
]


@contextmanager
def timer(name):
    """Context manager for timing operations"""
    start = time.time()
    yield
    elapsed = time.time() - start
    print(f"⏱️  {name}: {elapsed:.2f}s ({elapsed/60:.2f}m)")


def run_file(filename: str, extra_args: list = None) -> bool:
    """Run a Python script and return True on success."""
    file_path = BASE_DIR / filename
    print(f"\n🚀 {filename}  ({file_path})")

    if not file_path.exists():
        print(f"   ⚠️  {filename} not found at {file_path} — skipping")
        return False

    interpreter = 'node' if file_path.suffix == '.js' else 'python'
    cmd = [interpreter, str(file_path)] + (extra_args or [])
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Error executing {filename}: {e}")
        answer = input("Continue with next file? (y/n): ").strip().lower()
        return answer == 'y'


print(f"📂 Base directory: {BASE_DIR}")
print("=" * 60)
print("🚀 Starting Optimised Video Processing Pipeline")

pipeline_start = time.time()

# ── Stage 1+2: Sequential ─────────────────────────────────────────────────────
for filename in sequential_before:
    with timer(f"Completed {filename}"):
        ok = run_file(filename)
    if not ok:
        print("Pipeline stopped.")
        raise SystemExit(1)

# ── Stage 3: Video_Maker ‖ transcriber (parallel) ────────────────────────────
print(f"\n{'='*60}")
print("⚡ PARALLEL STAGE: Video_Maker + transcriber")

# Remove any stale sentinel from a previous run
if SENTINEL.exists():
    SENTINEL.unlink()

vm_path  = BASE_DIR / parallel_stage['video_maker']
tr_path  = BASE_DIR / parallel_stage['transcriber']

parallel_start = time.time()

if not vm_path.exists():
    print(f"⚠️  {vm_path} not found — running sequential fallback")
    run_file(parallel_stage['video_maker'])
    run_file(parallel_stage['transcriber'])
else:
    # Launch both processes simultaneously
    vm_proc = subprocess.Popen(['python', str(vm_path)])
    tr_proc = None

    if tr_path.exists():
        print(f"   🎬 Video_Maker started  (PID {vm_proc.pid})")
        tr_proc = subprocess.Popen(['python', str(tr_path), '--watch'])
        print(f"   📝 transcriber started  (PID {tr_proc.pid}, --watch mode)")
    else:
        print(f"⚠️  transcriber.py not found — will run after Video_Maker finishes")

    # Wait for Video_Maker, then signal the transcriber
    vm_proc.wait()
    parallel_vm_time = time.time() - parallel_start

    if vm_proc.returncode != 0:
        print(f"❌ Video_Maker exited with code {vm_proc.returncode}")
        answer = input("Continue anyway? (y/n): ").strip().lower()
        if answer != 'y':
            if tr_proc:
                tr_proc.terminate()
            raise SystemExit(1)

    print(f"\n✅ Video_Maker finished in {parallel_vm_time:.1f}s")

    # Write sentinel so transcriber does its final sweep and exits
    SENTINEL.touch()
    print(f"🏁 Sentinel written → transcriber will finish remaining videos")

    if tr_proc:
        tr_proc.wait()
        if tr_proc.returncode not in (0, None):
            print(f"⚠️  transcriber exited with code {tr_proc.returncode}")
    else:
        # Transcriber was not launched in watch mode — run it now in batch mode
        run_file(parallel_stage['transcriber'])

    # Clean up sentinel
    if SENTINEL.exists():
        SENTINEL.unlink()

    parallel_total = time.time() - parallel_start
    print(f"⏱️  Parallel stage total: {parallel_total:.2f}s ({parallel_total/60:.2f}m)")

# ── Stage 4–6: Sequential ────────────────────────────────────────────────────
for filename in sequential_after:
    with timer(f"Completed {filename}"):
        ok = run_file(filename)
    if not ok:
        print("Pipeline stopped.")
        raise SystemExit(1)

total_time = time.time() - pipeline_start
print("\n" + "=" * 60)
print("🎉 Pipeline execution finished!")
print(f"⏱️  Total time: {total_time:.2f}s ({total_time/60:.2f}m)")
print("=" * 60)
