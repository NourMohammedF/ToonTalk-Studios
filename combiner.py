r"""
combiner.py  —  Automated video assembly for the YouTube pipeline.

Segment types
─────────────
  [CLIP]    regular clips  → concatenated with gray progress bar
  [CHAPTER] chapter clips  → played as-is, no progress bar

Outputs
───────
  final/final_output.mp4   assembled video + background music
  final/chapters.srt        chapter timestamps (SRT subtitle format)
"""

import os
import sys
import subprocess
from pathlib import Path

# ══════════════════════════════════════════════════════════════════════════════
#  Bootstrap: re-launch inside the project's virtual environment
# ══════════════════════════════════════════════════════════════════════════════
BASE_DIR    = Path(__file__).resolve().parent
VENV_PATH   = BASE_DIR / "libraries" / "combiner"
VENV_PYTHON = VENV_PATH / "Scripts" / "python.exe"


def is_venv_active():
    return sys.prefix == str(VENV_PATH)


if not is_venv_active():
    if not VENV_PYTHON.exists():
        print(f"❌ Virtual environment not found: {VENV_PATH}")
        print("   Create it with: python -m venv libraries\\combiner")
        sys.exit(1)
    print(f"🔄 Switching to virtual environment: {VENV_PATH}")
    sys.exit(subprocess.run([str(VENV_PYTHON), __file__] + sys.argv[1:]).returncode)

# ══════════════════════════════════════════════════════════════════════════════
#  Imports  (executed inside the venv)
# ══════════════════════════════════════════════════════════════════════════════
import re
import glob
import random
import tempfile
import shutil
import numpy as np

from moviepy.editor import (
    VideoFileClip, AudioFileClip,
    concatenate_videoclips, concatenate_audioclips,
    CompositeAudioClip, ColorClip,
)
from moviepy.audio.AudioClip import AudioArrayClip
from moviepy.video.fx.all   import fadeout
from moviepy.audio.fx.all   import audio_fadein, audio_fadeout

print("✅ Running inside virtual environment\n")

# ══════════════════════════════════════════════════════════════════════════════
#  Paths
# ══════════════════════════════════════════════════════════════════════════════
INPUT_FOLDER  = BASE_DIR / "Videos" / "Sub with front"
OUTPUT_FOLDER = BASE_DIR / "final"
OUTPUT_FILE   = OUTPUT_FOLDER / "final_output.mp4"
SRT_FILE      = OUTPUT_FOLDER / "chapters.srt"
MUSIC_FOLDER  = BASE_DIR / "Background music"

# ══════════════════════════════════════════════════════════════════════════════
#  Tuning / render settings
# ══════════════════════════════════════════════════════════════════════════════
FADE_DUR    = 0.5    # video clip fade-out AND black-gap length  (seconds)
MUSIC_FADE  = 2.0    # music fade-in / fade-out per section      (seconds)
BAR_HEIGHT  = 20     # progress-bar height (pixels)
BAR_COLOR   = (0, 0, 0)   # gray (R, G, B)
BAR_OPACITY = 0.85

# ── YouTube-optimised ffmpeg flags ──
# CRF 18 + yuv420p + High Profile survive YouTube's re-encoder without
# introducing extra compression artefacts or colour-space mismatches.
_YT_PARAMS = [
    "-crf",       "18",         # high quality; survives YT's re-encode
    "-pix_fmt",   "yuv420p",    # REQUIRED — YT rejects other colour spaces
    "-profile:v", "high",       # H.264 High Profile
    "-level:v",   "4.2",        # covers up to 1080p60; change to 5.1 for 4K
    "-bf",        "2",          # B-frames (YouTube recommendation)
    "-movflags",  "+faststart", # metadata at front → streaming-friendly
]
YT_WRITE_KW = dict(
    codec         = "libx264",
    audio_codec   = "aac",
    audio_fps     = 48000,      # YouTube standard sample rate
    audio_bitrate = "384k",     # generous stereo audio bitrate
    threads       = 8,
    preset        = "slow",     # more CPU effort → better quality/size ratio
    ffmpeg_params = _YT_PARAMS,
)

# ══════════════════════════════════════════════════════════════════════════════
#  Utility functions
# ══════════════════════════════════════════════════════════════════════════════

def extract_number(filename: str) -> int:
    """Return the leading integer of a filename for reliable numeric sorting."""
    m = re.match(r'^(\d+)', filename)
    return int(m.group(1)) if m else 10_000_000


def is_chapter_file(filename: str) -> bool:
    return 'chapter-' in filename.lower()


def fmt_srt(sec: float) -> str:
    """Convert seconds → SRT timestamp  HH:MM:SS,mmm"""
    ms = int(round((sec % 1) * 1000))
    s  = int(sec) % 60
    m  = (int(sec) // 60) % 60
    h  = int(sec) // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def silent_audio(duration: float, fps: int = 48000) -> AudioArrayClip:
    """Return a stereo silent clip (used to give black-gap clips an audio track)."""
    n = max(2, int(duration * fps))
    return AudioArrayClip(np.zeros((n, 2), dtype=np.float32), fps=fps)


def get_music_files() -> list:
    """Return all audio files found in MUSIC_FOLDER."""
    exts  = ("mp3", "wav", "m4a", "aac", "ogg", "flac")
    found = set()
    for ext in exts:
        found.update(glob.glob(str(MUSIC_FOLDER / f"*.{ext}")))
        found.update(glob.glob(str(MUSIC_FOLDER / f"*.{ext.upper()}")))
    return sorted(found)


def looped_audio(path: str, duration: float):
    """
    Load an audio file and return a clip of exactly `duration` seconds,
    looping the source track as many times as needed.
    """
    base = AudioFileClip(path)
    if base.duration >= duration:
        return base.subclip(0, duration)
    n     = int(np.ceil(duration / base.duration))
    parts = [AudioFileClip(path) for _ in range(n)]
    loop  = concatenate_audioclips(parts)
    base.close()
    return loop.subclip(0, duration)

# ══════════════════════════════════════════════════════════════════════════════
#  Gray progress bar  (transparent background)
# ══════════════════════════════════════════════════════════════════════════════

def add_progress_bar(clip,
                     height:       int   = BAR_HEIGHT,
                     color:        tuple = BAR_COLOR,
                     opacity:      float = BAR_OPACITY,
                     hb_threshold: float = 2.0,
                     hb_freq:      float = 3.0,
                     hb_amp:       float = 0.3):
    """
    Overlays a gray progress bar at the bottom of `clip`.

    • Only the *filled* portion is painted — the unfilled area is transparent
      (the original video shows through with no background rectangle).
    • Heartbeat: in the last `hb_threshold` seconds the bar pulses taller
      at `hb_freq` Hz with amplitude `hb_amp` × base height.
    """
    dur = clip.duration

    def fl(get_frame, t):
        frame  = get_frame(t).astype(float)
        H, W   = frame.shape[:2]
        filled = int(min(1.0, t / dur) * W)

        # Heartbeat — bar grows taller near the end of the clip
        curr_h = height
        time_left = dur - t
        if time_left <= hb_threshold:
            tau    = hb_threshold - time_left
            curr_h = int(height * (1 + hb_amp * np.sin(2 * np.pi * hb_freq * tau)))
            curr_h = max(1, curr_h)

        if filled > 0 and curr_h > 0:
            y0 = H - curr_h
            for ch in range(3):
                frame[y0:H, :filled, ch] = (
                    frame[y0:H, :filled, ch] * (1.0 - opacity)
                    + color[ch]              *        opacity
                )
        return frame.astype('uint8')

    return clip.fl(fl, apply_to=['video'])

# ══════════════════════════════════════════════════════════════════════════════
#  Background music builder
# ══════════════════════════════════════════════════════════════════════════════

def build_bg_music(timeline: list, music_files: list, total_dur: float):
    """
    Compose one background audio track that spans the entire video.

    Section / track assignment
    ──────────────────────────
    Sections are separated at every chapter-clip start time:

        Track A  [ 0 ───────────── ch₁ start ]
        Track B  [ ch₁ start ───── ch₂ start ]
        Track C  [ ch₂ start ───── ch₃ start ]  …

    Each track is chosen randomly (without repeating until the pool is
    exhausted).  Tracks are looped if they are shorter than the section.

    Volume inside each section
    ──────────────────────────
        1.00  (100 %)  during chapter-clip intervals
        0.15  ( 15 %)  during regular-clip and transition intervals

    Each section has an independent fade-in at its start and fade-out at
    its end (MUSIC_FADE seconds each, capped at 10 % of section length).

    Returns (composite_audio, open_clips).
    The caller MUST keep open_clips alive until after write_videofile()
    completes, then close them.  MoviePy evaluates audio lazily; closing
    an AudioFileClip before the render reads it kills its ffmpeg process
    and causes 'NoneType has no attribute stdout'.
    """
    if not music_files:
        return None, []

    # Build section boundaries: [0, ch₁_start, ch₂_start, …, total_dur]
    ch_starts  = sorted({e['start'] for e in timeline if e.get('is_chapter')})
    boundaries = sorted({0.0} | set(ch_starts) | {total_dur})

    pool       = music_files[:]
    random.shuffle(pool)
    pool_i     = 0
    parts      = []
    open_clips = []   # kept alive until after the render; closed by main()

    for bi in range(len(boundaries) - 1):
        sec_s = boundaries[bi]
        sec_e = boundaries[bi + 1]
        sec_d = sec_e - sec_s
        if sec_d <= 0:
            continue

        # Cycle through the shuffled pool (re-shuffle when exhausted)
        if pool_i >= len(pool):
            pool   = music_files[:]
            random.shuffle(pool)
            pool_i = 0
        track  = pool[pool_i];  pool_i += 1

        music  = looped_audio(track, sec_d)
        # NOTE: do NOT close `music` here.  sub_clips reference it through
        # MoviePy's lazy-eval closure chain.  Closing early sets proc=None
        # which causes 'NoneType has no attribute stdout' during the render.
        open_clips.append(music)

        # Build volume-varying sub-clips based on overlapping timeline entries
        sub_clips = []
        for entry in sorted(timeline, key=lambda e: e['start']):
            ov_s = max(entry['start'], sec_s)
            ov_e = min(entry['end'],   sec_e)
            if ov_s >= ov_e:
                continue
            vol = 1.0 if entry.get('is_chapter') else 0.07
            sub_clips.append(
                music.subclip(ov_s - sec_s, ov_e - sec_s).volumex(vol)
            )

        if not sub_clips:
            continue

        sec_audio = concatenate_audioclips(sub_clips)

        # Per-section fades (capped at 10 % of section length each side)
        fi = min(MUSIC_FADE, sec_d * 0.10)
        fo = min(MUSIC_FADE, sec_d * 0.10)
        sec_audio = audio_fadein( sec_audio, fi)
        sec_audio = audio_fadeout(sec_audio, fo)

        parts.append(sec_audio.set_start(sec_s))

    if not parts:
        return None, open_clips

    return CompositeAudioClip(parts).set_duration(total_dur), open_clips

# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

    # ── 1.  Collect and numerically sort all video files ─────────────────────
    raw = sorted(
        [f for f in os.listdir(INPUT_FOLDER)
         if f.lower().endswith(('.mp4', '.mov', '.avi', '.mkv'))],
        key=extract_number,
    )
    if not raw:
        print("❌ No video files found in the input folder."); return

    print(f"📂 {len(raw)} file(s) found in input folder:")
    for f in raw:
        tag = "[CHAPTER]" if is_chapter_file(f) else "[CLIP]   "
        print(f"   {tag}  {f}")

    music_files = get_music_files()
    if music_files:
        print(f"\n🎵 {len(music_files)} background music track(s) found")
    else:
        print("\n⚠️  No music found in 'Background music' folder — "
              "skipping background audio.")

    # ── 2.  Group files into logical segments ─────────────────────────────────
    # 'regular' → contiguous non-chapter clips (joined + progress bar)
    # 'chapter' → single chapter clip (played as-is)
    segments = []
    buf: list = []
    for f in raw:
        p = INPUT_FOLDER / f
        if is_chapter_file(f):
            if buf:
                segments.append(('regular', buf));  buf = []
            segments.append(('chapter', [p]))
        else:
            buf.append(p)
    if buf:
        segments.append(('regular', buf))

    # ── 3.  Measure each segment's total duration ─────────────────────────────
    print("\n⏱  Measuring clip durations …")
    seg_durs = []
    for stype, spaths in segments:
        d = 0.0
        for p in spaths:
            c = VideoFileClip(str(p));  d += c.duration;  c.close()
        seg_durs.append(d)
        print(f"   [{stype:7s}]  {len(spaths)} clip(s)  →  {d:.2f}s")

    # ── 4.  Build the final-video timeline ────────────────────────────────────
    # Every segment is separated from the next by a FADE_DUR-long black gap.
    # The timeline covers [0, total_dur] with no gaps — segments + black gaps.
    timeline = []
    t = 0.0
    for i, ((stype, spaths), dur) in enumerate(zip(segments, seg_durs)):
        is_ch = (stype == 'chapter')
        timeline.append({
            'start':      t,
            'end':        t + dur,
            'is_chapter': is_ch,
            'seg_idx':    i,
        })
        t += dur
        if i < len(segments) - 1:          # black-gap transition
            timeline.append({
                'start':      t,
                'end':        t + FADE_DUR,
                'is_chapter': False,
                'seg_idx':    None,
            })
            t += FADE_DUR
    total_dur = t
    print(f"\n🎞  Total assembled length: {total_dur:.2f}s  "
          f"({total_dur / 60:.1f} min)")

    # ── 5.  Write SRT chapter file ────────────────────────────────────────────
    # One entry per chapter clip; timestamps reflect the exact position in the
    # final video.  The end timestamp is trimmed by FADE_DUR so the subtitle
    # disappears the moment the screen begins to fade to black.
    srt_lines = []
    srt_n = ch_n = 1
    for entry in timeline:
        if not entry['is_chapter']:
            continue
        stem  = Path(segments[entry['seg_idx']][1][0]).stem
        # "03-chapter-intro-scene" → "Chapter Intro Scene"
        label = re.sub(r'^\d+[-_\s]*', '', stem)
        label = label.replace('-', ' ').replace('_', ' ').strip().title()
        if not label:
            label = f"Chapter {ch_n}"

        ch_start = entry['start']
        ch_end   = entry['end'] - FADE_DUR   # subtitle disappears as fade begins
        srt_lines += [
            str(srt_n),
            f"{fmt_srt(ch_start)} --> {fmt_srt(ch_end)}",
            label,
            "",
        ]
        srt_n += 1;  ch_n += 1

    SRT_FILE.write_text('\n'.join(srt_lines), encoding='utf-8')
    print(f"📝 SRT  → {SRT_FILE}")
    # Print a preview
    for line in srt_lines[:min(len(srt_lines), 12)]:
        if line:  print(f"   {line}")

    # ── 6.  Render each segment to an intermediate temp file ──────────────────
    temp_dir   = tempfile.mkdtemp()
    temp_files: list = []

    try:
        for i, ((stype, spaths), dur) in enumerate(zip(segments, seg_durs)):
            print(f"\n🎬 Segment {i+1}/{len(segments)}  [{stype}]  ({dur:.1f}s)")

            clips = [VideoFileClip(str(p)) for p in spaths]
            combo = (concatenate_videoclips(clips, method="compose")
                     if len(clips) > 1 else clips[0])

            if stype == 'regular':
                combo = add_progress_bar(combo)

            # Fade out at end (capped so it never exceeds 45 % of segment)
            fade_t = min(FADE_DUR, dur * 0.45)
            combo  = fadeout(combo, fade_t)

            tmp = os.path.join(temp_dir, f"seg_{i:04d}.mp4")
            combo.write_videofile(tmp, fps=combo.fps or 30, **YT_WRITE_KW)
            temp_files.append(tmp)

            combo.close()
            for c in clips:
                try:    c.close()
                except: pass

        # ── 7.  Stitch segments with black-gap transitions ─────────────────────
        print("\n🔗 Stitching segments …")
        final_clips = []
        ref_size    = None

        for i, tmp in enumerate(temp_files):
            sc = VideoFileClip(tmp)
            if ref_size is None:
                ref_size = sc.size
            final_clips.append(sc)
            if i < len(temp_files) - 1:
                # Silent black frame between segments
                blk = ColorClip(ref_size, color=(0, 0, 0), duration=FADE_DUR)
                blk = blk.set_audio(silent_audio(FADE_DUR))
                final_clips.append(blk)

        final = concatenate_videoclips(final_clips, method="compose")

        # ── 8.  Composite background music ────────────────────────────────────
        open_music = []
        if music_files:
            print("🎵 Compositing background music …")
            bg, open_music = build_bg_music(timeline, music_files, total_dur)
            if bg is not None:
                orig = final.audio
                final = final.set_audio(
                    CompositeAudioClip([orig, bg]) if orig else bg
                )

        # ── 9.  Final YouTube-optimised render ────────────────────────────────
        print(f"\n🚀 Writing final video → {OUTPUT_FILE}")
        final.write_videofile(
            str(OUTPUT_FILE), fps=final.fps or 30, **YT_WRITE_KW
        )

        # Close video clips
        for c in final_clips:
            try:    c.close()
            except: pass

        # Safe to close music readers now that the render is complete
        for c in open_music:
            try:    c.close()
            except: pass

        print(f"\n✅ Done!\n   📹 {OUTPUT_FILE}\n   📝 {SRT_FILE}")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()