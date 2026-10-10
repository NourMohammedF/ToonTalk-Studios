r"""
trend_idea.py

Scans a wide spread of trend sources (Google Trends, Google News, X/Twitter,
and both general + niche Reddit) for what's hot right now, asks the NIM
"viral" model (already configured in your json) to pick or synthesize the
single best video idea — across ANY niche (psychology, true crime, tech,
finance, relationships, life hacks, weird facts, drama, etc.), whichever is
most likely to go viral — and writes ONLY a "video_idea" key into your config
file.

The write is done by editing the raw file TEXT directly (not by re-dumping
the whole JSON object), so every other key, every blank line, and every
hand-aligned space in the rest of the file is left byte-for-byte untouched.

Usage:
    cd to this script's folder, then:
        python trend_idea.py

Reads/writes ".\upload.config.json" next to this script (hardcoded).

First run will auto-create/activate a venv at .\libraries\Promoter if one
doesn't exist yet (instructions are printed if it's missing).
"""

import os
import sys
import subprocess
from pathlib import Path

# ==================== VIRTUAL ENV AUTO-ACTIVATION ====================
SCRIPT_DIR = Path(__file__).parent.resolve()
VENV_PATH  = SCRIPT_DIR / "libraries" / "Promoter"

venv_activated = "Promoter" in sys.prefix or "VENV_ACTIVATED" in os.environ

if not venv_activated:
    print("🔄 Activating virtual environment...")
    os.environ["VENV_ACTIVATED"] = "1"

    if os.name == "nt":
        venv_python = VENV_PATH / "Scripts" / "python.exe"
    else:
        venv_python = VENV_PATH / "bin" / "python"

    if venv_python.exists():
        print(f"✓ Using Python: {venv_python}")
        # Forward any CLI args (e.g. a custom config path) to the relaunch
        subprocess.call([str(venv_python), str(Path(__file__).resolve())] + sys.argv[1:])
    else:
        print(f"❌ Virtual environment not found at : {VENV_PATH}")
        print(f"❌ Expected Python at               : {venv_python}")
        print()
        print("Run these commands first:")
        print(f'  cd "{SCRIPT_DIR}"')
        print(f'  python -m venv libraries\\Promoter')
        print(f'  libraries\\Promoter\\Scripts\\activate')
        print(f'  pip install requests praw pytrends')
    sys.exit()
# =======================================================================

import json
import re
import random
import importlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


def ensure_import(module_name, pip_name=None):
    """Import module_name in the interpreter that is ACTUALLY running this
    process right now (sys.executable), auto pip-installing pip_name (or
    module_name) into that same interpreter if it's missing.

    Using `sys.executable -m pip` (rather than relying on a bare `pip` /
    `pip.exe` on PATH) matters on Windows: if a venv folder was ever
    copied/renamed, Scripts\\pip.exe can keep pointing at the OLD location
    it was created in and silently install there instead. Going through
    `-m pip` always targets whatever interpreter is currently running.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError:
        pkg = pip_name or module_name
        print(f"📦 '{module_name}' not found in this interpreter — installing {pkg}...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", pkg])
        except Exception as e:
            print(f"⚠️  Could not auto-install {pkg}: {e}")
            return None
        try:
            return importlib.import_module(module_name)
        except ImportError:
            return None


requests = ensure_import("requests")
if requests is None:
    print("❌ 'requests' is required and could not be installed automatically.")
    print(f"   Fix manually:  \"{sys.executable}\" -m pip install requests")
    sys.exit(1)

praw = ensure_import("praw")

# pytrends is no longer used — Google RSS feeds are scraped directly instead.


# Config file lives right next to this script.
CONFIG_PATH = SCRIPT_DIR / "upload.config.json"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Subreddits worth checking across MANY niches, not just one — the goal is
# to surface whichever story/topic is most viral overall, regardless of
# subject. Edit this list to add/remove niches; it is NOT read from the
# config file, so changing it never touches upload.config.json.
SUBREDDITS = [
    # confessions & general drama
    "AmItheAsshole", "AITAH", "confession", "confessions", "TrueOffMyChest",
    "offmychest", "tifu", "nosleep", "LetsNotMeet", "AmItheButtface", "AmItheAngel",
    # relationships & family
    "relationship_advice", "relationships", "dating_advice", "Marriage",
    "survivinginfidelity", "JUSTNOMIL", "JustNoFamily", "raisedbynarcissists",
    "EntitledParents",
    # revenge & malicious compliance
    "MaliciousCompliance", "pettyrevenge", "ProRevenge", "NuclearRevenge",
    # workplace
    "antiwork", "workreform", "talesfromtechsupport", "TalesFromRetail",
    "IDontWorkHereLady",
    # story/update aggregators
    "BestofRedditorUpdates", "UpdateMe",
    # true crime & mystery
    "UnresolvedMysteries", "TrueCrime", "RBI", "conspiracy",
    # money & finance
    "personalfinance", "FinancialIndependence", "wallstreetbets", "investing",
    "povertyfinance",
    # tech & AI
    "technology", "artificial", "Futurology", "gadgets",
    # health, fitness & self-improvement
    "GetMotivated", "loseit", "fitness", "productivity", "DecidingToBeBetter",
    # science & weird facts
    "todayilearned", "interestingasfuck", "science", "space",
    # gaming & internet culture
    "gaming", "OutOfTheLoop", "LivestreamFail",
    # life hacks & DIY
    "LifeProTips", "DIY", "Frugal",
]

# ==================== CREATIVITY CONSTANTS ====================
# Each run randomly picks one angle + one title format so the model
# is forced into a different creative lane instead of always defaulting
# to the same "The Psychology Behind Why People…" template.

CREATIVE_ANGLES = [
    # ── Psychology & cognition (one lane among many, not the default) ──
    "lead with a counterintuitive fact that flips a common assumption",
    "expose a cognitive bias or dark pattern the audience hasn't named yet",
    "use the sunk-cost fallacy or effort-justification bias as the lens for the trend",
    "expose a hidden manipulation tactic embedded in the trending event",
    "anchor it to attachment theory, trauma responses, or childhood conditioning",
    "frame it through group psychology, mob mentality, or social contagion",
    "frame it around an evolutionary psychology or survival-instinct explanation",
    "reveal what peer-reviewed science actually says vs. what most people believe",

    # ── True crime & mystery ────────────────────────────────────────────
    "frame it as an unsolved mystery with one detail that doesn't add up",
    "walk through the timeline of events like an investigator building a case",
    "reveal the one overlooked clue that changes how the whole story reads",

    # ── Money, business & careers ───────────────────────────────────────
    "break down the exact numbers behind a trending financial decision or scam",
    "expose a business or industry trick most people don't realise they're paying for",
    "frame it as a cautionary tale about money — what went wrong and the cost",
    "reveal an under-the-radar opportunity everyone is missing right now",

    # ── Tech, science & how-things-work ─────────────────────────────────
    "explain the surprising mechanism behind a trending piece of tech or science news",
    "predict where a trending technology or discovery leads next, and why it matters",
    "compare two competing trending products/approaches and declare a clear winner",

    # ── Life hacks, self-improvement & challenges ───────────────────────
    "frame it as a simple change that produces a disproportionately large result",
    "structure it as a 30-day or X-day challenge/transformation built on the trend",
    "expose the common mistake almost everyone makes, and the fix",

    # ── Drama, relationships & human stories ────────────────────────────
    "tell it as a true story with a twist ending revealed at the very end",
    "frame it around the psychology of revenge, betrayal, or justice — what drives it",
    "use a villain-vs-underdog framing pulled from the trending story",
    "reveal why people stay in a clearly bad situation, and what finally changes it",

    # ── Culture, trends & internet moments ──────────────────────────────
    "connect two seemingly unrelated trending topics through one shared mechanism",
    "explain why this specific story is spreading so fast right now",
    "take a contrarian stance and defend the unpopular side of a trending debate",
    "frame it as a myth-bust — everything people believe about this is wrong",
    "use a countdown/ranking structure built from the trending data",

    # ── Curiosity & shock value ─────────────────────────────────────────
    "lead with a shocking statistic or fact most viewers have never heard",
    "frame it as a before/after reveal tied to the trending topic",
    "use a cliffhanger open — withhold the payoff until late in the video",
]

TITLE_FORMATS = [
    # ── Classic viral structures ───────────────────────────────────────
    "question hook      (e.g. 'Why Is Everyone Suddenly Talking About This?')",
    "numbered list      (e.g. '7 Signs You Are Making This Mistake Without Knowing It')",
    "reveal hook        (e.g. 'The Real Reason Nobody Talks About This Until Now')",
    "warning hook       (e.g. 'Stop Ignoring This Before It Costs You')",
    "challenge hook     (e.g. 'Almost Everyone Has This Completely Backwards')",
    "story hook         (e.g. 'Here Is Exactly What Happened, Step By Step')",
    "dark truth         (e.g. 'The Uncomfortable Truth Behind Why This Keeps Happening')",
    "science/data reveal(e.g. 'The Numbers Behind This Trend Are Genuinely Disturbing')",
    # ── Identity and self-recognition ─────────────────────────────────
    "identity threat    (e.g. 'If You Do This Regularly, You Might Be...')",
    "direct reframe     (e.g. 'It Is Not Bad Luck. This Is What Is Actually Happening')",
    "empathy hook       (e.g. 'This Is Why It Is So Hard To Walk Away, And It Is Not Weakness')",
    "secret society     (e.g. 'Only People Who Have Been Through This Will Truly Understand')",
    "self-test hook     (e.g. 'How Many of These Apply To You? The Answer Says A Lot')",
    # ── Myth-busting and expert insider ───────────────────────────────
    "myth-bust          (e.g. 'Everything You Think You Know About This Is Wrong')",
    "insider reveal     (e.g. 'Insiders Know This, But Most People Never Find Out')",
    "before/after       (e.g. 'What Happens After 30 Days of Doing This')",
    "data deep dive     (e.g. 'I Went Through 50 Cases On This. The Results Are Unsettling')",
    # ── Story and case study ──────────────────────────────────────────
    "true case study    (e.g. 'The True Story That Finally Explains Why This Happens')",
    "historical mirror  (e.g. 'This Happened Before in History, And Here Is What It Tells Us')",
    "cliffhanger open   (e.g. 'Nobody Expected What Happened After This')",
    "confession frame   (e.g. 'I Made This Same Mistake Until I Learned The Truth')",
    # ── Consequence and time pressure ─────────────────────────────────
    "consequence hook   (e.g. 'Most People Do Not Realise What This Is Actually Costing Them')",
    "time hook          (e.g. 'After Just 30 Days, This Changes Something Alarming')",
    "comparison hook    (e.g. 'People Who Do X vs People Who Do Y — The Real Difference')",
    "prediction hook    (e.g. 'This Will Change How You Think About Every Choice You Make')",
]


# ==================== TREND SOURCES ====================

def get_google_trends_daily():
    """Google's daily trending searches — the old /trendingsearches/daily/rss endpoint
    was retired by Google; this uses the current unified trending RSS instead."""
    try:
        resp = requests.get(
            "https://trends.google.com/trending/rss?geo=US&hours=48",
            headers=UA, timeout=15,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        titles = [item.findtext("title") for item in root.findall(".//item")]
        return [t for t in titles if t][:20]
    except Exception as e:
        print(f"Google Trends (daily) fetch failed: {e}")
        return []


def get_google_trends_realtime():
    """Realtime trending topics via Google Trends' public RSS feed — no library needed."""
    try:
        resp = requests.get(
            "https://trends.google.com/trending/rss?geo=US",
            headers=UA, timeout=15,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        titles = [item.findtext("title") for item in root.findall(".//item")]
        return [t for t in titles if t][:20]
    except Exception as e:
        print(f"Google Trends (realtime) fetch failed: {e}")
        return []


def get_google_news_trending():
    """Top headlines from Google News' public RSS feed. No API key, no
    extra dependency — uses the stdlib XML parser."""
    try:
        resp = requests.get(
            "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
            headers=UA, timeout=15,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        titles = [item.findtext("title") for item in root.findall(".//item")]
        return [t for t in titles if t][:20]
    except Exception as e:
        print(f"Google News fetch failed: {e}")
        return []


def get_twitter_trends():
    """Currently-trending topics on X/Twitter via public trend mirrors.
    Tries trends24.in first, then getdaytrends.com as fallback."""
    sources = [
        ("https://trends24.in/united-states/", [
            r'class="trend-name"[^>]*>\s*<a[^>]*>([^<]+)</a>',
            r'<a[^>]+class="[^"]*trend[^"]*"[^>]*>([^<]+)</a>',
            r'<li[^>]*>\s*<a[^>]*>(#\w+)[^<]*</a>',
        ]),
        ("https://getdaytrends.com/united-states/", [
            r'<span[^>]+class="[^"]*main[^"]*"[^>]*>([^<]+)</span>',
            r'<td[^>]*>\s*<a[^>]*>([^<]{3,50})</a>\s*</td>',
        ]),
    ]
    for url, patterns in sources:
        try:
            resp = requests.get(url, headers=UA, timeout=15)
            resp.raise_for_status()
            for pattern in patterns:
                matches = re.findall(pattern, resp.text)
                if matches:
                    seen, trends = set(), []
                    for t in matches:
                        t = t.strip()
                        if t and t not in seen and 2 < len(t) < 80:
                            seen.add(t)
                            trends.append(t)
                    if len(trends) >= 3:
                        return trends[:20]
        except Exception as e:
            print(f"Twitter/X trends failed ({url}): {e}")
    print("Twitter/X trends: all sources failed or returned no matches")
    return []


def _make_reddit_client(cfg):
    if praw is None:
        print("praw not installed, skipping Reddit sources")
        return None
    r_cfg = cfg.get("reddit", {})
    try:
        return praw.Reddit(
            client_id=r_cfg.get("client_id"),
            client_secret=r_cfg.get("client_secret"),
            user_agent=r_cfg.get("user_agent", "trend-scout/1.0"),
        )
    except Exception as e:
        print(f"Reddit client setup failed: {e}")
        return None


def get_reddit_all_trending(cfg, reddit=None):
    """General, cross-topic 'what's hot on Reddit right now' (r/all), as a
    broader signal alongside the niche subreddits below."""
    reddit = reddit or _make_reddit_client(cfg)
    if reddit is None:
        return []
    posts = []
    try:
        for post in reddit.subreddit("all").hot(limit=30):
            if post.stickied:
                continue
            posts.append({
                "subreddit": str(post.subreddit),
                "title": post.title,
                "score": post.score,
                "comments": post.num_comments,
                "url": f"https://reddit.com{post.permalink}",
            })
    except Exception as e:
        print(f"Reddit r/all fetch failed: {e}")
        return []
    posts.sort(key=lambda p: p["score"] + p["comments"] * 2, reverse=True)
    return posts[:25]


def get_reddit_niche_trends(cfg, reddit=None):
    """Hot posts from a curated list of subreddits spanning many niches
    (drama, true crime, finance, tech, health, gaming, life hacks, etc.) —
    cast wide so the best idea can come from any of them."""
    reddit = reddit or _make_reddit_client(cfg)
    if reddit is None:
        return []
    posts = []
    for sub in SUBREDDITS:
        try:
            for post in reddit.subreddit(sub).hot(limit=8):
                if post.stickied:
                    continue
                posts.append({
                    "subreddit": sub,
                    "title": post.title,
                    "score": post.score,
                    "comments": post.num_comments,
                    "url": f"https://reddit.com{post.permalink}",
                })
        except Exception as e:
            print(f"Reddit fetch failed for r/{sub}: {e}")
    posts.sort(key=lambda p: p["score"] + p["comments"] * 2, reverse=True)
    return posts[:40]


# ==================== YOUTUBE + GENERAL-INTEREST SOURCES ====================

def get_youtube_trending(cfg):
    """Top trending YouTube videos via the official Data API v3.
    Reads the key from cfg['Youtube_API']['api_key'] in upload.config.json."""
    api_key = cfg.get("Youtube_API", {}).get("api_key", "")
    if not api_key:
        print("YouTube API key not set in config, skipping.")
        return []
    try:
        resp = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "snippet",
                "chart": "mostPopular",
                "regionCode": "US",
                "maxResults": 20,
                "key": api_key,
            },
            headers=UA,
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        return [item["snippet"]["title"] for item in items]
    except Exception as e:
        print(f"YouTube Trending fetch failed: {e}")
        return []


def get_general_viral_articles():
    """Trending headlines from broad, multi-niche sources (science, tech,
    business, weird/interesting) so the model has good raw material from
    outside psychology too — not just Reddit/Twitter/Google feeds."""
    sources = [
        "https://www.sciencedaily.com/rss/top/science.xml",
        "https://www.sciencedaily.com/rss/top/technology.xml",
        "https://medium.com/feed/tag/self-improvement",
    ]
    titles = []
    for url in sources:
        try:
            resp = requests.get(url, headers=UA, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            found = [item.findtext("title") for item in root.findall(".//item")]
            titles += [t for t in found if t][:10]
        except Exception as e:
            print(f"General-interest source fetch failed ({url}): {e}")
    return titles[:25]


# ==================== IDEA HISTORY ====================
# Each entry is {"title": "...", "topics": ["word1", "word2", ...]}.
# Topic keywords let future runs block the same SUBJECT even when
# the title wording is completely different.

HISTORY_PATH = SCRIPT_DIR / "idea_history.json"
MAX_HISTORY  = 25

# Words too generic to use as topic signals.
_STOP = {
    "the","a","an","is","are","was","were","be","been","being",
    "this","that","these","those","why","how","what","when","where",
    "who","which","you","your","i","my","we","our","it","its",
    "do","did","does","have","has","had","will","would","could",
    "should","may","might","can","not","no","nor","but","and","or",
    "so","yet","here","just","still","even","only","also","too",
    "about","after","before","behind","by","for","from","in","into",
    "of","on","out","over","than","to","up","with","without","until",
    "now","then","again","never","always","every","same","different",
    "new","old","first","last","next","most","more","less","many",
    "much","very","really","actually","already","heres","thats",
    "people","everyone","anyone","someone","nobody","everybody",
    "made","make","makes","making","keep","keeps","kept","going",
    "gone","went","come","came","know","knew","think","thought",
    "feel","felt","look","looked","looking","get","got","give",
    "gave","take","took","here","truly","deeply","finally",
    "brain","psychology","psychological","people","person","human",
}


def _extract_topic_keywords(title: str) -> list:
    """Return up to 6 meaningful subject words from a title.
    These are stored per history entry so future runs can block
    the same event/person/topic even with a completely different title."""
    words = re.findall(r"[A-Za-z][a-zA-Z']{2,}", title)
    keys  = [w for w in words if w.lower() not in _STOP]
    return keys[:6]


def _load_idea_history() -> list:
    """Return history as a list of dicts: [{title, topics}, ...].
    Silently upgrades old plain-string entries on first read."""
    if not HISTORY_PATH.exists():
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        upgraded = []
        for entry in raw:
            if isinstance(entry, str):
                upgraded.append({"title": entry,
                                  "topics": _extract_topic_keywords(entry)})
            else:
                upgraded.append(entry)
        return upgraded
    except Exception:
        return []


def _save_idea_to_history(title: str) -> None:
    """Append a new entry (title + extracted topic keywords) to history."""
    history = _load_idea_history()
    existing_titles = {e["title"] for e in history}
    if title not in existing_titles:
        history.append({"title": title, "topics": _extract_topic_keywords(title)})
    history = history[-MAX_HISTORY:]
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Warning: Could not save idea history: {e}")


# ==================== CROSS-PLATFORM DETECTION ====================

def _annotate_cross_platform(sources: dict) -> dict:
    """Build _cross_platform_hot from titles whose keywords appear in 2+
    sources, but cap at MAX_PER_TOPIC entries per dominant keyword cluster.
    This prevents a single viral story from monopolising all 15 slots and
    forcing the model to produce variations of the same idea every run."""
    MAX_PER_TOPIC = 3   # max entries sharing the same top keyword

    keyword_sources: dict = {}
    for source_name, items in sources.items():
        if source_name.startswith("_"):
            continue
        for item in items:
            text = (item if isinstance(item, str) else item.get("title", "")).lower()
            for word in re.findall(r'\b[a-z]{5,}\b', text):
                keyword_sources.setdefault(word, set()).add(source_name)

    hot_words = {w for w, srcs in keyword_sources.items() if len(srcs) >= 2}

    # Score each hot word by how many distinct sources it appears in.
    def _word_score(w):
        return len(keyword_sources.get(w, []))

    seen: set = set()
    hot_titles: list = []
    topic_quota: dict = {}   # dominant_keyword -> count already added

    for source_name, items in sources.items():
        if source_name.startswith("_"):
            continue
        for item in items:
            title = item if isinstance(item, str) else item.get("title", "")
            t_lower = title.lower()
            if not title or title in seen:
                continue
            title_hot = [w for w in re.findall(r'\b[a-z]{5,}\b', t_lower)
                         if w in hot_words]
            if not title_hot:
                continue
            # The "dominant keyword" for this title is the one in the most sources.
            dominant = max(title_hot, key=_word_score)
            if topic_quota.get(dominant, 0) >= MAX_PER_TOPIC:
                continue   # this topic cluster is full — skip
            topic_quota[dominant] = topic_quota.get(dominant, 0) + 1
            seen.add(title)
            hot_titles.append(title)

    if hot_titles:
        sources["_cross_platform_hot"] = hot_titles[:15]
    return sources


# ==================== NIM SYNTHESIS ====================

def ask_nim_for_best_idea(cfg, sources):
    """Use the NIM model to turn raw multi-source trend data into one
    concrete, ready-to-script video idea — with enforced creativity so
    each run produces a genuinely different result.

    Three mechanisms drive variety:
      1. A random creative angle is picked each run from CREATIVE_ANGLES.
      2. A random title format is picked each run from TITLE_FORMATS.
      3. The last N generated titles are injected as hard exclusions so
         the model cannot repeat or closely paraphrase past ideas.
    """
    google_cfg = cfg["google"]
    api_key    = google_cfg["api_key"]
    model = google_cfg.get("model_viral", 'gemini-3.5-flash-lite')

    # ── Randomly pick creative direction for this run ─────────────────
    angle = random.choice(CREATIVE_ANGLES)
    fmt   = random.choice(TITLE_FORMATS)

    # ── Build topic-level AND title-level exclusion blocks ───────────
    history = _load_idea_history()
    topic_block = ""
    title_block = ""
    if history:
        recent = history[-15:]
        # Aggregate unique topic keywords from recent entries.
        seen_kw: set = set()
        topic_keywords: list = []
        for entry in recent:
            for kw in entry.get("topics", []):
                if kw.lower() not in seen_kw:
                    seen_kw.add(kw.lower())
                    topic_keywords.append(kw)
        if topic_keywords:
            topic_block = (
                "\n\nBLOCKED SUBJECTS/EVENTS — these real-world topics have already been "
                "covered. Do NOT make a video about any of them, regardless of the angle, "
                "format, or wording you use:\n"
                + "  " + ", ".join(topic_keywords[:30])
            )
        title_block = (
            "\n\nBLOCKED TITLES — do NOT repeat or closely paraphrase these:\n"
            + "\n".join(f"  • {entry['title']}" for entry in recent)
        )

    system_prompt = (
        "You are the creative director for a YouTube channel that makes videos across "
        "ANY niche — psychology, true crime, tech, business/finance, relationships, "
        "life hacks, science, weird facts, drama, self-improvement — whatever is "
        "genuinely most viral right now. You are NOT restricted to psychology; do not "
        "default to a psychology framing just because it's familiar. Pick the niche "
        "based purely on which trending signal has the strongest viral potential.\n\n"

        "You will receive trend data from multiple sources plus a '_cross_platform_hot' list "
        "of titles whose keywords appear in multiple sources — start there for traction signals.\n\n"

        "Your job is NOT to pick whichever topic appears most often. That produces "
        "predictable, forgettable titles. Find the single most scroll-stopping idea across "
        "ALL topics and niches present in the data, then commit fully to whichever niche it "
        "belongs to — do not force it into a psychology angle if it doesn't fit.\n\n"

        "THIS RUN'S CREATIVE DIRECTION (you must honour both):\n"
        f"  • Angle  : {angle}\n"
        f"  • Format : {fmt}\n\n"

        "Hard rules:\n"
        "  1. Check '_cross_platform_hot' for traction signals, but do NOT default to "
        "whichever item appears most — spread across different subjects and niches.\n"
        "  2. If nothing there is strong, pick the best single signal from any source and "
        "reframe it creatively.\n"
        "  3. Titles must be specific and vivid — reject vague, generic titles.\n"
        "  4. NEVER use overused phrases like 'The Psychology Behind Why People' or "
        "'You Won't Believe What Happened Next'.\n"
        "  5. The title must create immediate curiosity, tension, or self-recognition, "
        "regardless of which niche it comes from.\n"
        f"{topic_block}"
        f"{title_block}\n\n"

        "Silently consider 2-3 candidate ideas from DIFFERENT niches, then output only "
        "the single best one overall.\n"
        "Respond with compact JSON and absolutely nothing else:\n"
        '{"title": "..."}'
    )

    user_prompt = (
        "Here is today's trend data grouped by source "
        "('_cross_platform_hot' highlights multi-platform topics):\n\n"
        f"{json.dumps(sources, ensure_ascii=False, indent=2)}\n\n"
        "Generate the single most creative, scroll-stopping YouTube video title — "
        "from whichever niche has the strongest viral idea, not necessarily psychology."
    )

    resp = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            "temperature": 0.92,   # ← was 0.7; higher = more creative variation per run
            "max_tokens": 2048,
        },
        timeout=60,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[DEBUG] raw response was: {repr(raw)}")
        raise
    return {"title": parsed["title"]}


# ==================== FORMAT-PRESERVING FILE WRITE ====================

def _find_balanced_object_span(raw, open_brace_idx):
    """Given the index of a '{' in raw, return the index just after its
    matching '}', respecting JSON string literals so braces inside string
    values don't throw off the count."""
    depth, i, in_string, escape = 0, open_brace_idx, False, False
    while i < len(raw):
        ch = raw[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    raise ValueError("Unbalanced braces while scanning for end of object")


def _format_video_idea_block(idea):
    """Render the video_idea object indented as a top-level key (2 spaces),
    with its fields indented one level deeper (4 spaces) — matching the
    general style of the rest of the file."""
    body = json.dumps(idea, indent=2, ensure_ascii=False)
    indented = "\n".join(("  " + line if line else line) for line in body.split("\n"))
    return f'  "video_idea": {indented.lstrip()}'


def upsert_video_idea(path, idea):
    """Insert (or, on later runs, replace) the top-level 'video_idea' key by
    editing the raw file TEXT directly — never json.load + json.dump the
    whole file. This guarantees every other key, blank line, and hand-
    aligned space elsewhere in the file stays exactly as it was."""
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    cfg = json.loads(raw)  # validate + check whether video_idea already exists
    block = _format_video_idea_block(idea)

    key_pos = raw.find('"video_idea"')
    if "video_idea" in cfg and key_pos != -1:
        brace_pos = raw.index("{", key_pos)
        end_pos = _find_balanced_object_span(raw, brace_pos)
        line_start = raw.rfind("\n", 0, key_pos) + 1
        raw = raw[:line_start] + block + raw[end_pos:]
    else:
        trimmed = raw.rstrip()
        if not trimmed.endswith("}"):
            raise ValueError("Config file doesn't end with '}' — refusing to touch it")
        body = trimmed[:-1].rstrip()
        raw = body + ",\n\n" + block + "\n}\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(raw)


# ==================== MAIN ====================

def main():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.loads(f.read())

    reddit = _make_reddit_client(cfg)

    print("Fetching Google Trends (daily)...")
    g_daily = get_google_trends_daily()

    print("Fetching Google Trends (realtime)...")
    g_realtime = get_google_trends_realtime()

    print("Fetching Google News headlines...")
    g_news = get_google_news_trending()

    print("Fetching X/Twitter trends...")
    tw = get_twitter_trends()

    print("Fetching Reddit r/all (general)...")
    r_all = get_reddit_all_trending(cfg, reddit)

    print("Fetching Reddit niche subreddits...")
    r_niche = get_reddit_niche_trends(cfg, reddit)

    print("Fetching YouTube trending videos...")
    yt = get_youtube_trending(cfg)

    print("Fetching general-interest viral articles...")
    general_articles = get_general_viral_articles()

    sources = {
        "google_trends_daily": g_daily,
        "google_trends_realtime": g_realtime,
        "google_news_headlines": g_news,
        "twitter_trends": tw,
        "reddit_all_trending": r_all,
        "reddit_niche_posts": r_niche,
        "youtube_trending": yt,
        "general_viral_articles": general_articles,
    }

    # Annotate which topics are hot across multiple sources — gives the
    # model a clear signal instead of making it infer from repetition.
    sources = _annotate_cross_platform(sources)

    hit_sources = [name for name, data in sources.items() if data and not name.startswith("_")]
    if not hit_sources:
        print("No trend data collected from any source — aborting without touching the config.")
        sys.exit(1)
    cross = sources.get("_cross_platform_hot", [])
    print(f"Collected data from: {', '.join(hit_sources)}")
    print(f"Cross-platform hot topics found: {len(cross)}")

    print("Asking NIM model to pick the best idea...")
    idea = ask_nim_for_best_idea(cfg, sources)

    # Persist title so future runs know to avoid it.
    _save_idea_to_history(idea["title"])

    upsert_video_idea(CONFIG_PATH, idea)

    print("\nDone. video_idea written:")
    print(json.dumps(idea, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()