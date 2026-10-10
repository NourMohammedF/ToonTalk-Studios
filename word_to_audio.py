import os
import sys
import subprocess
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

# ==================== NOW SAFE TO IMPORT PACKAGES ====================
import re
import warnings
import numpy as np
import torch
import soundfile as sf
from docx import Document
from pydub import AudioSegment
import time

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

# === SET CACHE PATH BEFORE IMPORTING CHATTERBOX ===
VENV_DIR = VENV_PATH
MODEL_CACHE = VENV_DIR / "chatterbox_model"
os.environ['HUGGINGFACE_HUB_CACHE'] = str(MODEL_CACHE)
os.environ['TRANSFORMERS_CACHE']    = str(MODEL_CACHE)
os.environ['HF_HOME']               = str(MODEL_CACHE)
os.environ['HF_DATASETS_CACHE']     = str(MODEL_CACHE)

from chatterbox.tts import ChatterboxTTS

# Device setup
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Using device: {device}")

# ==================== PATH CONFIGURATION ====================
CLONE_MODELS_DIR = VENV_DIR / "clone_models"
VOICE_REFERENCES = {
    "male":   CLONE_MODELS_DIR / "male_voice.mp3",
    "female": CLONE_MODELS_DIR / "female_voice.mp3"
}

INPUT_DOCX = SCRIPT_DIR / "pod1.docx"
OUTPUT_DIR = SCRIPT_DIR / "Audio" / "output_audio" / "final"

# ==================== LOAD CHATTERBOX MODEL ====================
print("Loading Chatterbox TTS model...")
print(f"Model cache directory: {MODEL_CACHE}")
try:
    model = ChatterboxTTS.from_pretrained(device=device)
    print("✅ Chatterbox model loaded successfully")
    print(f"Model sample rate: {model.sr} Hz")
except Exception as e:
    print(f"Failed to load Chatterbox model: {e}")
    import traceback; print(traceback.format_exc())
    sys.exit(1)

# ==================== OPTIMIZED FUNCTIONS ====================

import inspect

VOICE_PROMPT_TEXTS = {
    "male": "Hello there. My name is Sam and this is a short sample for voice cloning. I'm speaking naturally, just like I would in a real conversation. The quick brown fox jumps over the lazy dog. That's a classic sentence used to test voices. Numbers can sound different too. 1, 2, 3, 4, 5. Let's try something expressive. Wow, that's amazing. And now, something calm and steady. Take a deep breath and relax. Finally, here's how I might sound when asking a question. Do you think this voice sounds real? Thanks for listening. This recording should be clear, natural, and about a minute long.",
    "female": "Hello there. My name is Sam and this is a short sample for voice cloning. I'm speaking naturally, just like I would in a real conversation. The quick brown fox jumps over the lazy dog. That's a classic sentence used to test voices. Numbers can sound different too. 1, 2, 3, 4, 5. Let's try something expressive. Wow, that's amazing. And now, something calm and steady. Take a deep breath and relax. Finally, here's how I might sound when asking a question. Do you think this voice sounds real? Thanks for listening. This recording should be clear, natural, and about a minute long."
}

def read_docx(file_path):
    """Read DOCX while preserving paragraph separation"""
    try:
        doc = Document(file_path)
        return [para.text.strip() for para in doc.paragraphs if para.text.strip()]
    except Exception as e:
        print(f"DOCX read error: {e}")
        raise


def parse_content(content_lines):
    """Parse chapters and dialogues from content lines."""
    elements = []
    for line in content_lines:
        line = line.strip()
        if not line:
            continue
        chapter_match = re.match(r'^(?:Chapter|CHAPTER)\s+(\d+)\s*:\s*(.+)$', line, re.IGNORECASE)
        if chapter_match:
            elements.append(('chapter', chapter_match.group(1), chapter_match.group(2)))
            continue
        dialogue_match = re.match(r'^(\w+)\s*:\s*(.+)$', line)
        if dialogue_match:
            speaker = dialogue_match.group(1).strip()
            text    = dialogue_match.group(2).strip()
            speaker_key = 'male' if speaker.lower() in ['max', 'male'] else 'female'
            elements.append(('dialogue', speaker_key, text))
    return elements


def preprocess_text(text):
    """Clean and prepare text for TTS"""
    text = re.sub(r'[^\w\s\.\,\!\?\-\:\;\'\"\(\)]', '', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'([.!?]){2,}', r'\1', text)
    text = re.sub(r'\s*([.,:!?])\s*', r'\1 ', text)
    return text.strip()


def _tts_generate(text: str, ref_audio_path, ref_text: str = None) -> np.ndarray:
    """Single-pass TTS inference — dynamically maps prompt arguments accepted by Chatterbox."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        
        gen_kwargs = {
            "exaggeration": 0.25,
            "cfg_weight": 0.90,
            "temperature": 0.22
        }
        
        # Inspect model signature dynamically to match valid keyword arguments
        sig = inspect.signature(model.generate)
        params = sig.parameters
        
        # Pass audio reference under the correct parameter name
        if "audio_prompt_path" in params:
            gen_kwargs["audio_prompt_path"] = str(ref_audio_path)
        elif "ref_audio_path" in params:
            gen_kwargs["ref_audio_path"] = str(ref_audio_path)
        elif "audio_prompt" in params:
            gen_kwargs["audio_prompt"] = str(ref_audio_path)

        # Pass reference text under the correct parameter name if supported by your version
        if ref_text:
            for possible_kwarg in ["audio_prompt_text", "ref_text", "prompt_text", "audio_text", "prompt"]:
                if possible_kwarg in params:
                    gen_kwargs[possible_kwarg] = ref_text
                    break

        wav_tensor = model.generate(text, **gen_kwargs)
    return wav_tensor.cpu().numpy().flatten().astype(np.float32)


def generate_long_text_audio(text, model, voice_clone, ref_text=None, max_length=280):
    """Generate audio for long text by chunking, keeping ref_text explicit"""
    sentences = re.split(r'[.!?]', text)
    chunks = []
    current_chunk = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(current_chunk) + len(sentence) < max_length:
            current_chunk += sentence + ". "
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = sentence + ". "
    if current_chunk:
        chunks.append(current_chunk.strip())

    print(f"Split into {len(chunks)} chunks for processing...")

    audio_chunks = []
    fade_samples = 1536
    fade_in  = np.linspace(0, 1, fade_samples, dtype=np.float32) ** 0.6
    fade_out = np.linspace(1, 0, fade_samples, dtype=np.float32) ** 0.6

    for i, chunk in enumerate(chunks):
        chunk_audio = _tts_generate(chunk, voice_clone, ref_text=ref_text)
        fade_len = min(fade_samples, len(chunk_audio) // 8)
        if fade_len > 0:
            chunk_audio[:fade_len]  *= fade_in[:fade_len]
            chunk_audio[-fade_len:] *= fade_out[:fade_len]
        audio_chunks.append(chunk_audio)

        if i < len(chunks) - 1:
            pause_samples = int(model.sr * 0.2)
            audio_chunks.append(np.zeros(pause_samples, dtype=np.float32))

    print(f"✅ Generated {len(chunks)}/{len(chunks)} chunks")
    full_audio = np.concatenate(audio_chunks)
    peak = np.max(np.abs(full_audio))
    if peak > 0.88:
        full_audio = full_audio * 0.88 / peak
    return full_audio


def generate_chatterbox_audio(text, speaker, output_path):
    """Generate audio with Chatterbox"""
    try:
        ref_audio_path = VOICE_REFERENCES.get(speaker)
        ref_text = VOICE_PROMPT_TEXTS.get(speaker)

        if not ref_audio_path or not ref_audio_path.exists():
            print(f"No reference audio found for {speaker} at {ref_audio_path}")
            return False

        processed_text = preprocess_text(text)
        print(f"Text length: {len(processed_text)} characters")
        print(f"Preview: {processed_text[:80]}...")

        if len(processed_text) > 150:
            print("Using chunked generation for long text...")
            wav_np = generate_long_text_audio(processed_text, model, ref_audio_path, ref_text=ref_text)
        else:
            print("Generating in single pass...")
            wav_np = _tts_generate(processed_text, ref_audio_path, ref_text=ref_text)
            peak = np.max(np.abs(wav_np))
            if peak > 0.92:
                wav_np = wav_np * 0.92 / peak

        sf.write(str(output_path), wav_np, model.sr, subtype='PCM_16')
        print(f"✅ Successfully saved: {output_path.name}")
        return True

    except Exception as e:
        print(f"Chatterbox TTS generation failed: {e}")
        import traceback; print(traceback.format_exc())
        return False


def generate_audio(elements, output_dir):
    """Generate audio files with correct sequencing"""
    output_dir.mkdir(parents=True, exist_ok=True)
    counter = 1

    for element in elements:
        try:
            if element[0] == 'chapter':
                chapter_num   = element[1].zfill(2)
                filename      = f"{counter:03d}-chapter-{chapter_num}.mp3"
                silent        = AudioSegment.silent(duration=3000)
                silent.export(str(output_dir / filename), format='mp3')
                print(f"📖 Saved silent chapter {chapter_num}: {filename}")

            elif element[0] == 'dialogue':
                speaker, text = element[1], element[2]
                print(f"🎤 Generating {speaker} voice...")
                filename    = f"{counter:03d}-{speaker}.wav"
                output_path = output_dir / filename
                success     = generate_chatterbox_audio(text, speaker, output_path)
                if not success:
                    print(f"❌ Failed: {filename}")

            counter += 1

        except Exception as e:
            print(f"Failed at element {counter}: {e}")
            import traceback; print(traceback.format_exc())
            continue


# ==================== MAIN EXECUTION ====================
if __name__ == "__main__":
    try:
        print("=" * 60)
        print("🎙️  Chatterbox TTS - Podcast Generator (OPTIMISED)")
        print(f"📂 Script directory: {SCRIPT_DIR}")
        print(f"📄 Input DOCX: {INPUT_DOCX}")
        print(f"📁 Output directory: {OUTPUT_DIR}")
        print("=" * 60)

        start_time = time.time()

        if not INPUT_DOCX.exists():
            print(f"❌ Input file not found: {INPUT_DOCX}")
            sys.exit(1)

        for speaker, path in VOICE_REFERENCES.items():
            if not path.exists():
                print(f"❌ Voice reference not found: {path}")
                print(f"   Please place {speaker}_voice.mp3 in: {CLONE_MODELS_DIR}")
                sys.exit(1)

        print("✅ All files verified")

        content_lines = read_docx(INPUT_DOCX)
        elements      = parse_content(content_lines)
        print(f"📋 Parsed {len(elements)} elements")

        chapters   = [(e[1], e[2]) for e in elements if e[0] == 'chapter']
        male_lines = sum(1 for e in elements if e[0] == 'dialogue' and e[1] == 'male')
        fem_lines  = sum(1 for e in elements if e[0] == 'dialogue' and e[1] == 'female')
        print(f"📋 {len(elements)} elements — chapters: {len(chapters)}, "
              f"male: {male_lines}, female: {fem_lines}")
        for num, title in chapters:
            print(f"   Ch{num}: {title}")
        print("=" * 60)

        generate_audio(elements, OUTPUT_DIR)

        elapsed_time = time.time() - start_time
        print("=" * 60)
        print("🎉 AUDIO GENERATION COMPLETE!")
        print(f"📁 Output files saved to: {OUTPUT_DIR}")
        print(f"⏱️  Total time: {elapsed_time:.2f}s ({elapsed_time/60:.2f}m)")
        print("=" * 60)

    except Exception as e:
        print(f"❌ Main process failed: {e}")
        import traceback; print(traceback.format_exc())
        sys.exit(1)
