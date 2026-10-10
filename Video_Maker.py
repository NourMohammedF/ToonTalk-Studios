import sys
import os
from pathlib import Path

# Get the script's directory and project root
SCRIPT_DIR   = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR

# Virtual environment path
VENV_PATH = PROJECT_ROOT / "libraries" / "video_maker"


def is_venv():
    return hasattr(sys, 'real_prefix') or (
        hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix
    )


if not is_venv():
    if sys.platform == "win32":
        python_exe = VENV_PATH / "Scripts" / "python.exe"
    else:
        python_exe = VENV_PATH / "bin" / "python"

    if python_exe.exists():
        print(f"🔄 Activating virtual environment: {VENV_PATH}")
        import subprocess
        result = subprocess.call([str(python_exe)] + sys.argv)
        sys.exit(result)
    else:
        print(f"❌ Virtual environment not found at: {VENV_PATH}")
        sys.exit(1)

from moviepy.config import change_settings

IMAGEMAGICK_BINARY = str(PROJECT_ROOT / "libraries" / "video_maker" / "ImageMagick-7.1.1-Q16-HDRI" / "magick.exe")
change_settings({"IMAGEMAGICK_BINARY": IMAGEMAGICK_BINARY})

import time
import random
import shutil
import subprocess
from functools import lru_cache
from docx import Document
from moviepy.editor import VideoFileClip, AudioFileClip
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright
import asyncio
from playwright.async_api import async_playwright
# ── OPTIMISATION: ThreadPoolExecutor for parallel chapter rendering ───────────
from concurrent.futures import ThreadPoolExecutor

if not hasattr(ImageDraw.ImageDraw, "textsize"):
    def textsize(self, text, font=None, *args, **kwargs):
        bbox = self.textbbox((0, 0), text, font=font, *args, **kwargs)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    ImageDraw.ImageDraw.textsize = textsize


# Path configurations
records_folder = PROJECT_ROOT / "Audio" / "output_audio" / "final"
# NOTE: both background_path and green_background_path previously pointed to the
# same file — collapsed to one constant.  If you have a separate background image
# for male clips, restore the two-variable pattern and the ternary below.
BACKGROUND_PATH = PROJECT_ROOT / "green_background.jpg"
output_folder   = PROJECT_ROOT / "Videos"
login_state_path    = PROJECT_ROOT / "login_state.json"
chapter_videos_path = PROJECT_ROOT / "Chapter Videos"
word_doc_path       = PROJECT_ROOT / "pod1.docx"

CHROME_USER_DATA = os.environ.get(
    "CHROME_USER_DATA",
    r"C:\Users\Nour\AppData\Local\Google\Chrome\User Data\Default"
)
CHROME_EXECUTABLE = os.environ.get(
    "CHROME_EXECUTABLE",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)

urls = {
    "male":   "https://new.express.adobe.com/tools/animate-from-audio?afaSize=YouTubeLandscape&afaPuppetId=Scribbles+White+VQA.puppet&afaPuppetCategory=artist-dave-werner&afaPuppetScale=0.75574&afaPuppetOffset=0.02941%2C0.07769",
    "female": "https://new.express.adobe.com/tools/animate-from-audio?afaSize=YouTubeLandscape&afaPuppetId=Eliza+VQA.puppet&afaPuppetCategory=artist-dave-werner&afaPuppetScale=0.75574&afaPuppetOffset=0.02941%2C0.07769"
}

os.makedirs(output_folder, exist_ok=True)

NUM_BROWSERS = 6


@lru_cache(maxsize=32)
def get_cached_font(font_name, font_size):
    try:
        return ImageFont.truetype(font_name, font_size)
    except Exception:
        try:
            return ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            return ImageFont.load_default()


def parse_chapter_titles(doc_path):
    doc      = Document(doc_path)
    chapters = {}
    for para in doc.paragraphs:
        text = para.text.strip()
        if text.startswith("Chapter "):
            chapter_part = text.split(":", 1)
            if len(chapter_part) == 2:
                chapter_num = chapter_part[0].split()[1]
                title = (chapter_part[1].split("\\")[0].split("\n")[0]
                         .split("Zoe:")[0].split("Max:")[0].strip())
                chapters[int(chapter_num)] = title
    return chapters


chapters_dict = parse_chapter_titles(word_doc_path)


def render_text_image(text, font_name, font_size, max_width,
                      color='white', bg_color=(0, 0, 0, 0), padding=20):
    font      = get_cached_font(font_name, font_size)
    words     = text.split()
    lines     = []
    cur_line  = ""
    dummy_img = Image.new("RGBA", (10, 10))
    draw      = ImageDraw.Draw(dummy_img)

    for w in words:
        test_line = (cur_line + " " + w).strip()
        w_width, _ = draw.textsize(test_line, font=font)
        if w_width + 2 * padding <= max_width or not cur_line:
            cur_line = test_line
        else:
            lines.append(cur_line)
            cur_line = w
    if cur_line:
        lines.append(cur_line)

    line_heights = [draw.textsize(line, font=font)[1] for line in lines] or [font_size]
    text_height  = sum(line_heights) + (len(lines) - 1) * 4
    text_width   = max([draw.textsize(line, font=font)[0] for line in lines]) if lines else 0

    img_w = text_width  + 2 * padding
    img_h = text_height + 2 * padding
    img   = Image.new("RGBA", (img_w, img_h), bg_color)
    draw  = ImageDraw.Draw(img)
    y     = padding
    for i, line in enumerate(lines):
        draw.text((padding, y), line, font=font, fill=color)
        y += line_heights[i] + 4
    return img


def get_video_codec():
    """Detect if NVIDIA GPU encoding is available, fallback to CPU"""
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=5
        )
        if 'h264_nvenc' in result.stdout:
            print("🚀 GPU encoding (h264_nvenc) available")
            return 'h264_nvenc'
    except Exception:
        pass
    print("💻 Using CPU encoding (libx264)")
    return 'libx264'


VIDEO_CODEC = get_video_codec()


def validate_video_file(video_path):
    try:
        result = subprocess.run(
            ['ffmpeg', '-v', 'error', '-i', str(video_path), '-f', 'null', '-'],
            capture_output=True, text=True, timeout=30
        )
        if "Error while decoding" in result.stderr or "Invalid data" in result.stderr:
            return False
        return True
    except Exception as e:
        print(f"⚠️ Validation error for {video_path}: {e}")
        return False


def process_chapter_audio(file_path, chapter_num, output_folder, valid_videos):
    """Process chapter audio — direct ffmpeg with codec-aware preset."""
    import numpy as np

    start_time  = time.time()
    
    if not valid_videos:
        raise Exception("No valid video files found in chapter videos directory.")

    selected_video = random.choice(valid_videos)
    video_path     = os.path.join(chapter_videos_path, selected_video)
    title          = chapters_dict.get(chapter_num, "Title Not Found")
    base_name      = os.path.splitext(os.path.basename(file_path))[0]
    output_path    = os.path.join(output_folder, f"{base_name}.mp4")
    temp_output    = output_path + ".tmp.mp4"

    FONT_OPTIONS = ['BRLNSB.TTF', 'SummerDash', 'SHOWG',
                    'Awesome-Autumn', 'Riskoda', 'BRLNSB.TTF',
                    'Howdy Lemon' , 'Harderdor-Black']

    try:
        selected_font = random.choice(FONT_OPTIONS)

        video_clip = VideoFileClip(video_path)
        audio_clip = AudioFileClip(file_path)
        final_duration          = min(video_clip.duration, audio_clip.duration)
        video_width, video_height = video_clip.size
        video_fps               = video_clip.fps or 24
        video_clip.close()
        audio_clip.close()

        max_text_width  = int(video_width * 0.8)
        chapter_pil_img = render_text_image(f'Chapter {chapter_num}', selected_font,
                                            82, max_text_width,
                                            color='white', bg_color=(0,0,0,0), padding=20)
        title_pil_img   = render_text_image(title, selected_font, 
                                            55, max_text_width,
                                            color='white', bg_color=(0,0,0,0), padding=10)

        chapter_png = os.path.join(output_folder, f"temp_chapter_{chapter_num}_{base_name}.png")
        title_png   = os.path.join(output_folder, f"temp_title_{chapter_num}_{base_name}.png")
        chapter_pil_img.save(chapter_png, "PNG")
        title_pil_img.save(title_png,   "PNG")

        chapter_y = int((video_height / 2 - chapter_pil_img.height / 2) - 100)
        chapter_x = int((video_width  - chapter_pil_img.width)  / 2)
        title_y   = int((video_height / 2 - title_pil_img.height / 2)   + 30)
        title_x   = int((video_width  - title_pil_img.width)    / 2)

        render_start = time.time()

        # ── OPTIMISATION: codec-aware preset ──────────────────────────────────
        # The original code always used -preset medium (a CPU libx264 preset).
        # When VIDEO_CODEC is h264_nvenc that preset is ignored and FFmpeg falls
        # back to the encoder's default — wasting every chapter render.
        # Now we pass the correct preset for whichever codec was detected.
        # ──────────────────────────────────────────────────────────────────────
        if VIDEO_CODEC == 'h264_nvenc':
            codec_params = ['-preset', 'p4', '-rc:v', 'vbr', '-cq', '23',
                            '-b:v', '5000k', '-pix_fmt', 'yuv420p']
        else:
            codec_params = ['-preset', 'fast', '-crf', '23',
                            '-b:v', '5000k', '-pix_fmt', 'yuv420p']

        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-i', str(video_path),
            '-i', str(file_path),
            '-i', chapter_png,
            '-i', title_png,
            '-filter_complex',
            f'[0:v]trim=0:{final_duration},setpts=PTS-STARTPTS[v0];'
            f'[v0][2:v]overlay={chapter_x}:{chapter_y}[v1];'
            f'[v1][3:v]overlay={title_x}:{title_y}[vout];'
            f'[1:a]atrim=0:{final_duration},asetpts=PTS-STARTPTS[aout]',
            '-map', '[vout]',
            '-map', '[aout]',
            '-c:v', VIDEO_CODEC,
            '-c:a', 'aac',
            '-b:a', '192k',
            '-r', str(video_fps),
        ] + codec_params + ['-shortest', temp_output]

        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=300)

        for tmp in [chapter_png, title_png]:
            try:
                os.remove(tmp)
            except Exception:
                pass

        if result.returncode != 0:
            print(f"❌ FFmpeg error: {result.stderr}")
            return False

        render_time = time.time() - render_start

        if os.path.exists(temp_output):
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception:
                    time.sleep(0.5)
                    os.remove(output_path)
            shutil.move(temp_output, output_path)
        else:
            print("⚠️ Warning: Output file not created")
            return False

        total_time = time.time() - start_time
        print(f"✅ Chapter video created: {output_path}")
        print(f"   ⏱️  Total: {total_time:.1f}s | Render: {render_time:.1f}s")
        return True

    except Exception as e:
        print(f"❌ Processing error: {str(e)}")
        import traceback; traceback.print_exc()
        for tmp in [temp_output,
                    os.path.join(output_folder, f"temp_chapter_{chapter_num}_{base_name}.png"),
                    os.path.join(output_folder, f"temp_title_{chapter_num}_{base_name}.png")]:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        return False


async def process_adobe_file_tab(context, job, sem):
    file_path, gender, background, url, login_state_path, output_folder = job
    async with sem:
        start_time  = time.time()
        attempt = 0

        while True:
            attempt += 1
            try:
                page = await context.new_page()
                page.set_default_timeout(120000)
                await page.goto(url, timeout=120000)
                await asyncio.sleep(5)

                try:
                    await page.locator('sp-tab[label="Background"]').click()
                    await asyncio.sleep(1)
                    bg_input = page.locator("qa-animate-from-audio-backgrounds-panel").locator("input[type='file']")
                    await bg_input.set_input_files(str(background))
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"⚠️ Background step error for {file_path}: {e}")

                audio_input = page.locator("qa-file-upload").locator("input[type='file']")
                await audio_input.set_input_files(str(file_path))
                await asyncio.sleep(2)

                start_poll    = asyncio.get_event_loop().time()
                last_progress = 0
                stall_count   = 0
                max_stalls    = 10
                timeout       = 300

                while asyncio.get_event_loop().time() - start_poll < timeout:
                    try:
                        val = await page.locator("sp-progress-bar").get_attribute("aria-valuenow")
                        if val:
                            current_progress = int(val)
                            if current_progress == last_progress:
                                stall_count += 1
                                if stall_count >= max_stalls:
                                    print(f"⚠️ Progress stalled at {current_progress}%")
                            else:
                                stall_count   = 0
                                last_progress = current_progress
                            if current_progress >= 25:
                                break
                    except Exception:
                        pass
                    await asyncio.sleep(1.5)

                await asyncio.sleep(3)

                try:
                    async with page.expect_download(timeout=120000) as download_info:
                        await page.locator('sp-button[data-testid="qa-download-export-button"]').click()
                    download    = await download_info.value
                    output_path = os.path.join(output_folder,
                                               f"{os.path.splitext(os.path.basename(file_path))[0]}.mp4")
                    await download.save_as(output_path)

                    total_time = time.time() - start_time
                    print(f"✅ Video saved: {output_path}")
                    print(f"   ⏱️  Total time: {total_time:.1f}s")

                    try:
                        os.remove(file_path)
                    except Exception:
                        pass

                    await page.close()
                    return (file_path, True, f"Saved to {output_path} ({total_time:.1f}s)")

                except Exception as download_error:
                    print(f"⚠️ Download error (attempt {attempt}): {download_error}")
                    await page.close()
                    print(f"🔄 Retrying {os.path.basename(file_path)}...")
                    await asyncio.sleep(5)
                    continue

            except Exception as e:
                print(f"❌ Error (attempt {attempt}): {e}")
                try:
                    await page.close()
                except Exception:
                    pass
                print(f"🔄 Retrying {os.path.basename(file_path)}...")
                await asyncio.sleep(5)
                continue


async def run_adobe_jobs_async(adobe_jobs):
    sem = asyncio.Semaphore(NUM_BROWSERS)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=CHROME_USER_DATA,
            headless=False,
            executable_path=CHROME_EXECUTABLE
        )
        print(f"🌐 Running {len(adobe_jobs)} Adobe jobs on up to {NUM_BROWSERS} tabs...")
        tasks   = [process_adobe_file_tab(context, job, sem) for job in adobe_jobs]
        results = await asyncio.gather(*tasks)
        await context.close()
        return results


def process_audio_files():
    overall_start = time.time()

    with sync_playwright() as p:
        if not os.path.exists(login_state_path):
            print("No login state found. Opening browser for manual login...")
            context    = p.chromium.launch_persistent_context(
                user_data_dir=CHROME_USER_DATA, headless=False,
                executable_path=CHROME_EXECUTABLE
            )
            login_page = context.new_page()
            login_page.goto("https://account.adobe.com/")
            print("🔒 Please log in manually within 5 minutes...")
            time.sleep(300)
            context.storage_state(path=str(login_state_path))
            print("✅ Login state saved.")
            login_page.close()
            context.close()
        else:
            print("✅ Using cached login state.")

    chapter_jobs = []
    adobe_jobs   = []

    for file in os.listdir(records_folder):
        file_path = os.path.join(records_folder, file)
        if not file.lower().endswith(('.wav', '.mp3')):
            continue

        if "chapter" in file.lower():
            try:
                chapter_num = int(file.split("-")[2].split(".")[0])
                print(f"📄 Queued chapter audio: {file} (Chapter {chapter_num})")
                chapter_jobs.append((file_path, chapter_num))
            except Exception as e:
                print(f"❌ Chapter parsing error: {e}")
            continue

        gender = ("female" if "female" in file.lower()
                  else "male" if "male" in file.lower()
                  else "unknown")
        url        = urls.get(gender, urls["male"])
        background = BACKGROUND_PATH
        print(f"📄 Queued Adobe Express file: {file} ({gender})")
        adobe_jobs.append((str(file_path), gender, str(background), url,
                           str(login_state_path), str(output_folder)))

    adobe_start = time.time()
    success_count = failure_count = 0
    if adobe_jobs:
        results = asyncio.run(run_adobe_jobs_async(adobe_jobs))
        for file_path, success, message in results:
            if success:
                print(f"✅ Adobe job success: {os.path.basename(file_path)} -> {message}")
                success_count += 1
            else:
                print(f"❌ Adobe job failed: {os.path.basename(file_path)} -> {message}")
                failure_count += 1
    adobe_time = time.time() - adobe_start

    chapter_start = time.time()
    if chapter_jobs:
        # FIX: Scan and validate template videos ONCE sequentially here before threading
        video_files = [f for f in os.listdir(chapter_videos_path)
                       if f.endswith(('.mp4', '.mov', '.avi'))]
        if not video_files:
            print("❌ No video files found in chapter videos directory.")
            valid_videos = []
        else:
            print(f"🔍 Validating {len(video_files)} template video files once before parallel rendering...")
            valid_videos = [vf for vf in video_files
                            if validate_video_file(os.path.join(chapter_videos_path, vf))]

        max_ch_workers = min(len(chapter_jobs), 4)
        print(f"🎬 Parallel rendering for {len(chapter_jobs)} chapter(s) "
              f"({max_ch_workers} workers)...")

        def _process(args):
            fp, cn, vv = args
            print(f"📄 Processing chapter audio: {os.path.basename(fp)} (Chapter {cn})")
            ok = process_chapter_audio(fp, cn, str(output_folder), vv)
            if ok:
                try:
                    os.remove(fp)
                except Exception:
                    pass
            else:
                print(f"⚠️ Failed to process chapter file: {os.path.basename(fp)}")

        with ThreadPoolExecutor(max_workers=max_ch_workers) as pool:
            # Deliver the pre-computed valid_videos list to each thread task tuple
            list(pool.map(_process, [(fp, cn, valid_videos) for fp, cn in chapter_jobs]))

    chapter_time = time.time() - chapter_start
    total_time   = time.time() - overall_start

    print("\n" + "=" * 60)
    print("🎉 All processing completed! 📊 Performance Summary:")
    if adobe_jobs:
        print(f"   • Adobe jobs: {success_count}/{len(adobe_jobs)} successful in {adobe_time:.1f}s")
        print(f"   • Adobe failures: {failure_count}")
    print(f"   • Chapter jobs: {len(chapter_jobs)} files in {chapter_time:.1f}s")
    print(f"   • Total time: {total_time:.1f}s ({total_time/60:.1f} minutes)")
    if adobe_jobs and success_count > 0:
        print(f"   • Avg per successful Adobe job: {adobe_time/success_count:.1f}s")
    if chapter_jobs:
        print(f"   • Avg per chapter: {chapter_time/len(chapter_jobs):.1f}s")
    print("=" * 60)


if __name__ == '__main__':
    process_audio_files()
    