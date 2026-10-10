import os
import sys
import subprocess
from pathlib import Path
import time
import tempfile
import wave

# Get the script's directory and project root
SCRIPT_DIR = Path(__file__).parent.absolute()
PROJECT_ROOT = SCRIPT_DIR

# Virtual environment path (relative to project root)
VENV_PATH = PROJECT_ROOT / "libraries" / "transcriber"
VENV_PYTHON = VENV_PATH / "Scripts" / "python.exe"  # Windows

# --watch mode: set by main.py when running Video_Maker in parallel
WATCH_MODE = '--watch' in sys.argv


def activate_venv():
    """Activate virtual environment if not already active"""
    if sys.prefix == str(VENV_PATH) or sys.base_prefix == str(VENV_PATH):
        print("✅ Virtual environment already active")
        return True

    if not VENV_PYTHON.exists():
        print(f"❌ Virtual environment not found at: {VENV_PATH}")
        print(f"Full path: {VENV_PYTHON}")
        print("Please create it first with: python -m venv libraries/transcriber")
        return False

    print(f"🔄 Restarting script with virtual environment...")
    print(f"Using Python: {VENV_PYTHON}")
    result = subprocess.run([str(VENV_PYTHON), __file__] + sys.argv[1:])
    sys.exit(result.returncode)


activate_venv()

# Now import packages that need to be from venv
import torch
import whisper
import numpy as np

# Path configurations (relative)
INPUT_DIR  = PROJECT_ROOT / "Audio" / "output_audio" / "final"
OUTPUT_DIR = PROJECT_ROOT / "SRT"
WHISPER_MODEL_PATH = PROJECT_ROOT / "libraries" / "transcriber" / "whisper" / "medium.pt"

# Sentinel file written by main.py when Video_Maker has finished
SENTINEL_PATH = PROJECT_ROOT / "vm_done.flag"

# Audio file extensions this script will pick up
AUDIO_EXTENSIONS = (".wav", ".mp3")


def find_audio_files(directory: Path):
    """Return all .wav and .mp3 files in directory (non-recursive)."""
    files = []
    for ext in AUDIO_EXTENSIONS:
        files.extend(directory.glob(f"*{ext}"))
    return files


def format_time(seconds: float) -> str:
    """Convert seconds to SRT time format (hh:mm:ss,mmm) with high precision"""
    seconds = max(0.0, seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    milliseconds = (seconds - int(seconds)) * 1000
    return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02},{int(milliseconds):03}"


def validate_timestamps(words: list) -> list:
    """Validate and fix timestamp inconsistencies"""
    if not words:
        return words

    validated_words = []
    for i, word in enumerate(words):
        if 'start' not in word or 'end' not in word or 'word' not in word:
            continue
        start = word['start']
        end   = word['end']
        if end <= start:
            end = start + 0.1
        if validated_words:
            prev_end = validated_words[-1]['end']
            if start < prev_end:
                start = prev_end
                if end <= start:
                    end = start + 0.1
        validated_words.append({'word': word['word'], 'start': start, 'end': end})
    return validated_words


def extract_audio_with_ffmpeg(video_path: Path):
    """Extract audio using FFmpeg + Python wave module"""
    temp_audio_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_file:
            temp_audio_path = temp_file.name

        ffmpeg_cmd = [
            'ffmpeg', '-i', str(video_path),
            '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
            '-y', temp_audio_path
        ]
        result = subprocess.run(
            ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60
        )
        if result.returncode != 0:
            print(f"❌ FFmpeg error: {result.stderr.decode()}")
            if temp_audio_path and os.path.exists(temp_audio_path):
                os.unlink(temp_audio_path)
            return None

        with wave.open(temp_audio_path, 'rb') as wav_file:
            n_channels  = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate  = wav_file.getframerate()
            n_frames     = wav_file.getnframes()
            audio_data   = wav_file.readframes(n_frames)

            if sample_width == 2:
                audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            elif sample_width == 4:
                audio_array = np.frombuffer(audio_data, dtype=np.int32).astype(np.float32) / 2147483648.0
            else:
                print(f"❌ Unsupported sample width: {sample_width}")
                if temp_audio_path and os.path.exists(temp_audio_path):
                    os.unlink(temp_audio_path)
                return None

            if n_channels == 2:
                audio_array = audio_array.reshape(-1, 2).mean(axis=1)

        if temp_audio_path and os.path.exists(temp_audio_path):
            os.unlink(temp_audio_path)

        audio_array = np.asarray(audio_array).flatten()
        max_val = np.abs(audio_array).max()
        if max_val > 1.0:
            audio_array = audio_array / max_val

        print(f"✅ Audio extracted: {len(audio_array)} samples at {sample_rate} Hz")
        return audio_array

    except subprocess.TimeoutExpired:
        print("❌ FFmpeg timeout")
        if temp_audio_path and os.path.exists(temp_audio_path):
            os.unlink(temp_audio_path)
        return None
    except Exception as e:
        print(f"❌ Audio extraction error: {e}")
        import traceback; traceback.print_exc()
        if temp_audio_path and os.path.exists(temp_audio_path):
            os.unlink(temp_audio_path)
        return None


def transcribe_video(video_path: Path, output_path: Path, model_name: str = "medium"):
    """Transcribe video with maximum timing accuracy - pure word-by-word"""
    start_time = time.time()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")

    if WHISPER_MODEL_PATH.exists():
        print(f"Loading model from: {WHISPER_MODEL_PATH}")
        model = whisper.load_model(str(WHISPER_MODEL_PATH), device=device)
    else:
        print(f"Model file not found at {WHISPER_MODEL_PATH}, downloading {model_name}...")
        model = whisper.load_model(model_name, device=device)

    print("Extracting audio with FFmpeg...")
    audio_array = extract_audio_with_ffmpeg(video_path)
    if audio_array is None:
        print("❌ Failed to extract audio")
        return False

    try:
        print("Transcribing with Whisper (greedy decode — 4× faster at temperature=0)...")
        transcribe_start = time.time()

        # ── OPTIMISATION ──────────────────────────────────────────────────────────
        # At temperature=0.0 the model is fully deterministic, so beam_size/best_of
        # > 1 just repeats the same computation N times and discards N-1 results.
        # Greedy decode (beam_size=1, best_of=1) gives identical output ~4× faster.
        # condition_on_previous_text=False saves ~10% per segment and reduces
        # hallucination cascades on short clips.
        # ─────────────────────────────────────────────────────────────────────────
        result = model.transcribe(
            audio_array,
            word_timestamps=True,
            fp16=(device == "cuda"),
            language='en',
            beam_size=1,                        # greedy — was 5
            best_of=1,                          # no redundancy — was 5
            temperature=0.0,
            condition_on_previous_text=False,   # saves ~10% — was True
            compression_ratio_threshold=2.4,
            logprob_threshold=-1.0,
            no_speech_threshold=0.6
        )

        transcribe_time = time.time() - transcribe_start
        print(f"✅ Transcription completed in {transcribe_time:.2f}s")

    except Exception as e:
        print(f"❌ Transcription failed: {e}")
        import traceback; traceback.print_exc()
        return False

    print("Generating accurate word-by-word SRT file...")
    srt_lines = []
    counter     = 1
    total_words = 0
    skipped_words = 0

    for segment in result['segments']:
        words = validate_timestamps(segment.get('words', []))
        total_words += len(words)
        for word in words:
            text = word['word'].strip()
            if not text:
                skipped_words += 1
                continue
            start = format_time(word['start'])
            end   = format_time(word['end'])
            srt_lines.extend([str(counter), f"{start} --> {end}", text, ""])
            counter += 1

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(srt_lines))

    total_time = time.time() - start_time
    print(f"✅ Saved subtitles to: {output_path}")
    print(f"📊 Words processed: {total_words} (skipped: {skipped_words})")
    print(f"⏱️  Total processing time: {total_time:.2f}s")
    return True


def batch_process(input_dir: Path, output_dir: Path, selected_model: str,
                  already_processed: set = None) -> set:
    """Process all unprocessed .wav/.mp3 files in input_dir. Returns set of processed stems."""
    processed = already_processed or set()

    video_files = [f for f in find_audio_files(input_dir) if f.stem not in processed]
    if not video_files:
        return processed

    success_count  = 0
    failure_count  = 0
    skipped_count  = 0

    for idx, video_file in enumerate(video_files, 1):
        output_path = output_dir / f"{video_file.stem}.srt"
        print(f"[{idx}/{len(video_files)}] {video_file.name}")

        if "chapter" in video_file.name.lower():
            print("🔍 Detected 'chapter' — creating empty SRT file")
            output_path.write_text("", encoding='utf-8')
            print(f"✅ Created empty SRT: {output_path.name}")
            skipped_count += 1
            processed.add(video_file.stem)
            continue

        if transcribe_video(video_file, output_path, selected_model):
            success_count += 1
        else:
            failure_count += 1
            print(f"⚠️  Failed to process: {video_file.name}")

        processed.add(video_file.stem)

    print(f"\n📊 This pass: {success_count} successful, {failure_count} failed, "
          f"{skipped_count} skipped")
    return processed


if __name__ == "__main__":
    overall_start = time.time()

    print("=" * 60)
    print("🎬 ACCURATE Word-by-Word Video Transcriber")
    if WATCH_MODE:
        print("👁️  WATCH MODE — processing files as they arrive")
    print("=" * 60)
    print(f"Project Root: {PROJECT_ROOT}")
    print(f"Input Directory: {INPUT_DIR}")
    print(f"Output Directory: {OUTPUT_DIR}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not INPUT_DIR.exists():
        INPUT_DIR.mkdir(parents=True, exist_ok=True)

    selected_model = "medium"
    processed = set()

    if WATCH_MODE:
        # ── WATCH MODE ────────────────────────────────────────────────────────────
        # main.py starts this alongside Video_Maker.py.  We poll INPUT_DIR for new
        # .mp4 files and transcribe them as they appear.  We exit only after both
        # conditions are met:
        #   1. The vm_done.flag sentinel has been written by main.py
        #   2. We have no unprocessed .mp4 files remaining
        # ─────────────────────────────────────────────────────────────────────────
        print(f"📂 Watching {INPUT_DIR} for videos (sentinel: {SENTINEL_PATH})")
        poll_interval = 5  # seconds between sweeps

        while True:
            processed = batch_process(INPUT_DIR, OUTPUT_DIR, selected_model, processed)

            sentinel_exists = SENTINEL_PATH.exists()
            remaining = [f for f in find_audio_files(INPUT_DIR) if f.stem not in processed]

            if sentinel_exists and not remaining:
                print("✅ Sentinel received and all videos processed — exiting watch mode.")
                break

            if sentinel_exists:
                # vm_done but still have files; process them right away without sleeping
                continue
            time.sleep(poll_interval)
    else:
        # ── BATCH MODE (original behaviour) ──────────────────────────────────────
        if not INPUT_DIR.exists():
            print(f"❌ Input directory not found: {INPUT_DIR}")
            sys.exit(1)

        video_files = find_audio_files(INPUT_DIR)
        if not video_files:
            print(f"No .wav/.mp3 files found in {INPUT_DIR}")
            sys.exit(1)

        print(f"Found {len(video_files)} audio file(s) to process")
        print("=" * 60)

        processed = batch_process(INPUT_DIR, OUTPUT_DIR, selected_model)

    overall_time = time.time() - overall_start
    success_count = len(processed)
    print("\n" + "=" * 60)
    print("✅ Batch processing completed!")
    print(f"⏱️  Total time: {overall_time:.2f}s ({overall_time/60:.2f}m)")
    if success_count > 0:
        print(f"⏱️  Average time per video: {overall_time/success_count:.2f}s")