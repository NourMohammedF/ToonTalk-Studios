import os
import sys
import subprocess
import hashlib
from pathlib import Path
import shutil

# ======================
# Auto-activate Virtual Environment
# ======================
def activate_virtual_env():
    script_dir = Path(__file__).parent.absolute()
    venv_path  = script_dir / "libraries" / "Subtitle_Animator"

    if hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
        print("✅ Virtual environment already activated")
        return

    if sys.platform == "win32":
        activate_script = venv_path / "Scripts" / "activate_this.py"
        python_exe      = venv_path / "Scripts" / "python.exe"
    else:
        activate_script = venv_path / "bin" / "activate_this.py"
        python_exe      = venv_path / "bin" / "python"

    if activate_script.exists():
        exec(open(str(activate_script)).read(), {'__file__': str(activate_script)})
        print(f"✅ Virtual environment activated from: {venv_path}")
    elif python_exe.exists():
        print("🔄 Restarting script with virtual environment Python...")
        subprocess.call([str(python_exe), __file__] + sys.argv[1:])
        sys.exit()
    else:
        print(f"⚠️ Virtual environment not found at: {venv_path}")
        print("Continuing with system Python...")


activate_virtual_env()

from moviepy.config import change_settings

SCRIPT_DIR         = Path(__file__).parent.absolute()
IMAGEMAGICK_BINARY = str(
    SCRIPT_DIR / "libraries" / "Subtitle_Animator"
    / "ImageMagick-7.1.1-Q16-HDRI" / "magick.exe"
)
change_settings({"IMAGEMAGICK_BINARY": IMAGEMAGICK_BINARY})

import math
import json
import numpy as np
import pysrt
from moviepy.editor import VideoFileClip, ImageClip, TextClip, CompositeVideoClip
import cv2
import time
import gc
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

# ======================
# Configuration
# ======================
NUM_WORKERS = 4

print(f"🔧 ImageMagick configured at: {IMAGEMAGICK_BINARY}")
print(f"🚀 Parallel workers: {NUM_WORKERS}")

# ── CHROMA KEY CONFIG ─────────────────────────────────────────────────────────
# CHROMA_COLOR: must match the green in green_background.jpg.
#   Common values:  0x00B400 (matte green)  |  0x00FF00 (pure green)
#   To find the exact value: open green_background.jpg in any image editor,
#   use the eyedropper on the background and copy the hex colour.
# CHROMA_SIMILARITY: how close a pixel must be to CHROMA_COLOR to be keyed out.
#   Raise if you see green fringes; lower if you see holes in the subject.
# CHROMA_BLEND: controls edge softness.  0.0 = hard edge, 0.15 = soft.
# ─────────────────────────────────────────────────────────────────────────────
CHROMA_COLOR      = "0afe00"
CHROMA_SIMILARITY = "0.2"
CHROMA_BLEND      = "0.1"


# ======================
# PRE-BAKE WORD PNGs (called once in main process before the pool starts)
# ======================
def prebake_word_pngs(srt_files: list, cache_dir: Path, imagemagick_binary: str) -> dict:
    """
    Render all unique words from every SRT file to PNG files in cache_dir.

    ── OPTIMISATION ────────────────────────────────────────────────────────────
    Problem B: The original code called TextClip(word, ...) inside each parallel
    worker, spawning a new magick.exe process per word.  A 100-word video
    launches ~100 subprocesses each taking 100–500 ms of overhead BEFORE any
    rendering — hundreds of wasted seconds across 4 workers.

    Fix: collect every unique word across ALL SRT files, render each word to a
    PNG once in the main process, then workers use ImageClip(path) which is a
    simple file read — zero subprocess spawning in the hot path.
    ────────────────────────────────────────────────────────────────────────────
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Collect unique words
    words = set()
    for srt_path in srt_files:
        try:
            subs = pysrt.open(str(srt_path), encoding='utf-8')
            for s in subs:
                text = s.text.strip()
                if text:
                    words.add(text)
        except Exception as e:
            print(f"⚠️  Could not read SRT {srt_path}: {e}")

    print(f"   Found {len(words)} unique words to render")

    word_png_map = {}
    rendered = 0
    skipped  = 0

    for word in words:
        # Use MD5 for the filename: avoids special characters and length limits
        word_hash = hashlib.md5(word.encode('utf-8')).hexdigest()
        out_path  = cache_dir / f"{word_hash}.png"
        word_png_map[word] = out_path

        if out_path.exists():
            skipped += 1
            continue

        safe_word = word.replace('%', '%%')
        
        # Render the word to a transparent-background PNG
        # Parameters match the original TextClip: Impact 75pt, white fill,
        # black stroke width 5, kerning 2, centred label
        cmd = [
            imagemagick_binary,
            "-background", "none",
            "-fill",        "white",
            "-stroke",      "black",
            "-strokewidth", "5",
            "-font",        "Impact",
            "-pointsize",   "75",
            "-kerning",     "2",
            "-gravity",     "center",
            f"label:{safe_word}",
            str(out_path)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                print(f"   ⚠️  ImageMagick failed for '{word}': {result.stderr[:100]}")
                word_png_map.pop(word, None)
                continue
        except Exception as e:
            print(f"   ⚠️  Exception rendering '{word}': {e}")
            word_png_map.pop(word, None)
            continue

        rendered += 1

    print(f"   ✅ Pre-baked {rendered} new PNGs, {skipped} from cache")
    return word_png_map


# ======================
# Local ffprobe helper — avoids a MoviePy load just to read width/height/duration
# ======================
def _get_clip_info(video_path: Path) -> tuple:
    """Return (width, height, duration) via ffprobe — zero MoviePy overhead."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", str(video_path)],
        capture_output=True, text=True
    )
    data     = json.loads(result.stdout)
    vs       = next(s for s in data["streams"] if s["codec_type"] == "video")
    duration = float(data["format"]["duration"])
    return vs["width"], vs["height"], duration


# ======================
# FFmpeg chroma key + grayscale + composite (replaces Python fl() callback)
# ======================

def has_audio(path: Path) -> bool:
    """True if the file contains at least one audio stream."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True
    )
    return bool(r.stdout.strip())

def chroma_key_ffmpeg(video_path: Path, bg_video_path: Path, temp_output: Path,
                      clip_duration: float, width: int, height: int,
                      flip_subject: bool) -> bool:
    """
    Perform chroma key + grayscale conversion + background composite via FFmpeg.

    ── OPTIMISATION ────────────────────────────────────────────────────────────
    Problem C: clip.fl(composite_frame) routes every decoded frame through Python.
    At 30 fps × clip duration × 4 workers that is thousands of Python function
    calls, each doing np.cvtColor + np.inRange + morphological ops + np.where.
    The GPU sits idle during all of this.

    Fix: one FFmpeg subprocess call with a filter_complex chain that:
      1. chromakey   → keys out the green background (CUDA-accelerated path
                       where available, otherwise fast C implementation)
      2. colorchannelmixer → converts keyed subject to grayscale (luma weights)
      3. lut         → doubles contrast  (matches cv2.convertScaleAbs alpha=2)
      4. hflip       → mirrors subject + background for female clips
      5. overlay     → composites subject over looped background
    All math runs in C (or CUDA) — zero Python per-frame calls.
    ────────────────────────────────────────────────────────────────────────────
    """
    # Grayscale coefficients (NTSC/sRGB luma weights), alpha channel preserved
    ccm = ("colorchannelmixer="
           "0.2126:0.7152:0.0722:0:"   # R output
           "0.2126:0.7152:0.0722:0:"   # G output
           "0.2126:0.7152:0.0722:0:"   # B output
           "0:0:0:1")                  # A output (pass-through)

    # Contrast × 2 via LUT (matches cv2.convertScaleAbs alpha=2, beta≈0)
    contrast_lut = "lut=r='min(val*2,255)':g='min(val*2,255)':b='min(val*2,255)':a='val'"

    d = f"{clip_duration:.3f}"

    if flip_subject:
        # Female: flip both subject and background horizontally
        fc = (
            f"[0:v]chromakey={CHROMA_COLOR}:{CHROMA_SIMILARITY}:{CHROMA_BLEND}[fg];"
            f"[fg]{ccm}[gray];"
            f"[gray]{contrast_lut}[adj];"
            f"[adj]hflip[fg_f];"
            f"[1:v]trim=0:{d},setpts=PTS-STARTPTS,scale={width}:{height},hflip[bg];"
            f"[bg][fg_f]overlay=format=auto[out]"
        )
    else:
        # Male: no flip
        fc = (
            f"[0:v]chromakey={CHROMA_COLOR}:{CHROMA_SIMILARITY}:{CHROMA_BLEND}[fg];"
            f"[fg]{ccm}[gray];"
            f"[gray]{contrast_lut}[adj];"
            f"[1:v]trim=0:{d},setpts=PTS-STARTPTS,scale={width}:{height}[bg];"
            f"[bg][adj]overlay=format=auto[out]"
        )

    # Extend filter_complex to mix audio from both foreground and background
    fg_audio = has_audio(video_path)
    bg_audio = has_audio(bg_video_path)

    if fg_audio and bg_audio:
        # Both have audio -> mix them
        fc_full = fc + ";[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        audio_map = ["-map", "[aout]"]
    elif fg_audio:
        # Only foreground has audio
        fc_full = fc
        audio_map = ["-map", "0:a"]
    elif bg_audio:
        # Only background has audio
        fc_full = fc
        audio_map = ["-map", "1:a"]
    else:
        fc_full = fc
        audio_map = []   # no audio at all

    cmd = [
        "ffmpeg", "-y",
        "-hwaccel", "cuda",
        "-i", str(video_path),
        "-stream_loop", "-1", "-i", str(bg_video_path),
        "-filter_complex", fc_full,
        "-map", "[out]",
        *audio_map,
        "-t", d,                  # needed: looped background is infinite
        "-c:v", "h264_nvenc", "-preset", "p4",
        "-rc", "vbr", "-cq", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(temp_output)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        print(f"⚠️  FFmpeg chroma key failed, stderr:\n{result.stderr[:400]}")
        return False
    return True


# ======================
# Subtitle animation (unchanged — MoviePy handles timing/rotation/scale)
# ======================
def animate_subtitle_fast(txt_clip, duration, start_pos=(1600, 900), end_pos=(600, 200),
                           start_rotation=70, end_rotation=0):
    """Optimised animation with simplified easing"""
    def ease_fast(t, d):
        p = t / d
        return 1 - (1 - p) ** 2

    def pos(t):
        p = ease_fast(t, duration)
        x = start_pos[0] + (end_pos[0] - start_pos[0]) * p
        y = start_pos[1] + (end_pos[1] - start_pos[1]) * p
        x += 2 * math.sin(6.28 * 2 * t)
        return (x, y)

    def rotation(t):
        p = ease_fast(t, duration)
        return start_rotation + (end_rotation - start_rotation) * p

    def scale(t):
        p = ease_fast(t, duration)
        return 0.2 + 0.5 * p

    def fade_mask(get_frame, t):
        opacity = 1 - ease_fast(t, duration)
        return get_frame(t) * opacity

    animated_clip = (
        txt_clip.resize(scale)
        .rotate(rotation, expand=True)
        .set_position(pos)
        .set_duration(duration)
    )
    animated_clip.mask = animated_clip.mask.fl(fade_mask)
    return animated_clip


# ======================
# GPU encoding helper
# ======================
def write_video_turbo(clip, output_path, gpu_id=0):
    """Ultra-fast GPU encoding"""
    try:
        clip.write_videofile(
            str(output_path),
            codec='h264_nvenc',
            audio_codec='aac',
            threads=4,
            preset='p4',
            ffmpeg_params=[
                '-rc', 'vbr', '-cq', '23',
                '-b:v', '5M', '-maxrate', '8M',
                '-profile:v', 'high',
                '-pix_fmt', 'yuv420p',
                '-gpu', str(gpu_id)
            ],
            logger=None,
            write_logfile=False
        )
        return True
    except Exception:
        # CPU fallback
        clip.write_videofile(
            str(output_path),
            codec="libx264",
            audio_codec="aac",
            threads=4,
            preset='veryfast',
            ffmpeg_params=['-crf', '23', '-pix_fmt', 'yuv420p'],
            logger=None,
            write_logfile=False
        )
        return False


# ======================
# 🚀 MAIN WORKER FUNCTION (runs in separate process)
# ======================
def process_single_video(args):
    """
    Process a single video — chroma key + background + animated subtitles.

    Changes vs original:
      • Pass 1 (chroma key + grayscale + flip + composite) → FFmpeg subprocess
        instead of clip.fl(composite_frame) Python callback.
      • Pass 2 (subtitle animation) → ImageClip from pre-baked PNG instead of
        TextClip which spawned a new magick.exe process per word.
    """
    video_file, input_folder, output_folder, srt_folder, bg_video_path, \
        worker_id, word_png_map_str = args

    try:
        video_path  = Path(input_folder)  / video_file
        output_path = Path(output_folder) / video_file
        start_time  = time.time()

        # Chapter videos: copy as-is
        if 'chapter' in video_file.lower():
            print(f"[Worker {worker_id}] 📋 Chapter: {video_file} → copying")
            shutil.copy2(str(video_path), str(output_path))
            try:
                time.sleep(0.2)
                if video_path.exists():
                    video_path.unlink()
            except Exception as e:
                print(f"[Worker {worker_id}] ⚠️ Could not delete original: {e}")
            elapsed = time.time() - start_time
            return (video_file, True, "Chapter - Skipped", elapsed)

        # Load SRT
        video_basename = video_path.stem
        srt_path       = Path(srt_folder) / f"{video_basename}.srt"
        if not srt_path.exists():
            return (video_file, False, "No subtitle file", 0)
        subtitles = pysrt.open(str(srt_path), encoding='utf-8')

        gender    = 'female' if 'female' in video_file.lower() else 'male'
        flip_flag = (gender == 'female')

        # ── PASS 1: Chroma key via FFmpeg (no Python per-frame callback) ──────
        temp_video = Path(input_folder) / f"temp_{worker_id}_{video_basename}.mp4"

        # ── OPTIMISATION: ffprobe replaces a VideoFileClip load just for
        # width/height/duration — one ffprobe call vs full MoviePy decode init.
        width, height, clip_duration = _get_clip_info(video_path)

        chroma_ok = chroma_key_ffmpeg(
            video_path, Path(bg_video_path), temp_video,
            clip_duration, width, height, flip_flag
        )

        if not chroma_ok:
            # Fallback: original Python chroma key
            print(f"[Worker {worker_id}] ⚠️  FFmpeg chroma key failed — using Python fallback")
            background_clip = VideoFileClip(str(bg_video_path))
            with VideoFileClip(str(video_path)) as clip:
                bg_loop = background_clip.loop(duration=clip.duration)

                def composite_frame(get_frame, t):
                    fg_frame = get_frame(t)
                    bg_frame = bg_loop.get_frame(t)
                    return _process_frame_fallback(fg_frame, bg_frame, flip_flag)

                new_clip = clip.fl(composite_frame)
                write_video_turbo(new_clip, temp_video, gpu_id=worker_id % 2)
            background_clip.close()

        # ── PASS 2: Subtitle animation using pre-baked ImageClips ─────────────
        video_clip     = VideoFileClip(str(temp_video))
        video_duration = video_clip.duration
        video_width    = video_clip.size[0]

        male_mouth_position = (820, 535)
        base_end_pos        = (550, 200)

        if gender == 'female':
            mouth_position = (video_width - male_mouth_position[0], male_mouth_position[1])
            end_pos        = (video_width - base_end_pos[0], base_end_pos[1])
            rot_start, rot_end = -70, 0
        else:
            mouth_position = male_mouth_position
            end_pos        = base_end_pos
            rot_start, rot_end = 70, 0

        subtitle_clips = []
        overlap_ratio  = -0.5
        fixed_duration = 1.5

        for i, sub in enumerate(subtitles):
            start          = sub.start.ordinal / 1000.0
            original_end   = sub.end.ordinal   / 1000.0

            if i > 0:
                prev_end         = subtitles[i-1].end.ordinal / 1000.0
                overlap_duration = (original_end - start) * overlap_ratio
                start            = max(start - overlap_duration, prev_end - overlap_duration)

            duration_sub = min(fixed_duration, video_duration - start) if start < video_duration else 0
            if duration_sub <= 0:
                continue

            word = sub.text.strip()

            # ── Use pre-baked PNG (no magick.exe subprocess) ──────────────────
            png_path = word_png_map_str.get(word)
            if png_path and Path(png_path).exists():
                txt_clip = ImageClip(png_path).set_duration(duration_sub)
            else:
                # Fallback: original TextClip (spawns ImageMagick)
                txt_clip = TextClip(
                    word, fontsize=75, font="Impact",
                    color="#ffffff", stroke_color="black", stroke_width=5,
                    method='label', align='center', kerning=2
                ).set_duration(duration_sub)

            animated_clip = animate_subtitle_fast(
                txt_clip, duration_sub,
                start_pos=mouth_position,
                end_pos=end_pos,
                start_rotation=rot_start,
                end_rotation=rot_end
            ).set_start(start)

            subtitle_clips.append(animated_clip)

        final_clip = CompositeVideoClip([video_clip] + subtitle_clips)
        write_video_turbo(final_clip, output_path, gpu_id=worker_id % 2)

        video_clip.close()
        final_clip.close()
        gc.collect()

        if temp_video.exists():
            temp_video.unlink()

        try:
            time.sleep(0.5)
            if video_path.exists():
                video_path.unlink()
        except Exception as e:
            print(f"[Worker {worker_id}] ⚠️ Could not delete original: {e}")

        elapsed = time.time() - start_time
        print(f"[Worker {worker_id}] ✅ Completed: {video_file} ({elapsed:.1f}s)")
        return (video_file, True, "Success", elapsed)

    except Exception as e:
        print(f"[Worker {worker_id}] ❌ Error: {video_file} - {str(e)}")
        import traceback; traceback.print_exc()
        return (video_file, False, str(e), 0)


def _process_frame_fallback(frame, background, flip_subject=False):
    """Original Python chroma key — used only when FFmpeg fails."""
    hsv         = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)
    lower_green = np.array([30, 40, 40],  dtype=np.uint8)
    upper_green = np.array([90, 255, 255], dtype=np.uint8)
    mask        = cv2.inRange(hsv, lower_green, upper_green)
    kernel      = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask        = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    subject_mask = mask == 0

    subject = np.zeros_like(frame)
    subject[subject_mask] = frame[subject_mask]
    gray_subject     = cv2.cvtColor(subject, cv2.COLOR_RGB2GRAY)
    gray_subject_rgb = cv2.cvtColor(gray_subject, cv2.COLOR_GRAY2RGB)
    adjusted_subject = cv2.convertScaleAbs(gray_subject_rgb, alpha=2, beta=1)

    if flip_subject:
        adjusted_subject = cv2.flip(adjusted_subject, 1)
        subject_mask     = cv2.flip(subject_mask.astype(np.uint8), 1).astype(bool)
        background       = cv2.flip(background, 1)

    bg_resized = cv2.resize(background, (frame.shape[1], frame.shape[0]),
                            interpolation=cv2.INTER_LINEAR)
    return np.where(subject_mask[..., np.newaxis], adjusted_subject, bg_resized)


# ======================
# Main parallel processing
# ======================
if __name__ == '__main__':
    multiprocessing.freeze_support()

    input_videos_folder  = SCRIPT_DIR / "Videos"
    output_videos_folder = SCRIPT_DIR / "Videos" / "Subtitled"
    srt_folder           = SCRIPT_DIR / "SRT"
    background_video_path = SCRIPT_DIR / "background_video.mp4"
    word_cache_dir        = SCRIPT_DIR / "libraries" / "Subtitle_Animator" / "word_cache"

    output_videos_folder.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("🚀 PARALLEL VIDEO PROCESSING (FFmpeg chroma key + pre-baked PNGs)")
    print(f"📂 Input:      {input_videos_folder}")
    print(f"📂 Output:     {output_videos_folder}")
    print(f"📂 SRT:        {srt_folder}")
    print(f"🎬 Background: {background_video_path}")
    print(f"⚡ Workers:    {NUM_WORKERS}")

    video_files = [f for f in os.listdir(input_videos_folder)
                   if f.lower().endswith(('.mp4', '.mov', '.avi', '.mkv'))]

    if not video_files:
        print("❌ No video files found!")
        sys.exit(1)

    print(f"\n📹 Found {len(video_files)} videos to process")

    # ── Pre-bake ALL unique words ONCE before spawning parallel workers ───────
    srt_files = list(srt_folder.glob("*.srt"))
    print(f"\n🔤 Pre-baking word PNGs ({len(srt_files)} SRT files)...")
    word_png_map = prebake_word_pngs(srt_files, word_cache_dir, IMAGEMAGICK_BINARY)
    print(f"✅ Word PNG cache ready: {len(word_png_map)} words\n")

    # Serialize to {word: str(path)} for subprocess pickling
    word_png_map_str = {w: str(p) for w, p in word_png_map.items()}

    jobs = [
        (video_file, str(input_videos_folder), str(output_videos_folder),
         str(srt_folder), str(background_video_path),
         i % NUM_WORKERS, word_png_map_str)
        for i, video_file in enumerate(video_files)
    ]

    total_start           = time.time()
    success_count         = 0
    failure_count         = 0
    total_processing_time = 0

    print(f"🚀 Starting parallel processing with {NUM_WORKERS} workers...\n")

    with ProcessPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = {executor.submit(process_single_video, job): job[0] for job in jobs}
        for future in as_completed(futures):
            video_file = futures[future]
            try:
                filename, success, message, elapsed = future.result()
                if success:
                    success_count         += 1
                    total_processing_time += elapsed
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
    print(f"   • Successful:            {success_count}")
    print(f"   • Failed:                {failure_count}")
    print(f"   • Wall time:             {total_elapsed:.2f}s ({total_elapsed/60:.2f}m)")
    print(f"   • Total processing time: {total_processing_time:.2f}s")
    if success_count > 0:
        print(f"   • Average per video:     {total_processing_time/success_count:.2f}s")
        print(f"   • Speedup vs sequential: {total_processing_time/total_elapsed:.2f}x")
        print(f"   • Efficiency:            "
              f"{(total_processing_time/(total_elapsed*NUM_WORKERS))*100:.1f}%")