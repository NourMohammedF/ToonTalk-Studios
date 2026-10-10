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
# NOTE: the Adobe Express URLs below (afaBackgroundColor=%230afe00) already bake
# a green-screen background into the tool itself, so no background image ever
# needs to be uploaded — the old "Background" tab / file-input flow is gone.
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
    "male":   "https://new.express.adobe.com/tools/animate-from-audio?afaBackgroundColor=%230afe00&afaBackgroundCategory=all&afaSize=YouTubeLandscape&afaPuppetId=Scribbles+White+VQA.puppet&afaPuppetCategory=artist-dave-werner&afaPuppetScale=0.75574&afaPuppetOffset=0.02941%2C0.07769",
    "female": "https://new.express.adobe.com/tools/animate-from-audio?afaBackgroundColor=%230afe00&afaBackgroundCategory=all&afaSize=YouTubeLandscape&afaPuppetId=Eliza+VQA.puppet&afaPuppetCategory=artist-dave-werner&afaPuppetScale=0.75574&afaPuppetOffset=0.02941%2C0.07769"
}

os.makedirs(output_folder, exist_ok=True)

# 2 tabs per gender = 4 tabs total, opened once in Stage 1 and reused for every job
NUM_TABS_PER_GENDER = 2


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
    _word_to_int = {
        "one":1,"two":2,"three":3,"four":4,"five":5,
        "six":6,"seven":7,"eight":8,"nine":9,"ten":10,
    }
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
                key = int(chapter_num) if chapter_num.isdigit() \
                      else _word_to_int.get(chapter_num.lower(), len(chapters) + 1)
                chapters[key] = title
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


async def open_and_prepare_tab(context, gender, url, index):
    """
    STAGE 1 (preparation): open one tab, navigate to the tool, and verify the
    editor UI actually loaded. The green-screen background is already baked
    into the URL (afaBackgroundColor=...), so there is nothing to upload here.
    """
    page = await context.new_page()
    page.set_default_timeout(180000)
    try:
        await page.goto(url, timeout=180000)
        await asyncio.sleep(8)

        # Verify the editor actually finished loading before we trust this tab.
        # The page can be slow to render, so give it a generous 2 minutes.
        await page.locator("qa-file-upload").locator("input[type='file']").wait_for(
            state="attached", timeout=120000
        )

        print(f"✅ Tab ready: {gender} #{index + 1}")
        return page

    except Exception as e:
        print(f"❌ Tab failed to load ({gender} tab #{index + 1}): {e}")
        try:
            await page.close()
        except Exception:
            pass
        return None


async def prepare_tab_pool(context):
    """
    STAGE 1 (preparation): open NUM_TABS_PER_GENDER tabs for each gender
    and confirm every one loaded successfully. Self-heals forever — a tab
    that fails to load is retried with a growing backoff (capped at 30s)
    indefinitely rather than being given up on, since a permanently-missing
    slot would starve that gender's whole job queue for the rest of the run.
    """
    pool = {"male": [], "female": []}

    for gender, url in urls.items():
        for i in range(NUM_TABS_PER_GENDER):
            page   = None
            retry  = 0
            while not page:
                page = await open_and_prepare_tab(context, gender, url, i)
                if page:
                    break
                retry  += 1
                backoff = min(10 * retry, 90)
                print(f"🔄 Retrying tab load: {gender} #{i + 1} "
                      f"(attempt {retry + 1}, waiting {backoff}s)...")
                await asyncio.sleep(backoff)
            pool[gender].append(page)

    ready = len(pool["male"]) + len(pool["female"])
    total = NUM_TABS_PER_GENDER * 2
    print(f"🌐 Stage 1 complete: {ready}/{total} tabs ready "
          f"(male={len(pool['male'])}, female={len(pool['female'])})")
    return pool


async def click_until_effect(page, click_locator, verify_locator, description,
                             retries=8, click_timeout=20000, verify_timeout=20000,
                             verify_state="visible", pre_click_delay=5):
    """
    Click click_locator, then confirm verify_locator actually shows up as a
    result — and if it doesn't, RE-CLICK rather than just re-waiting.
    This is the key difference from a plain wait_for: sometimes Playwright's
    click() succeeds (the element was visible/enabled/stable) but the app
    itself doesn't respond to it — the click "lands" with no effect. A single
    long wait can't recover from that; only clicking again can.

    verify_state defaults to "visible" (right for buttons/links), but pass
    "attached" when verifying something like a file <input> that's
    intentionally hidden behind a styled upload button — it will never
    become "visible" even when everything worked correctly.

    pre_click_delay gives the page a moment to settle after the previous
    step before each click attempt, in case a click right after a UI
    transition doesn't register.
    """
    last_error = None
    for attempt in range(retries):
        try:
            await asyncio.sleep(pre_click_delay)
            await click_locator.wait_for(state="visible", timeout=click_timeout)
            await click_locator.click(timeout=click_timeout)
            await verify_locator.wait_for(state=verify_state, timeout=verify_timeout)
            return
        except Exception as e:
            last_error = e
            print(f"⚠️ {description} click had no effect (attempt {attempt + 1}/{retries}): {e}")
            await asyncio.sleep(1.5)

    raise RuntimeError(f"{description} never took effect after {retries} attempts: {last_error}")


async def reset_tab_for_next_audio(page):
    """
    STAGE 3 (reuse): Adobe Express changed the post-download flow. There is
    no more "Back to edit" button — after a successful download, a toast
    notification appears ("Your file has been saved to your computer and in
    Your stuff.") with a single "Animate another video" action button. That
    button takes the page straight back to the same start interface Stage 2
    needs (record/browse audio upload), so a single click replaces the old
    two-step Back -> Replace audio sequence.

    The click is verified and re-clicked (not just re-waited) if it doesn't
    produce its expected result, so a silently-ignored click surfaces and
    self-heals in seconds instead of eating a full timeout before the
    caller's much heavier full-page-reload fallback ever kicks in.
    """
    animate_another_button = page.locator(
        'sp-button[data-testid="toast-action-btn"]', has_text="Animate another video"
    )
    audio_input = page.locator("qa-file-upload").locator("input[type='file']")

    await click_until_effect(page, animate_another_button, audio_input,
                             "Animate another video toast button",
                             verify_state="attached", verify_timeout=30000)


class DownloadThrottle:
    """
    Shared across every tab worker so downloads triggered by *different*
    tabs are always spaced at least min_interval seconds apart — not just
    within one tab's own sequence of jobs. Prevents two tabs from clicking
    Download at nearly the same moment.
    """
    def __init__(self, min_interval=5):
        self.lock          = asyncio.Lock()
        self.min_interval   = min_interval
        self.last_download  = 0.0

    async def wait_turn(self):
        async with self.lock:
            now     = asyncio.get_event_loop().time()
            elapsed = now - self.last_download
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self.last_download = asyncio.get_event_loop().time()


async def download_with_retry(page, file_path, output_folder, throttle,
                              max_wait_seconds=300, per_click_timeout=60000):
    """
    Click Download and wait for the browser download event — re-clicking if
    a click doesn't actually trigger a download, instead of one long wait
    that either eventually fires or times out with nothing to show for it.
    Waits its turn on the shared throttle first so this download doesn't
    land within min_interval seconds of another tab's download.

    Keeps retrying for up to max_wait_seconds (5 minutes) total — the page
    can take a while to finish rendering the export. If it's still failing
    after that, the page itself is treated as the problem — this raises,
    and the caller (tab_worker) reloads/reprepares the tab instead of just
    re-clicking forever.
    """
    download_button = page.locator('sp-button[data-testid="qa-download-export-button"]')
    last_error = None
    start = time.time()

    await throttle.wait_turn()

    while time.time() - start < max_wait_seconds:
        try:
            await download_button.wait_for(state="visible", timeout=120000)
            async with page.expect_download(timeout=per_click_timeout) as download_info:
                await download_button.click(timeout=15000)
            download    = await download_info.value
            output_path = os.path.join(output_folder,
                                       f"{os.path.splitext(os.path.basename(file_path))[0]}.mp4")
            await download.save_as(output_path)
            return output_path
        except Exception as e:
            last_error = e
            remaining = max_wait_seconds - (time.time() - start)
            if remaining <= 0:
                break
            await asyncio.sleep(min(3, remaining))

    raise RuntimeError(
        f"Download never triggered after {max_wait_seconds}s — page needs reload: {last_error}"
    )


async def run_audio_job(page, file_path, output_folder, throttle):
    """
    Core upload -> process -> download logic, shared by Stage 2 (first run
    on a tab) and Stage 3 (every reused run on that same tab). The page is
    assumed to already be in an audio-upload-ready state.
    """
    start_time = time.time()

    audio_input = page.locator("qa-file-upload").locator("input[type='file']")
    await audio_input.set_input_files(str(file_path))
    await asyncio.sleep(4)

    # Fail fast if the upload never actually started processing, instead of
    # silently burning the full download-button timeout for nothing.
    try:
        await page.locator("sp-progress-bar").wait_for(state="attached", timeout=30000)
    except Exception:
        raise RuntimeError("Audio upload never started processing (no progress bar appeared)")

    output_path = await download_with_retry(page, file_path, output_folder, throttle)

    total_time = time.time() - start_time
    print(f"✅ Video saved: {output_path}")
    print(f"   ⏱️  Total time: {total_time:.1f}s")

    # Audio file is intentionally kept (not deleted) so the end-of-run
    # completeness check can always re-derive which audio files still
    # need a video, and so a file can safely be re-rendered if needed.
    return output_path, total_time


async def tab_worker(context, gender, url, worker_index, page,
                     job_queue, output_folder, throttle):
    """
    Owns one persistent tab for the life of the run. Pulls files for its
    gender off the shared queue one at a time:
      - first job  -> STAGE 2, run straight on the already-loaded tab
      - every job after that -> STAGE 3, reset via the "Animate another
        video" toast button first
    If something goes wrong mid-job, falls back to reloading that single tab
    from scratch (closest equivalent to the old close/reopen behaviour) so
    one bad job can't permanently wedge a worker.

    Self-heals forever: there is no attempt cap on a given file. A job that
    keeps failing is retried indefinitely (with a capped backoff) rather than
    being marked as a permanent failure — the end-of-run completeness check
    is the only thing that ultimately decides a file is "done".
    """
    results   = []
    first_job = True

    while True:
        try:
            file_path = job_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        attempt = 0
        while True:
            attempt += 1
            try:
                if not first_job:
                    await reset_tab_for_next_audio(page)

                output_path, total_time = await run_audio_job(page, file_path, output_folder, throttle)
                results.append((file_path, True, f"Saved to {output_path} ({total_time:.1f}s)"))
                first_job = False
                break

            except Exception as e:
                print(f"❌ Error on {gender} tab #{worker_index + 1} "
                      f"(attempt {attempt}) for {os.path.basename(file_path)}: {e}")

                backoff = min(10 * attempt, 90)
                print(f"🔄 Retrying {os.path.basename(file_path)} in {backoff}s "
                      f"(attempt {attempt + 1})...")
                await asyncio.sleep(backoff)

                # Recovery: this single tab got itself into a bad state —
                # reload it from scratch rather than losing the whole worker.
                # The green-screen background is baked into `url`, so the
                # reload needs nothing re-applied — just the fresh page.
                try:
                    await page.goto(url, timeout=180000)
                    await asyncio.sleep(8)
                    await page.locator("qa-file-upload").locator("input[type='file']").wait_for(
                        state="attached", timeout=120000
                    )
                    first_job = True  # next attempt goes straight to Stage 2, no toast click needed
                except Exception as reload_e:
                    print(f"❌ Tab reload also failed, will keep retrying: {reload_e}")
                continue

    return results


async def run_adobe_jobs_async(adobe_jobs):
    if not adobe_jobs:
        return []

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=CHROME_USER_DATA,
            headless=False,
            executable_path=CHROME_EXECUTABLE
        )

        # ── STAGE 1: open and verify all tabs up front ──────────────────────
        print(f"🌐 Stage 1: preparing {NUM_TABS_PER_GENDER * 2} tabs "
              f"({NUM_TABS_PER_GENDER} male + {NUM_TABS_PER_GENDER} female)...")
        pool = await prepare_tab_pool(context)

        # Split jobs into per-gender queues so each tab only ever handles
        # audio that matches the puppet it already has loaded
        job_queues = {"male": asyncio.Queue(), "female": asyncio.Queue()}
        for file_path, gender, _url, _login, _out in adobe_jobs:
            g = gender if gender in job_queues else "male"
            job_queues[g].put_nowait(file_path)

        for gender in job_queues:
            if job_queues[gender].qsize() and not pool[gender]:
                print(f"⚠️ {job_queues[gender].qsize()} {gender} job(s) queued "
                      f"but no {gender} tabs loaded successfully — they will be skipped.")

        # ── STAGE 2 + 3: process every job, reusing each tab after its first run ──
        print(f"🎬 Stage 2/3: processing {len(adobe_jobs)} job(s) across "
              f"{len(pool['male']) + len(pool['female'])} ready tab(s)...")

        # Shared across all tabs so downloads never fire within 5s of each other
        throttle = DownloadThrottle(min_interval=5)

        worker_tasks = []
        for gender, pages in pool.items():
            url = urls[gender]
            for idx, page in enumerate(pages):
                worker_tasks.append(
                    tab_worker(context, gender, url, idx, page,
                              job_queues[gender], output_folder, throttle)
                )

        all_results = await asyncio.gather(*worker_tasks) if worker_tasks else []
        results = [r for group in all_results for r in group]

        for pages in pool.values():
            for page in pages:
                try:
                    await page.close()
                except Exception:
                    pass

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
        url = urls.get(gender, urls["male"])
        print(f"📄 Queued Adobe Express file: {file} ({gender})")
        adobe_jobs.append((str(file_path), gender, url,
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

    def _process_chapter(args):
        fp, cn, vv = args
        print(f"📄 Processing chapter audio: {os.path.basename(fp)} (Chapter {cn})")
        ok = process_chapter_audio(fp, cn, str(output_folder), vv)
        # Audio file is intentionally kept (not deleted) — the end-of-run
        # completeness check below relies on it still being on disk to know
        # whether a video still needs remaking.
        if not ok:
            print(f"⚠️ Failed to process chapter file: {os.path.basename(fp)}")
        return ok

    chapter_start = time.time()
    valid_videos  = []
    if chapter_jobs:
        # FIX: Scan and validate template videos ONCE sequentially here before threading
        video_files = [f for f in os.listdir(chapter_videos_path)
                       if f.endswith(('.mp4', '.mov', '.avi'))]
        if not video_files:
            print("❌ No video files found in chapter videos directory.")
        else:
            print(f"🔍 Validating {len(video_files)} template video files once before parallel rendering...")
            valid_videos = [vf for vf in video_files
                            if validate_video_file(os.path.join(chapter_videos_path, vf))]

        max_ch_workers = min(len(chapter_jobs), 4)
        print(f"🎬 Parallel rendering for {len(chapter_jobs)} chapter(s) "
              f"({max_ch_workers} workers)...")

        with ThreadPoolExecutor(max_workers=max_ch_workers) as pool:
            # Deliver the pre-computed valid_videos list to each thread task tuple
            list(pool.map(_process_chapter, [(fp, cn, valid_videos) for fp, cn in chapter_jobs]))

    chapter_time = time.time() - chapter_start

    # ── END-OF-RENDER COMPLETENESS CHECK ────────────────────────────────────
    # Now that audio files are never deleted, we can always tell which ones
    # still don't have a matching video and just keep remaking those — self
    # heals forever, no attempt cap, until every audio file has produced a
    # video (or the process is stopped manually).
    def _expected_output(file_path):
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        return os.path.join(output_folder, f"{base_name}.mp4")

    def _is_missing(file_path):
        out = _expected_output(file_path)
        return not os.path.exists(out) or os.path.getsize(out) == 0

    heal_round = 0
    if adobe_jobs or chapter_jobs:
        print("\n🔍 Running end-of-render completeness check...")
        while True:
            missing_adobe   = [job for job in adobe_jobs if _is_missing(job[0])]
            missing_chapter = [(fp, cn) for fp, cn in chapter_jobs if _is_missing(fp)]

            if not missing_adobe and not missing_chapter:
                if heal_round:
                    print(f"✅ Completeness check passed after {heal_round} heal round(s) — "
                          f"every audio file has a matching video.")
                else:
                    print("✅ Completeness check passed — every audio file has a matching video.")
                break

            heal_round += 1
            print(f"⚠️ Completeness round {heal_round}: {len(missing_adobe)} Adobe + "
                  f"{len(missing_chapter)} chapter file(s) still missing a video — remaking them...")

            if missing_adobe:
                redo_results = asyncio.run(run_adobe_jobs_async(missing_adobe))
                for file_path, success, message in redo_results:
                    if success:
                        print(f"✅ Re-render success: {os.path.basename(file_path)} -> {message}")
                        success_count += 1
                        failure_count = max(0, failure_count - 1)
                    else:
                        print(f"❌ Re-render still failing: {os.path.basename(file_path)} -> {message}")

            if missing_chapter:
                with ThreadPoolExecutor(max_workers=min(len(missing_chapter), 4)) as pool:
                    list(pool.map(_process_chapter, [(fp, cn, valid_videos) for fp, cn in missing_chapter]))

            backoff = min(5 * heal_round, 60)
            print(f"⏳ Waiting {backoff}s before the next completeness round...")
            time.sleep(backoff)

    total_time = time.time() - overall_start

    print("\n" + "=" * 60)
    print("🎉 All processing completed! 📊 Performance Summary:")
    if adobe_jobs:
        print(f"   • Adobe jobs: {success_count}/{len(adobe_jobs)} successful in {adobe_time:.1f}s")
        print(f"   • Adobe failures: {failure_count}")
    print(f"   • Chapter jobs: {len(chapter_jobs)} files in {chapter_time:.1f}s")
    if heal_round:
        print(f"   • Completeness heal rounds needed: {heal_round}")
    print(f"   • Total time: {total_time:.1f}s ({total_time/60:.1f} minutes)")
    if adobe_jobs and success_count > 0:
        print(f"   • Avg per successful Adobe job: {adobe_time/success_count:.1f}s")
    if chapter_jobs:
        print(f"   • Avg per chapter: {chapter_time/len(chapter_jobs):.1f}s")
    print("=" * 60)


if __name__ == '__main__':
    process_audio_files()