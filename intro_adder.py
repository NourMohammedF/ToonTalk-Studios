import os
import sys
import json
import argparse
import subprocess
from pathlib import Path

# ================= SETUP & VENV LOGIC =================
try:
    BASE_DIR = Path(__file__).resolve().parent
except NameError:
    BASE_DIR = Path.cwd()

VENV_PATH   = BASE_DIR / "libraries" / "combiner"
VENV_PYTHON = VENV_PATH / "Scripts" / "python.exe"


def is_venv_active():
    return sys.prefix == str(VENV_PATH) or os.path.normcase(sys.prefix) == os.path.normcase(str(VENV_PATH))


if not is_venv_active():
    if not VENV_PYTHON.exists():
        print(f"❌ Virtual environment not found at: {VENV_PATH}")
        sys.exit(1)
    print(f"🔄 Activating virtual environment: {VENV_PATH}")
    sys.stdout.flush()
    try:
        result = subprocess.run([str(VENV_PYTHON), __file__] + sys.argv[1:])
        sys.exit(result.returncode)
    except Exception as e:
        print(f"❌ Failed to launch subprocess: {e}")
        sys.exit(1)

# ================= CONFIG =================
DEFAULT_VIDEO = BASE_DIR / "Videos" / "Sub with front" / "001-male.mp4"
DEFAULT_INTRO = BASE_DIR / "thumbnail" / "intro.mp4"

NVENC_CODEC   = "h264_nvenc"   # switch to "hevc_nvenc" for smaller files if you don't need h264 compatibility
AUDIO_BITRATE = "320k"         # only used if audio has to be re-encoded (effectively lossless in practice)
GPU_CQ        = "16"           # NVENC constant-quality: visually lossless, standard-compliant, normal file size
                                # (true qp=0 lossless produces a non-standard bitstream most players/decoders
                                #  can't play back correctly — that's what caused the corrupted output)


# ================= PROBE HELPERS =================
def ffprobe_json(path: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ ffprobe failed on {path}:\n{result.stderr}")
        sys.exit(1)
    return json.loads(result.stdout)


def get_stream_info(probe: dict):
    video = next((s for s in probe["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in probe["streams"] if s["codec_type"] == "audio"), None)
    return video, audio


def video_params(v):
    if v is None:
        return None
    return (v.get("codec_name"), v.get("width"), v.get("height"), v.get("pix_fmt"), v.get("r_frame_rate"))


def audio_params(a):
    if a is None:
        return None
    return (a.get("codec_name"), a.get("sample_rate"), a.get("channels"))


def check_nvenc_available() -> bool:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return NVENC_CODEC in result.stdout


def escape_concat_path(path: Path) -> str:
    p = str(path.resolve()).replace("\\", "/")
    return p.replace("'", "'\\''")


# ================= PATH A: LOSSLESS STREAM COPY =================
def try_lossless_concat(intro: Path, video: Path, output: Path) -> bool:
    """If intro and video share identical codec/resolution/fps/audio params, just stream-copy concat.
    Zero re-encoding, zero quality loss, near-instant."""
    intro_v, intro_a = get_stream_info(ffprobe_json(intro))
    video_v, video_a = get_stream_info(ffprobe_json(video))

    if video_params(intro_v) != video_params(video_v):
        return False
    if audio_params(intro_a) != audio_params(video_a):
        return False

    list_file = output.parent / f"_concat_list_{os.getpid()}.txt"
    list_file.write_text(
        f"file '{escape_concat_path(intro)}'\nfile '{escape_concat_path(video)}'\n", encoding="utf-8"
    )

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy",
        str(output),
    ]
    print("⚡ Streams match — lossless stream-copy concat (no re-encode, instant).")
    result = subprocess.run(cmd)
    list_file.unlink(missing_ok=True)
    return result.returncode == 0 and output.exists() and output.stat().st_size > 0


# ================= PATH B: GPU LOSSLESS RE-ENCODE FALLBACK =================
def gpu_lossless_concat(intro: Path, video: Path, output: Path) -> bool:
    """Streams don't match, so a re-encode is unavoidable. Do it on GPU (NVENC) at qp=0
    (true lossless) so quality is still untouched, just rendered as fast as possible."""
    video_v, video_a = get_stream_info(ffprobe_json(video))
    _, intro_a = get_stream_info(ffprobe_json(intro))

    W   = video_v["width"]
    H   = video_v["height"]
    FPS = video_v.get("r_frame_rate", "30/1")
    SR  = video_a.get("sample_rate", "48000") if video_a else "48000"

    has_intro_audio = intro_a is not None
    has_video_audio = video_a is not None

    filters = [
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p[v0]",
        f"[1:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p[v1]",
    ]

    inputs = ["-i", str(intro), "-i", str(video)]
    next_idx = 2

    if has_intro_audio:
        filters.append(f"[0:a]aformat=sample_rates={SR}:channel_layouts=stereo[a0]")
    else:
        inputs += ["-f", "lavfi", "-t", "0.1", "-i", f"anullsrc=channel_layout=stereo:sample_rate={SR}"]
        filters.append(f"[{next_idx}:a]aformat=sample_rates={SR}:channel_layouts=stereo[a0]")
        next_idx += 1

    if has_video_audio:
        filters.append(f"[1:a]aformat=sample_rates={SR}:channel_layouts=stereo[a1]")
    else:
        inputs += ["-f", "lavfi", "-t", "0.1", "-i", f"anullsrc=channel_layout=stereo:sample_rate={SR}"]
        filters.append(f"[{next_idx}:a]aformat=sample_rates={SR}:channel_layouts=stereo[a1]")
        next_idx += 1

    filters.append("[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]")
    filter_complex = ";".join(filters)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", NVENC_CODEC, "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", GPU_CQ, "-b:v", "0",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        str(output),
    ]
    print(f"🎮 Streams differ — GPU re-encode ({NVENC_CODEC}, cq={GPU_CQ} high-quality VBR) to combine them.")
    result = subprocess.run(cmd)
    return result.returncode == 0 and output.exists() and output.stat().st_size > 0


# ================= MAIN COMBINE LOGIC =================
def combine(intro: Path, video: Path):
    if not intro.exists():
        print(f"❌ Intro not found: {intro}")
        sys.exit(1)
    if not video.exists():
        print(f"❌ Video not found: {video}")
        sys.exit(1)

    output = video.with_name(video.stem + "_tmp_combined" + video.suffix)
    if output.exists():
        output.unlink()

    ok = try_lossless_concat(intro, video, output)

    if not ok:
        if output.exists():
            output.unlink()
        if not check_nvenc_available():
            print(f"❌ {NVENC_CODEC} not available in this ffmpeg build — can't GPU-encode.")
            sys.exit(1)
        ok = gpu_lossless_concat(intro, video, output)

    if not ok or not output.exists() or output.stat().st_size == 0:
        print("❌ Combine failed — original left untouched.")
        if output.exists():
            output.unlink()
        sys.exit(1)

    os.replace(output, video)  # atomic same-filesystem replace
    print(f"✅ Done: {video}")


def parse_args():
    p = argparse.ArgumentParser(description="Prepend an intro clip to a video, in place.")
    p.add_argument("--video", type=Path, default=DEFAULT_VIDEO, help="Path to the target video")
    p.add_argument("--intro", type=Path, default=DEFAULT_INTRO, help="Path to the intro clip")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    video = args.video if args.video.is_absolute() else BASE_DIR / args.video
    intro = args.intro if args.intro.is_absolute() else BASE_DIR / args.intro
    combine(intro, video)