import os
import sys
import shutil
import subprocess
import json
from pathlib import Path


# Get the script's directory as the base path
BASE_DIR = Path(__file__).resolve().parent

# Virtual environment paths
VENV_PATH   = BASE_DIR / "libraries" / "front_adder"
VENV_PYTHON = VENV_PATH / "Scripts" / "python.exe"


def is_venv_active():
    return sys.prefix == str(VENV_PATH)


if not is_venv_active():
    if not VENV_PYTHON.exists():
        print(f"❌ Virtual environment not found at: {VENV_PATH}")
        sys.exit(1)
    print(f"🔄 Activating virtual environment: {VENV_PATH}")
    result = subprocess.run([str(VENV_PYTHON), __file__] + sys.argv[1:])
    sys.exit(result.returncode)

print("✅ Running in virtual environment")

import time
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing
import cv2

# ======================
# Configuration
# ======================
NUM_WORKERS = 4

video_folder  = BASE_DIR / "Videos" / "Subtitled"
output_folder = BASE_DIR / "Videos" / "Sub with front"
image_path    = BASE_DIR / "front.png"

output_folder.mkdir(parents=True, exist_ok=True)

print(f"🚀 Parallel workers: {NUM_WORKERS}")

# ── CHROMA VIGNETTE CONFIG ────────────────────────────────────────────────────
# VIGNETTE_ANGLE: lower = subtler effect, higher = more darkening at corners.
# PI/5 (≈ 36°) matches the Python vignette (inner_radius=0.5, outer_radius=1.0).
VIGNETTE_ANGLE = "PI/5"
# ─────────────────────────────────────────────────────────────────────────────


def get_video_info(path: str) -> dict:
    """Return width, height for a video file via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", str(path)],
        capture_output=True, text=True
    )
    info = json.loads(result.stdout)
    vs   = next(s for s in info["streams"] if s["codec_type"] == "video")
    return {"width": vs["width"], "height": vs["height"]}


# ======================
# 🚀 MAIN WORKER FUNCTION (runs in separate process)
# ======================
def process_single_video_front(args):
    """
    Process a single video — overlay front.png and apply vignette.

    ── OPTIMISATION ────────────────────────────────────────────────────────────
    The original code applied vignette via composite.fl_image(apply_vignette_wrapper)
    which routed every decoded frame through Python (NumPy mask multiply per frame).
    At 30 fps the RTX 5060 Ti sat idle while CPython did the math.

    Replacement: a single FFmpeg call that performs
      1. Scale overlay image to video dimensions
      2. Composite overlay on video  (overlay=format=auto preserves alpha)
      3. Apply vignette filter        (pure C, GPU-assisted decode/encode)
      4. GPU encode output            (h264_nvenc, no Python callback)
    All three operations happen in ONE encode pass, zero Python per-frame work.

    For female clips the overlay is horizontally flipped.  We pre-flip it with
    OpenCV (cheap, one-shot PNG write) and pass the flipped PNG to FFmpeg.
    ────────────────────────────────────────────────────────────────────────────
    """
    filename, video_folder_str, output_folder_str, image_path_str, worker_id = args

    try:
        start_time = time.time()

        source_path   = Path(video_folder_str)  / filename
        dest_path     = Path(output_folder_str) / filename
        base_image    = Path(image_path_str)
        cleaned_name  = filename.strip().lower()

        # Chapter videos: copy as-is (no overlay needed)
        if "chapter" in cleaned_name:
            shutil.copy2(source_path, dest_path)
            return (filename, True, "Copied (chapter)", 0)

        # Get video dimensions for scaling the overlay
        try:
            info = get_video_info(str(source_path))
            w, h = info["width"], info["height"]
        except Exception:
            # Fallback: try MoviePy
            from moviepy.editor import VideoFileClip
            with VideoFileClip(str(source_path)) as clip:
                w, h = clip.size

        # For female clips: flip overlay image horizontally
        is_female = "female" in cleaned_name
        if is_female:
            img          = cv2.imread(str(base_image), cv2.IMREAD_UNCHANGED)
            flipped_img  = cv2.flip(img, 1)
            temp_overlay = Path(output_folder_str) / f"_tmp_front_{Path(filename).stem}.png"
            cv2.imwrite(str(temp_overlay), flipped_img)
            overlay_path = temp_overlay
        else:
            overlay_path = base_image
            temp_overlay = None

        # ── Single FFmpeg call: overlay + vignette + GPU encode ───────────────
        # [1:v]scale={w}:{h}   → resize overlay to match video dimensions
        # overlay=format=auto  → handles RGBA PNG transparency correctly
        # vignette             → C-native darkening at corners (no Python)
        # -c:a copy            → audio stream-copied, no re-encode
        # ─────────────────────────────────────────────────────────────────────
        filter_complex = (
            f"[1:v]scale={w}:{h}[ovr];"
            f"[0:v][ovr]overlay=0:0:format=auto[comp];"
            f"[comp]vignette=angle={VIGNETTE_ANGLE}[out]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-hwaccel", "cuda",                    # NVDEC decode offload
            "-i", str(source_path),
            "-i", str(overlay_path),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "0:a?",          # audio pass-through (optional stream)
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-rc", "vbr", "-cq", "23",
            "-b:v", "5M", "-maxrate", "8M",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            str(dest_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            # Fallback: CPU libx264 (temp overlay PNG still exists at this point)
            cmd_cpu_clean = [
                "ffmpeg", "-y",
                "-i", str(source_path),
                "-i", str(overlay_path),
                "-filter_complex", filter_complex,
                "-map", "[out]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "23", "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                str(dest_path)
            ]
            result2 = subprocess.run(cmd_cpu_clean, capture_output=True, text=True)
            if result2.returncode != 0:
                raise RuntimeError(f"FFmpeg failed (GPU + CPU):\n{result.stderr}\n{result2.stderr}")

        # Clean up temp overlay PNG (after all FFmpeg attempts complete).
        # ── On Windows, NVDEC doesn't always release file handles synchronously
        # when the subprocess exits.  Retry with a short sleep rather than crashing
        # the worker — the file will be overwritten on the next female clip anyway.
        if temp_overlay and temp_overlay.exists():
            for _attempt in range(10):
                try:
                    temp_overlay.unlink()
                    break
                except PermissionError:
                    time.sleep(0.15)  # wait for Windows to release the handle

        elapsed = time.time() - start_time
        print(f"[Worker {worker_id}] ✅ Completed: {filename} ({elapsed:.1f}s)")
        return (filename, True, "Success", elapsed)

    except Exception as e:
        print(f"[Worker {worker_id}] ❌ Error: {filename} - {str(e)}")
        import traceback; traceback.print_exc()
        return (filename, False, str(e), 0)


# ======================
# Main parallel processing
# ======================
if __name__ == '__main__':
    multiprocessing.freeze_support()

    print("=" * 60)
    print("🚀 PARALLEL FRONT IMAGE OVERLAY (FFmpeg native vignette)")
    print(f"📂 Input:  {video_folder}")
    print(f"📂 Output: {output_folder}")
    print(f"🖼️  Front image: {image_path}")
    print(f"⚡ Workers: {NUM_WORKERS}")
    print("=" * 60)

    video_files = [f for f in os.listdir(video_folder) if f.lower().endswith('.mp4')]
    if not video_files:
        print("❌ No video files found!")
        sys.exit(1)

    print(f"\n📹 Found {len(video_files)} videos to process")

    jobs = [
        (video_file, str(video_folder), str(output_folder),
         str(image_path), i % NUM_WORKERS)
        for i, video_file in enumerate(video_files)
    ]

    total_start           = time.time()
    success_count         = 0
    failure_count         = 0
    total_processing_time = 0
    copied_count          = 0

    print(f"\n🚀 Starting parallel processing with {NUM_WORKERS} workers...\n")

    with ProcessPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {executor.submit(process_single_video_front, job): job[0] for job in jobs}
        for future in as_completed(futures):
            video_file = futures[future]
            try:
                filename, success, message, elapsed = future.result()
                if success:
                    success_count += 1
                    total_processing_time += elapsed
                    if "Copied" in message:
                        copied_count += 1
                else:
                    failure_count += 1
                    print(f"❌ Failed: {filename} - {message}")
            except Exception as e:
                failure_count += 1
                print(f"❌ Exception: {video_file} - {str(e)}")

    total_elapsed = time.time() - total_start

    print("\n" + "=" * 60)
    print("🎉 PARALLEL PROCESSING COMPLETE!")
    print(f"📊 Statistics:")
    print(f"   • Total videos:          {len(video_files)}")
    print(f"   • Processed (FFmpeg):    {success_count - copied_count}")
    print(f"   • Copied (chapters):     {copied_count}")
    print(f"   • Failed:                {failure_count}")
    print(f"   • Wall time:             {total_elapsed:.2f}s ({total_elapsed/60:.2f}m)")
    if success_count - copied_count > 0:
        print(f"   • Avg per video:         "
              f"{total_processing_time/(success_count-copied_count):.2f}s")
        print(f"   • Speedup vs sequential: "
              f"{total_processing_time/total_elapsed:.2f}x")