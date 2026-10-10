# ToonTalk Studios - Video Production Pipeline

An automated end-to-end video production pipeline for creating YouTube podcast-style videos. The pipeline handles script generation, text-to-speech, video rendering (via Adobe Express), transcription, subtitle animation, front overlay addition, segment combining, and YouTube upload.

## 🎬 Pipeline Overview

```
┌─────────────────────┐
│  clean_pipeline.py  │  ← Cleans previous run artifacts
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  trend_idea.py      │  ← Fetches trending topics from Reddit/YouTube
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│ thumbnail_generator │  ← Generates thumbnails via Adobe Express
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│ generate_script.py  │  ← AI script generation (NVIDIA NIM / Gemini)
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│ word_to_audio.py    │  ← Text-to-speech (ElevenLabs / Edge TTS)
└─────────┬───────────┘
          ▼
┌─────────────────────┐     ┌─────────────────────┐
│ Video_Maker_V2.py   │     │  transcriber.py     │  ← PARALLEL
│ (Adobe Express)     │     │  --watch mode       │
└─────────┬───────────┘     └─────────┬───────────┘
          ▼                           ▼
┌─────────────────────────────────────────────┐
│           Subtitle_Animator.py              │  ← Animated subtitles
└─────────────────────┬───────────────────────┘
          ▼
┌─────────────────────┐
│   front_adder.py    │  ← Front overlay/intro
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│    combiner.py      │  ← Combine all segments
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  upload_youtube.js  │  ← Upload to YouTube
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│   shutdown.py       │  ← Optional system shutdown
└─────────────────────┘
```

## 📁 Project Structure

```
repo/
├── main.py                    # Pipeline orchestrator (run this)
├── clean_pipeline.py          # Cleanup utility
├── trend_idea.py              # Trend research (Reddit, YouTube, DuckDuckGo)
├── thumbnail_generator.py     # Thumbnail generation (Adobe Express + Gemini)
├── generate_script.py         # Podcast script generator (Gemini via NVIDIA NIM)
├── word_to_audio.py           # TTS audio generation
├── Video_Maker.py             # Video rendering v1 (Adobe Express)
├── Video_Maker_V2.py          # Video rendering v2 (optimized, parallel)
├── transcriber.py             # Whisper transcription (with --watch mode)
├── Subtitle_Animator.py       # Animated subtitle generation
├── front_adder.py             # Front overlay/intro adder
├── combiner.py                # Video segment combiner
├── upload_youtube.js          # YouTube uploader (Node.js)
├── shutdown.py                # Optional system shutdown
├── upload.config.example.json # Configuration template (copy to upload.config.json)
├── package.json               # Node dependencies
└── .gitignore
```

## ⚙️ Configuration

**Never commit `upload.config.json`** - it contains API keys and credentials.

1. Copy the example config:
   ```bash
   cp upload.config.example.json upload.config.json
   ```

2. Fill in all required fields:
   - **YouTube**: Email + app password (for upload)
   - **Google**: API key for Gemini (via NVIDIA NIM)
   - **Reddit**: Client ID, secret, user agent (for trend research)
   - **YouTube Data API**: API key (for trend research)
   - **Paths**: Adjust if needed

## 🚀 Quick Start

### Prerequisites
- Python 3.10+
- Node.js 18+
- FFmpeg (in PATH)
- Virtual environment at `./libraries/Promoter` (auto-activated by scripts)

### Install Dependencies
```bash
# Python deps (in venv)
cd libraries/Promoter/Scripts
activate
pip install openai python-docx whisper ffmpeg-python

# Node deps
npm install
```

### Run the Pipeline
```bash
python main.py
```

The pipeline runs sequentially with one parallel stage (Video_Maker + transcriber). Total runtime: ~15-30 minutes depending on video length.

## 🔑 Required API Keys

| Service | Purpose | Where to Get |
|---------|---------|--------------|
| Google Gemini (NVIDIA NIM) | Script generation, thumbnails | [NVIDIA NIM](https://build.nvidia.com/) or [Google AI Studio](https://aistudio.google.com/) |
| YouTube Data API v3 | Trend research, upload | [Google Cloud Console](https://console.cloud.google.com/) |
| Reddit API | Trend research | [Reddit Apps](https://www.reddit.com/prefs/apps) |
| ElevenLabs (optional) | Premium TTS | [ElevenLabs](https://elevenlabs.io/) |
| Adobe Express | Video rendering, thumbnails | Requires Adobe account |

## 📝 Key Files

- **`main.py`** - Entry point, orchestrates the entire pipeline
- **`upload.config.example.json`** - Template for configuration (copy to `upload.config.json`)
- **`generate_script.py`** - Generates podcast scripts with Max & Zoe personas
- **`Video_Maker_V2.py`** - Renders video clips via Adobe Express automation
- **`transcriber.py`** - Transcribes audio to SRT (supports `--watch` for parallel processing)
- **`upload_youtube.js`** - Uploads final video to YouTube with SEO metadata

## ⚠️ Important Notes

1. **Adobe Express Automation**: Video_Maker and thumbnail_generator use browser automation (Playwright) to control Adobe Express. You must be logged into Adobe in your default browser profile.

2. **YouTube Upload**: Uses `youtube-videos-uploader` npm package. First run requires manual OAuth consent.

3. **Parallel Stage**: Video_Maker_V2 and transcriber run in parallel. Transcriber watches for new MP4 files and transcribes them as they appear.

4. **Sentinel File**: `vm_done.flag` signals transcriber to do final sweep and exit.

5. **Virtual Environment**: Scripts auto-activate `./libraries/Promoter` venv. Create it first:
   ```bash
   python -m venv libraries/Promoter
   libraries/Promoter/Scripts/activate
   pip install openai python-docx whisper ffmpeg-python
   ```

## 📄 License

Private project - not for distribution.