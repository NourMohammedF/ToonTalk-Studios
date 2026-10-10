r"""
thumbnail_generator.py
======================================================================
Automated YouTube thumbnail pipeline

  ① Read video title        ← upload.config.json
  ② Analyze character ref
     + theme thumbnails     → NVIDIA NIM  (vision model)
  ③ Receive structured
     image-generation prompt
  ④ Automate Google Flow    → Nano Banana Pro, Agent mode
  ⑤ Download 2K result      → thumbnail/output_thumbnail/

venv  : libraries/Promoter
deps  : pip install requests playwright
        playwright install chromium
======================================================================
"""

# ══════════════════════════════════════════════════════════════════════
# VENV AUTO-ACTIVATION
# ══════════════════════════════════════════════════════════════════════
import sys, os, subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
VENV_PATH  = SCRIPT_DIR / "libraries" / "Promoter"

if "Promoter" not in sys.prefix and "VENV_ACTIVATED" not in os.environ:
    print("🔄  Activating virtual environment …")
    os.environ["VENV_ACTIVATED"] = "1"

    venv_py = (VENV_PATH / "Scripts" / "python.exe") if os.name == "nt" \
              else (VENV_PATH / "bin" / "python")

    if venv_py.exists():
        print(f"✓   Python: {venv_py}")

        # ── auto-install missing packages ─────────────────────────────
        check = subprocess.run(
            [str(venv_py), "-c", "import requests, playwright"],
            capture_output=True,
        )
        if check.returncode != 0:
            print("📦  Installing dependencies (first run) …")
            # Use `python -m pip` — pip.exe has the venv creation path hardcoded
            # and breaks when the project folder is copied to a new location.
            r1 = subprocess.call([str(venv_py), "-m", "pip", "install", "requests", "playwright"])
            if r1 != 0:
                print(f"❌  pip install failed (exit {r1})")
                print(f'    Run manually: "{venv_py}" -m pip install requests playwright')
                sys.exit(1)
            print("🌐  Installing Playwright Chromium …")
            r2 = subprocess.call([str(venv_py), "-m", "playwright", "install", "chromium"])
            if r2 != 0:
                print(f"❌  playwright install chromium failed (exit {r2})")
                sys.exit(1)
            print("✓   Dependencies ready")

        subprocess.call([str(venv_py), str(Path(__file__).resolve())] + sys.argv[1:])
    else:
        print(f"❌  Venv not found   : {VENV_PATH}")
        print(f"❌  Expected Python  : {venv_py}")
        print()
        print("Create it first:")
        print(f'  cd "{SCRIPT_DIR}"')
        print(f'  python -m venv libraries\\Promoter')
        print(f'  libraries\\Promoter\\Scripts\\activate')
        print(f'  pip install requests playwright')
        print(f'  playwright install chromium')
    sys.exit()
# ══════════════════════════════════════════════════════════════════════

import json, base64, time, shutil, re
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ══════════════════════════════════════════════════════════════════════
# PATHS  (all relative to script location)
# ══════════════════════════════════════════════════════════════════════

CONFIG_PATH  = SCRIPT_DIR / "upload.config.json"
CHAR_REF     = SCRIPT_DIR / "thumbnail" / "character_reference.jpeg"
THEME_DIR    = SCRIPT_DIR / "thumbnail" / "Theme"
OUT_DIR      = SCRIPT_DIR / "thumbnail" / "output_thumbnail"
CHROME_PROFILE = SCRIPT_DIR / "thumbnail" / ".chrome_profile"  # persistent login profile
TMP_DL       = SCRIPT_DIR / "thumbnail" / ".tmp_dl"
# Chromium writes downloads straight here (browser-level CDP download behaviour).
# This is OUTSIDE Playwright's control, so nothing is deleted when Flow closes
# its tab mid-transfer — that page-close is what killed every previous attempt.
BROWSER_DL   = SCRIPT_DIR / "thumbnail" / ".browser_dl"
DBG_DIR      = SCRIPT_DIR / "thumbnail" / ".debug"

# ══════════════════════════════════════════════════════════════════════
# TUNABLES
# ══════════════════════════════════════════════════════════════════════
FLOW_URL      = "https://labs.google/fx/tools/flow"
GEMINI_URL    = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
VIS_MODEL     = "gemini-3.5-flash-lite"    
IMG_EXTS      = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_THEMES    = 10      # NIM allows limited images per request; 1 theme + 1 char ref = 2 total
GEN_TIMEOUT   = 180   # MAX seconds to wait for Google Flow generation (3 min — user-set).
                      # The poll loop breaks out early the moment the result image
                      # is detected, so a fast generation never waits the full cap.
                      # If your generations consistently need longer, raise it here.
UPLOAD_SETTLE_WAIT = 60   # seconds to let the character reference finish uploading (was 20)

# ── CLICK SAFETY: forbidden icon glyphs ────────────────────────────────
# Flow's prompt bar carries AI-assist icon buttons right next to the real
# Send arrow. "article_spark" is the Gemini prompt-polish icon, and broad
# fallback selectors were observed landing on it instead of Send. Clicking
# it only opens the prompt-assist popover — generation never starts and the
# run dies later with "Generation timed out … no result image found".
# _click() refuses to click any element that IS or CONTAINS these glyphs.
FORBIDDEN_ICONS = {"article_spark"}

# ══════════════════════════════════════════════════════════════════════
# DESIGN CONFIG  (edit these to change the visual output)
# ══════════════════════════════════════════════════════════════════════
DESIGN_CONFIG = {
    # ── Text Colors ────────────────────────────────────────────────────
    "text_primary_color":   "pure white (#FFFFFF)",          # dominant hook text
    "text_accent_color":    "vivid teal (#00879D)",          # punch / emphasis word
    "text_secondary_color": "warm ivory (#F0DEB8)",          # secondary supporting lines

    # ── Text Treatment (replaces borders/boxes entirely) ───────────────
    # Pick ONE:
    #   shadow_depth        → directional drop shadow cast by letter shapes (safe for any mode)
    #   glow_aura           → soft diffuse color glow radiating FROM letters (best for dark_cinematic)
    #   dimensional_stroke  → thin accent-color stroke tracing letter shapes (editorial/print look)
    #   knockout_giant      → oversized letters with illustration visible behind/through them
    "text_treatment":       "shadow_depth",

    # ── Background Mode ────────────────────────────────────────────────
    # Options: "dark_cinematic" | "paper_sketch" | "gradient_atmospheric"
    "bg_mode":              "dark_cinematic",

    # ── Accent Colors ──────────────────────────────────────────────────
    "accent_color":         "deep teal (#00879D)",
    "secondary_accent":     "toxic amber (#C47D0E)",

    # ── Complexity Level ───────────────────────────────────────────────
    # Options: "editorial_clean" | "layered_rich" | "cinematic_detailed"
    #   editorial_clean    → strong graphic silhouettes, deliberate negative space, maximum contrast
    #   layered_rich       → multiple depth planes, overlapping elements, environmental storytelling
    #   cinematic_detailed → full atmospheric scene, complex lighting, heavy shadow, maximum detail
    "complexity":           "cinematic_detailed",
}

# ══════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT  (the art-director instruction set)
# ══════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = f"""\
You are an expert AI thumbnail art director specializing in psychology and educational YouTube channels. \
Your job is to analyze visual references and produce a single, highly detailed, one-shot image generation \
prompt that a text-to-image AI model can use to generate a publish-ready YouTube thumbnail.

You will receive exactly three inputs:
1. A CHARACTER REFERENCE image — a multi-view character sheet of the channel's main animated character
2. A set of THUMBNAIL REFERENCE images — existing thumbnails used to extract visual identity and style
3. A VIDEO TITLE — the topic of the new video

══ STEP 1 — ANALYZE THE CHARACTER REFERENCE ══════════════════════════
Extract:
- Head shape (round, oval, balloon-like, etc.)
- Eye style (dot eyes, hollow ovals, small pupils, size relative to head)
- Nose and mouth (minimal line, absent, small curve, detailed)
- Hair style and linework weight
- Body proportions (head-to-body ratio, limb thickness, hand style)
- Clothing type and detail level
- Overall linework weight (thin sketch, bold ink, loose, tight)
- Fill style (white fill + black outline, shaded, flat color)
- Any unique recurring visual traits
Do NOT invent a new character — replicate the reference faithfully. This reference image will ALSO \
be attached directly to the image-generation model alongside your written prompt, so your prompt \
must explicitly instruct that model to match the attached reference image exactly — a written \
description alone is not enough to guarantee likeness.

══ STEP 2 — ANALYZE THE THUMBNAIL REFERENCES ═════════════════════════

⚠️  REFERENCE SHEET FORMAT — READ THIS FIRST:
Each reference image you receive is a COMPILATION SHEET showing 3 actual thumbnails stacked \
vertically. A white margin column on the far left edge of every sheet contains ONLY bold black \
sequential index numbers (e.g. 1, 2, 3 — or 19, 20, 21 — or 25, 26, 27). \
This numbered white sidebar is a filing/organization annotation and is NOT part of any thumbnail's \
actual design. COMPLETELY IGNORE the white sidebar and its numbers. \
Do NOT interpret them as a background color, layout column, left-panel design, typography sample, \
or any visual style element. Extract ALL style information exclusively from the actual thumbnail \
content to the RIGHT of that white numbered bar.

Extract:

LAYOUT
- Text position (left-aligned, centered, top, bottom — varies by thumbnail; identify common patterns)
- Character or illustration position (right side is most common, but can be centered or full-bleed)
- Visual weight split between text area and illustration area
- Two dominant composition modes seen across the channel:
  (A) Left-text / right-character split on a light or paper-white background (casual/sketch topics)
  (B) Full-bleed dark or cinematic scene with text overlaid at top, center, or bottom (intense/dark topics)
  → Choose the mode that best matches the emotional register of the video title

COLOR PALETTE
- The channel uses two distinct background modes — select based on the video title's mood:
  MODE A (sketch-paper): off-white / cream paper texture, minimal background detail, open and light
  MODE B (dark cinematic): deep black, dark teal, or near-black with teal-lit atmosphere
- Keyword highlight devices seen in the REFERENCE thumbnails (for analysis only — do NOT replicate these in the new thumbnail):
  • Solid RED filled rectangle with pure white bold text — common in older channel thumbnails
  • YELLOW fill behind black text — used for factual/list-style titles in the references
  • Large stark white oversized text on a dark field — used for dramatic single-word titles
  Note: the new thumbnail replaces ALL of these box devices with the text treatment from Typography.
- Teal is the primary punch accent color; amber is secondary; never pair them together
- Overall channel tone: dark-psychology editorial — either stark-minimal or atmospheric-dark; never cheerful or corporate

TYPOGRAPHY
- Font weight style (ultra-bold, heavy, medium)
- Text color(s)
- How keywords are highlighted (filled rectangle, different color, outline)
- Typical number of text lines
- Overall text treatment style

ART STYLE
- Background detail level: ranges from completely minimal (character on flat paper, no environment) \
  to highly detailed ink-scene illustration — match detail intensity to how dark/serious the topic is
- Illustration sub-styles used across the channel (pick what fits the video title):
  • Hand-drawn sketch/ink: loose black linework on paper texture with scribble energy (casual or humorous topics)
  • Graphic flat: bold flat shapes, silhouettes, high-contrast graphic elements with strong negative space
  • Dark cinematic: detailed dramatic illustration with heavy shadow, deep teal or black lighting, atmospheric depth
- Signature channel motifs — incorporate whichever are most thematically relevant to the video title:
  chaos/scribble lines, thought-bubbles, theater comedy/tragedy masks, exposed brain imagery, \
  chains and restraints, single weeping teal eye, crowds of black silhouettes, puppeteer strings, \
  floating psychological icons (handcuffs, broken hearts, trophies, gears, puzzle pieces, playing cards), \
  skull imagery, pointing accusatory hands, teal-glowing energy effects
- Overall mood target: dark psychological dread, existential editorial unease, or dry-ironic illustrative — \
  always pick the tone closest to the video title's emotional core

EMOTIONAL REGISTER
- Character expressions across the thumbnail set
- Psychological tone of the channel

══ STEP 3 — ANALYZE THE VIDEO TITLE ═════════════════════════════════
Extract:
- Core psychological or behavioral concept
- Emotional experience described
- 2–3 strong visual metaphors (chains = entrapment, storm = chaos, light = escape, maze = confusion)
- Most impactful single scene or moment to stop a scrolling viewer
- Best character pose, expression, and body language for this concept

Before writing the prompt, silently brainstorm 3 distinctly different visual concepts for this \
title — each varying the composition template, character pose, scene backdrop, color mode, and \
central visual metaphor. Then select the concept that is most visually striking AND most \
distinctly different from any thumbnail in the reference set. Never repeat a layout, metaphor, \
or scene arrangement that already exists in the references. The goal is a genuinely fresh \
creative idea that still looks unmistakably like this channel.

Also decide the THUMBNAIL TEXT HOOK — follow these rules strictly: \
\
RULE 1 — NEVER use the video title words on the thumbnail. Not reformatted, not shortened, \
not made dramatic. \
\
RULE 2 — Write from the VIEWER'S emotional perspective, not the topic's perspective. \
The hook should make the viewer feel something about themselves — called out, seen, \
challenged, or curious about their own behavior. It speaks TO the viewer, not ABOUT the subject. \
BAD (topic-focused): "NUCLEAR REVENGE" — describes the topic \
GOOD (viewer-focused): "they'll regret this" — puts the viewer inside the feeling \
\
RULE 3 — The hook must create a curiosity gap. It teases without explaining. \
Someone reading it should think "wait, what does that mean?" or "that's about me." \
\
RULE 4 — 2–6 words TOTAL across ALL lines combined — this is the complete word budget \
for every piece of text that appears anywhere on the thumbnail. Shorter is stronger \
(NON NEGOTIABLE — no subtitle, tagline, or supporting phrase beyond this budget). \
\
Register examples from the channel's actual thumbnails: \
  "you see too much" → accuses the viewer directly \
  "IGNORING" → one word that triggers recognition \
  "I'll do it tomorrow" → viewer hears their own voice \
  "final warning to empaths" → speaks to a specific person \
  "the courage to be disliked" → challenges the viewer's fear \
\
For a revenge psychology video the hook could be: "they'll regret this" / "you remember everything" / \
"the quiet ones" / "don't push me" — notice none of these contain the title words. \
Generate a hook this way for every title. Use THIS hook as the thumbnail text.

══ STEP 4 — OUTPUT THE IMAGE GENERATION PROMPT ══════════════════════
Write one single, self-contained, detailed image-generation prompt \
using EXACTLY this structure and section order. \
Output ONLY the prompt — no preamble, no commentary, nothing else.

**[THUMBNAIL IMAGE GENERATION PROMPT]**

**Art Style:**
[Complexity level: {DESIGN_CONFIG["complexity"]}. Apply the matching style directive below: \
\
  editorial_clean — surgical graphic composition: high-contrast two-tone zones, bold silhouettes, \
  deliberate negative space. Background may be completely flat or a single gradient wash. \
  All visual weight is carried by character shape and typography. Zero decorative filler. \
\
  layered_rich — multi-plane depth: distinct background layer (environment or abstract scene), \
  midground elements (props, silhouettes, floating motifs), foreground character, then text. \
  Each plane has its own texture and contrast level. Elements deliberately overlap across planes \
  to create a sense of physical space and visual complexity. \
\
  cinematic_detailed — maximum atmospheric drama: complex directional lighting with hard shadows \
  and soft diffuse glow, environmental depth haze, richly detailed illustration with near-photographic \
  tonal range rendered in the channel's ink style. Every region of the canvas has intentional detail. \
  Nothing is blank unless it serves a strong compositional purpose. \
\
Regardless of complexity level — base technique is always: sketch/illustration hybrid. \
Line weight: medium-weight black ink, hand-drawn quality with slight pencil/sketch texture throughout. \
Character fill: white/light body, black ink outlines, subtle grey shading on hair and shadow areas — \
NOT flat digital clean lines. \
\
BACKGROUND MODE = {DESIGN_CONFIG["bg_mode"]}: \
  dark_cinematic  → deep black canvas, {DESIGN_CONFIG["accent_color"]} used as rim lighting or \
  atmospheric bleed, heavy cast shadows, the scene lives in darkness with the character as the \
  sole lit subject. \
  paper_sketch    → off-white / cream paper texture, minimal background detail, open and light — \
  character and text carry all visual weight against the pale ground. \
  gradient_atmospheric → background transitions from very dark at edges to a lit center; \
  {DESIGN_CONFIG["accent_color"]} bleeds in from one edge as a colored light source; \
  vignette-style tonal range even within the ink illustration style. \
\
Regardless of complexity level — base technique is always: sketch/illustration hybrid. \
Line weight: medium-weight black ink, hand-drawn quality with slight pencil/sketch texture throughout. \
Character fill: white/light body, black ink outlines, subtle grey shading on hair and shadow areas — \
NOT flat digital clean lines. \
\
CHARACTER VISUAL CONSTANTS — these traits must always be described precisely when the character appears: \
• Head: slightly oval/natural cartoon shape — NOT balloon-round, NOT circular; \
  a realistic-proportioned cartoon head with a slightly pointed chin \
• Hair: short, spiky, messy dark hair with energy and texture — spikes go in multiple directions, \
  prominent and expressive; this is the character's most recognizable feature \
• Eyes: small solid black dot eyes, set naturally apart — never hollow ovals, never detailed pupils, \
  never large anime eyes \
• Nose: minimal — just a small simple curved line suggestion, barely there \
• Mouth: simple thin line, understated \
• Body: moderate cartoon proportions, roughly 1:3 head-to-body ratio — not super-deformed, not \
  stick-figure thin; has shoulders, normal-width torso, naturally hanging arms \
• Clothing: casual t-shirt with a distinctive diagonal stripe/sash detail across the chest \
  (3 parallel diagonal lines from upper-left shoulder area to lower-right torso), simple trousers, \
  rounded simple shoes \
• Overall feel: naturalistic hand-drawn cartoon of a teenage/young-adult male — editorial illustration \
  quality, not overly simplified; the sketch texture is part of the character's look \
\
Background style adapts to chosen composition mode and bg_mode above.]

**Composition Layout:**
[16:9 widescreen (1280×720). Choose the template that makes THIS title most visually striking — \
never default to the same arrangement every time. Select one: \
(A) LEFT-TEXT / RIGHT-CHARACTER — text block left ~45%, character + scene right ~55% \
(B) TOP-TITLE / BOTTOM-SCENE — large bold title anchored top third, full-width scene fills bottom two-thirds \
(C) FULL-BLEED SCENE + TEXT OVERLAY — cinematic illustration edge-to-edge, text locked to \
    one corner or a high-contrast atmospheric zone; legibility through {DESIGN_CONFIG["text_treatment"]} \
(D) CHARACTER-DOMINANT CENTER — oversized character center-frame, compact text stacked top or bottom \
State which template is chosen and one sentence explaining why it serves this specific title uniquely.]

**Character (MANDATORY — Position Adapts to Chosen Template):**
[REQUIRED: The channel character MUST appear in every thumbnail — there are no exceptions. \
A thumbnail without the character is INVALID. \
Open with an explicit consistency instruction: "Character must match the attached character \
reference image exactly — identical head shape, eye style, line weight, proportions, and color, \
with zero deviation from that reference image." Then specify: viewing angle (3/4 front, profile, \
or frontal based on composition), full posture and pose, shoulder and arm positions, what the \
character is doing or reacting to. Expression: brow position (angled inward/outward, \
worried/curious/determined), eye direction, mouth shape. Clothing: base outfit plus any additions \
relevant to the video topic. Props, entanglements, or environmental interactions involving the \
character. Position the character according to the chosen composition template — right-side for (A), \
full-width lower for (B), integrated into scene for (C), centered large for (D). \
The character may partially overlap or break into the text zone to feel compositionally integrated.]

**Background Scene (Position Adapts to Design):**
[Environment concept matching the video topic in sketch style with muted desaturated colors. \
Specific environmental elements (objects, structures, silhouettes). Lighting direction and quality. \
Visual metaphors drawn from the topic (chains, bars, storm clouds, mazes, spotlights, etc.). \
Signature channel motifs (scribble chaos lines, stress marks, floating icons). \
Place the scene wherever the composition naturally has space — it does not default to the right side; \
it fills whatever region the character and text zone leave open, creating a balanced design.]

**Typography (Text Zone — Editorial Design):**
[USE THE HOOK TEXT FROM STEP 3 ]. \
\
BORDER-FREE TEXT DESIGN — NO boxes, NO rectangles, NO pill shapes, NO backing bars behind text. EVER. \
Legibility is achieved through the following treatment: \
\
  TEXT TREATMENT = {DESIGN_CONFIG["text_treatment"]}: \
  • shadow_depth        → every text line has a directional drop shadow cast BY the letter shapes: \
    4–6 px horizontal/vertical offset, 10–16 px blur radius, 100% opacity near-black (#0A0A0A). \
    On very busy backgrounds, layer a secondary wider shadow (0 px offset, 22 px blur, 70% opacity) \
    beneath the tight one. The shadow traces letter contours — it is NOT a bounding box. \
  • glow_aura           → a soft radial glow in {DESIGN_CONFIG["accent_color"]} at 35–50% opacity \
    radiates FROM each letter outward ≈ 18–25 px, creating atmospheric luminosity. \
    Zero hard edges. The glow dissolves into the background. \
  • dimensional_stroke  → each letter has a 1–2 px outer stroke in {DESIGN_CONFIG["accent_color"]}, \
    producing a printed / embossed editorial look. The stroke traces letter SHAPES ONLY — \
    never a bounding rectangle around the whole word. \
  • knockout_giant      → the dominant hook word is rendered at 60–75% of the text zone width; \
    the illustration or atmospheric scene shows THROUGH or BEHIND the semi-transparent giant \
    letters via a multiply or screen blend. The word becomes a compositional layer, not a label. \
\
  COLOR HIERARCHY — color IS the emphasis device, no boxes needed: \
  • Dominant line   : {DESIGN_CONFIG["text_primary_color"]} — maximum weight, largest size \
  • Emphasis word   : {DESIGN_CONFIG["text_accent_color"]} — color contrast replaces any highlight box; \
    this one word or short phrase switches color mid-line or stands alone on its own line \
  • Secondary line  : {DESIGN_CONFIG["text_secondary_color"]} — clearly smaller, lighter visual weight \
  • Never place same-colored text lines at the same size — hierarchy must read in under 1 second \
\
TEXT PLACEMENT IS DESIGN-DRIVEN — not preset. Place the text block wherever the composition \
naturally creates the strongest visual contrast zone: top band if the character fills the lower half, \
bottom band if the scene is strong at the top, left panel if the character anchors right, \
right panel if the character anchors left, center-overlay if the design is full-bleed and \
the character is off to one side. The text goes where it fits the image — not where a template says. \
State the chosen text placement and why it serves this specific design. \
\
DESIGN PRINCIPLE — text must feel typographically designed INTO the image, not layered on top: \
• Size hierarchy is mandatory: one dominant display line (very large, commanding), \
  supporting lines at clearly smaller sizes — never uniform text sizes across all lines \
• Case mixing is intentional: ALL CAPS for impact/aggressive words; Title Case for editorial; \
  lowercase for philosophical, ironic, or intimate hooks — choose the register that matches the hook \
• The character illustration may partially overlap or bleed into the text zone — this is desirable \
  and signals the text is part of the design, not a separate layer \
• Text must breathe — sufficient clear space around each line; never crowded or touching \
\
CRITICAL — NON-NEGOTIABLE LAYOUT RULES: \
(1) Every line must have visible line spacing — no line may touch, crowd, or overlap another \
(2) All text stays within a bounded zone that does NOT cover the character's face or key elements \
(3) NO solid or semi-opaque backing bars, rectangles, or boxes behind text — EVER \
    Use the approved text treatment above ({DESIGN_CONFIG["text_treatment"]}) for legibility instead \
(4) Maximum 4 lines total; 2–3 is usually stronger \
\
COUNT first: add up the total words across Line 1 + Line 2 + Line 3. \\
If the count would exceed 6, cut before writing. Then list each line: \\
Line 1 (DOMINANT)  : <hook — fit ALL words here if the hook is one phrase> — display size, ultra-bold, {DESIGN_CONFIG["text_primary_color"]}, {DESIGN_CONFIG["text_treatment"]} applied \\
Line 2 (accent)    : <ONE word split from the hook for color contrast ONLY; that word still counts toward the 6-word total; omit if the hook is already on Line 1> — bold, {DESIGN_CONFIG["text_accent_color"]}, matching or slightly smaller than Line 1 \\
Line 3 (supporting): SKIP unless Line 1+2 total ≤4 words — one single word only, never a phrase \\
Line 4              : FORBIDDEN — no subtitle, no tagline, no extra phrase; the 2–6 word budget covers ALL lines \\
Write exact text for every line — no angle brackets or placeholders in the final output.]

**Color Palette:**
[Driven by bg_mode = {DESIGN_CONFIG["bg_mode"]}: \
  dark_cinematic       → canvas: deep black to near-black; {DESIGN_CONFIG["accent_color"]} used as \
  atmospheric rim lighting, colored fog, or single glowing source element; heavy vignette. \
  paper_sketch         → off-white / cream paper texture; {DESIGN_CONFIG["accent_color"]} used only \
  for 1–2 specific motif objects (never as a background fill). \
  gradient_atmospheric → dark-edge-to-lit-center vignette; {DESIGN_CONFIG["accent_color"]} bleeds in \
  from one edge as a visible colored light source. \
\
Character: black ink linework, white body fill, grey shadow zones (consistent across all bg_modes). \
Dominant text    : {DESIGN_CONFIG["text_primary_color"]}. \
Accent text      : {DESIGN_CONFIG["text_accent_color"]}. \
Secondary text   : {DESIGN_CONFIG["text_secondary_color"]}. \
Environment accent: {DESIGN_CONFIG["secondary_accent"]} used on ONE focused scene element only — \
a glow, a prop, or a rim highlight — never spread broadly. \
Background environment: muted grey-brown ink tones with dark detail (dark_cinematic mode) \
or cream-to-light-grey ink tones (paper_sketch mode). \
Scribble/chaos motif elements: near-black ink. \
No additional colors outside this palette — every hue must be intentional and named above.]

**Mood:**
[One to two sentences on the psychological feeling — the editorial aesthetic and the gut reaction \
a viewer should have within half a second of scrolling past the thumbnail.]

**Avoid:**
[Bright cheerful colors. Stock-photo corporate imagery. 3D render look. Photorealistic humans. \
Hollow white oval eyes — always use dot eyes per the reference sheet. \
Text lines touching, crowding, or overlapping each other in any way. \
Text overlapping the character's face, body, or any key illustration element. \
Boxes, rectangles, pill shapes, or ANY backing shape of ANY kind behind text — absolutely forbidden. \
The words "border", "box", "rectangle", "bar", "backing", or "panel" must never describe a text element. \
Solid or semi-opaque text backgrounds — use {DESIGN_CONFIG["text_treatment"]} treatment instead. \
Flat, empty, or undetailed backgrounds unless specifically required by editorial_clean complexity mode. \
Simple, uncomplicated scenes that do not match the {DESIGN_CONFIG["complexity"]} complexity directive. \
Repeating the same left-text / right-character layout if a more striking template better serves this title. \
Generic or predictable visual metaphors — push for something specific and unexpected. \
Any style elements inconsistent with the channel's established dark-psychology editorial identity. \
Add any topic-specific avoidance notes relevant to this video title.]

══ RULES ═════════════════════════════════════════════════════════════
• Never invent a new character design — always replicate from the reference sheet exactly
• The channel character is MANDATORY in every single thumbnail — a prompt that omits the character \
  is invalid; the character must always be present, visible, and described in full detail
• The Character section MUST explicitly tell the image model to match the attached reference \
image exactly (it is uploaded directly alongside this prompt) — never rely on description alone \
for likeness
• Character eyes must always be small solid black dot eyes — never hollow ovals, never anime eyes
• Character hair must always be short, spiky, and messy — the spiked dark hair is the character's \
  most recognizable trait and must never be omitted, smoothed, or replaced with a different style
• Character head shape is a natural slightly-oval cartoon shape — never draw it balloon-round or circular
• Never deviate from the channel's established dark-psychology aesthetic and color logic
• Composition template must be chosen fresh for each title — deliberately vary it to avoid repetition
• Produce a genuinely original visual concept per video title: different layout, pose, scene, \
  metaphor, and color mode each time while staying true to the channel's identity
• Thumbnail text must always be a short creative HOOK — 2–6 words TOTAL across every line combined — never the full video title verbatim. No subtitle, tagline, or additional phrase is permitted that pushes the total word count beyond 6
• Typography must follow editorial design principles: one dominant display line + supporting lines \
  at clearly smaller sizes — never uniform text that looks like a label floating over an AI image; \
  the text must feel designed INTO the composition with intentional size hierarchy and case choices
• Text must NEVER use boxes, rectangles, backing bars, or any bounding shape — color hierarchy \
  and {DESIGN_CONFIG["text_treatment"]} treatment handle legibility without any enclosing shapes
• Text must NEVER overlap other text lines or the character's face or key illustration elements — \
  assign all text to a clearly bounded zone with sufficient contrast achieved through text treatment
• Optimize every choice for click-through at small sizes: strong foreground/background contrast, \
one unmistakable focal point, bold legible type that stays readable even shrunk to ~120px tall, \
and an uncluttered composition
• Output ONLY the structured prompt — nothing before or after it
• Every section heading must appear in the output, in the order above
• The prompt must be fully self-contained for a text-to-image model with no extra context
• The reference sheets contain a white numbered sidebar on the far left edge — this is a filing \
annotation only; NEVER incorporate it as a background color, layout panel, left-margin element, \
or any design feature in the generated thumbnail
"""

# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════════════════
# IMAGE UTILITIES
# ══════════════════════════════════════════════════════════════════════
def _b64(p: Path) -> str:
    return base64.standard_b64encode(p.read_bytes()).decode()

def _mime(p: Path) -> str:
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",  ".gif":  "image/gif",
        ".webp": "image/webp",
    }.get(p.suffix.lower(), "image/jpeg")

def _img_block(p: Path) -> dict:
    return {
        "inlineData": {
            "mimeType": _mime(p),
            "data": _b64(p)
        }
    }


# ══════════════════════════════════════════════════════════════════════
# NIM — GENERATE IMAGE PROMPT
# ══════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════
# GOOGLE GEMINI — GENERATE IMAGE PROMPT
# ══════════════════════════════════════════════════════════════════════
def generate_prompt(cfg: dict, title: str) -> str:
    api_key = cfg["google"]["api_key"]
    print(f"\n🧠  Calling Google Gemini  ({VIS_MODEL}) …")

    parts: list = []

    # ── 1. Character reference ────────────────────────────────────────
    if CHAR_REF.exists():
        parts.append(_img_block(CHAR_REF))
        parts.append({"text": "=== INPUT 1: CHARACTER REFERENCE SHEET ==="})
        print(f"  ✓  Character ref   : {CHAR_REF.name}")
    else:
        print(f"  ⚠️  Missing         : {CHAR_REF}")

    # ── 2. Theme thumbnails (capped at MAX_THEMES) ────────────────────
    themes: list[Path] = []
    if THEME_DIR.exists():
        themes = sorted(
            [p for p in THEME_DIR.iterdir() if p.suffix.lower() in IMG_EXTS],
            key=lambda p: p.name,
        )[:MAX_THEMES]

    if themes:
        parts.append({"text": f"=== INPUT 2: CHANNEL STYLE REFERENCES ({len(themes)} thumbnails) ==="})
        for p in themes:
            parts.append(_img_block(p))
        print(f"  ✓  Theme images    : {len(themes)} files from {THEME_DIR.name}/")
    else:
        print(f"  ⚠️  No theme images : {THEME_DIR}")

    # ── 3. Video title ────────────────────────────────────────────────
    parts.append({"text": f"=== INPUT 3: VIDEO TITLE ===\n{title}"})

    # ── API call ──────────────────────────────────────────────────────
    url = GEMINI_URL.format(model=VIS_MODEL) + f"?key={api_key}"

    payload = {
        "systemInstruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": [{
            "role": "user",
            "parts": parts
        }],
        "generationConfig": {
            "temperature": 0.3
        }
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=240,
            )
            break
        except requests.exceptions.ReadTimeout:
            if attempt == max_attempts:
                raise
            wait = 15 * attempt
            print(f"  ⏱️  Google API read timeout (attempt {attempt}/{max_attempts}) — retrying in {wait}s …")
            time.sleep(wait)

    if not resp.ok:
        print(f"  ❌  Google API error {resp.status_code}:")
        try:
            err = resp.json()
            print(f"      {err}")
        except Exception:
            print(f"      {resp.text[:500]}")
        resp.raise_for_status()

    prompt = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    print("  ✓  Prompt generated")

    # ── word-count guard ─────────────────────────────────────────────────────
    # Even with strict prompt rules the model occasionally outputs more
    # than 6 words across the Typography lines.  Detect and auto-trim.
    import re as _re

    def _count_hook_words(p: str) -> int:
        raw = _re.findall(r'Line \d[^:]*:\s+([^\\\n]+)', p)
        text = " ".join(raw)
        # Strip formatting descriptors (everything after " — ")
        text = _re.sub(r'\s*—.*', '', text)
        return len(text.split())

    wc = _count_hook_words(prompt)
    if wc > 6:
        print(f"  ⚠️  Hook word count = {wc} (limit 6) — requesting auto-trim …")
        trim_payload = {
            "contents": [{
                "role": "user",
                "parts": [{"text":
                    f"The Typography section of this image-generation prompt has {wc} words "
                    f"across its Line 1/2/3 fields.  The hard limit is 6 words TOTAL.\n\n"
                    f"Rewrite ONLY the Typography section so Lines 1+2+3 combined are ≤6 words.  "
                    f"Remove any subtitle or supporting phrase.  Return the COMPLETE corrected "
                    f"prompt — every section — with no other commentary.\n\n"
                    f"---\n{prompt}"
                }]
            }],
            "generationConfig": {"temperature": 0.1}
        }
        try:
            tr = requests.post(
                url, headers={"Content-Type": "application/json"},
                json=trim_payload, timeout=120
            )
            if tr.ok:
                trimmed = tr.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                new_wc  = _count_hook_words(trimmed)
                if new_wc <= 6:
                    prompt = trimmed
                    print(f"  ✓  Auto-trimmed to {new_wc} words")
                else:
                    print(f"  ⚠️  Trim returned {new_wc} words — using original")
        except Exception as _te:
            print(f"  ⚠️  Auto-trim failed: {_te} — using original")

    return prompt


# ══════════════════════════════════════════════════════════════════════
# PLAYWRIGHT HELPERS
# ══════════════════════════════════════════════════════════════════════
def _shot(page, label: str = ""):
    """Non-crashing debug screenshot."""
    try:
        DBG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        page.screenshot(path=str(DBG_DIR / f"{ts}_{label}.png"))
    except Exception:
        pass


def _probe(page, loc, timeout: int) -> bool:
    """Cheap existence check — polls loc.count() up to `timeout` ms instead of
    committing to Playwright's full wait_for(state="visible"), which blocks
    for the entire timeout even when a selector never matches anything. This
    is what let a long fallback-selector list turn into minutes of dead time
    when only the last selector in the list actually applied."""
    deadline = time.time() + (timeout / 1000)
    while time.time() < deadline:
        try:
            if loc.count() > 0:
                return True
        except Exception:
            pass
        page.wait_for_timeout(100)
    try:
        return loc.count() > 0
    except Exception:
        return False


def _hits_forbidden_icon(loc) -> bool:
    """True when the located element itself, or any icon glyph inside it, has
    a ligature name listed in FORBIDDEN_ICONS (e.g. 'article_spark').
    This is how a broad fallback selector (aria-label / text based) that
    accidentally resolves onto the AI prompt-polish icon button next to the
    Send arrow gets rejected instead of clicked."""
    if not FORBIDDEN_ICONS:
        return False
    js = f"""
    el => {{
      const forbidden = new Set({json.dumps(sorted(FORBIDDEN_ICONS))});
      const isIcon = el.matches('mat-icon, i, .google-symbols, [data-mat-icon-type]');
      const candidates = isIcon ? [el] : [];
      candidates.push(...el.querySelectorAll('mat-icon, i, .google-symbols, [data-mat-icon-type]'));
      for (const ic of candidates) {{
        const name = (ic.textContent || '').trim().toLowerCase();
        if (forbidden.has(name)) return true;
      }}
      return false;
    }}
    """
    try:
        return bool(loc.evaluate(js))
    except Exception:
        return False


def _click(page, sels: list, label: str, timeout: int = 15_000,
           post_check=None) -> bool:
    """Try each CSS / text selector in order; return True on first success.
    Only the FIRST (most likely / exact prod markup) selector gets the full
    `timeout` to appear. Every fallback selector after it only gets a quick
    ~1.2s existence probe — if it never matches, we move on immediately
    instead of waiting out the full timeout again, which is what made a
    5-6 selector fallback list take tens of seconds (or longer) per call.

    Safety rails added on top:
      • Forbidden-icon guard — a candidate that IS, or contains, one of the
        FORBIDDEN_ICONS glyph names (e.g. the 'article_spark' Gemini
        prompt-polish button next to the Send arrow) is NEVER clicked;
        we skip straight to the next selector instead.
      • post_check — when provided, the click only counts as successful if
        this callable returns True within a few seconds. This catches clicks
        that 'landed' on some button but did not produce the intended effect
        (a mis-click used to report ✓ and the run silently died later in the
        result-wait loop with 'Generation timed out')."""
    for i, sel in enumerate(sels):
        try:
            loc = page.locator(sel).first
            probe_timeout = timeout if i == 0 else min(1_200, timeout)
            if not _probe(page, loc, probe_timeout):
                continue
            loc.wait_for(state="visible", timeout=timeout)

            if _hits_forbidden_icon(loc):
                print(f"  ⛔  {label}: selector matched a forbidden icon "
                      f"({', '.join(sorted(FORBIDDEN_ICONS))}) — skipping it")
                continue

            loc.scroll_into_view_if_needed(timeout=5_000)
            loc.click(timeout=timeout)
            time.sleep(0.15)

            if post_check is None:
                print(f"  ✓  {label}")
                return True
            if post_check(page):
                print(f"  ✓  {label}  (effect verified)")
                return True
            print(f"  ↻  {label}: clicked, but the intended effect was not "
                  f"detected — trying next selector")
            continue
        except Exception:
            continue
    _shot(page, f"fail_{label[:25].replace(' ', '_')}")
    print(f"  ⚠️  Not found: {label}  ({len(sels)} selectors tried)")
    return False


def _read_field(loc) -> str:
    """Best-effort readback of whatever is currently in a field/contenteditable."""
    try:
        return loc.input_value(timeout=1_000)
    except Exception:
        pass
    try:
        return loc.inner_text(timeout=1_000)
    except Exception:
        return ""


def _fill(page, sels: list, text: str, label: str, timeout: int = 15_000,
          verify: bool = True, verify_attempts: int = 8, verify_interval_ms: int = 400) -> bool:
    """
    Fill the first found field.
    Tries native .fill() first (textarea/input), then keyboard for contenteditable.

    When verify=True (default) this does NOT just fire-and-forget the text in —
    it reads the field back afterwards and polls until the full string has
    actually landed. Some of these inputs (Angular contenteditable boxes,
    Google's own re-render-on-keystroke fields) lag behind what was typed, so
    trusting the return value of .fill()/keyboard.type() alone can leave a
    truncated / half-written prompt in the box. If it still doesn't match
    after polling, it clears and re-inserts once (still one shot, no
    letter-by-letter typing) before giving up.
    """
    def _norm(s: str) -> str:
        return " ".join((s or "").split())

    expected = _norm(text)

    def _await_match(loc) -> bool:
        for _ in range(verify_attempts):
            if _norm(_read_field(loc)) == expected:
                return True
            page.wait_for_timeout(verify_interval_ms)
        return _norm(_read_field(loc)) == expected

    for i, sel in enumerate(sels):
        try:
            loc = page.locator(sel).first
            # Same fast-fail probe as _click: only the first (most specific)
            # selector gets the full timeout, fallbacks get a quick existence
            # check so a 9-selector list (like the prompt box has) doesn't
            # burn 15s+ per non-matching selector before reaching the real one.
            probe_timeout = timeout if i == 0 else min(1_200, timeout)
            if not _probe(page, loc, probe_timeout):
                continue
            loc.wait_for(state="visible", timeout=timeout)

            # NOTE: we deliberately never use loc.fill(), Control+A, or a real
            # mouse .click() here. This field already has the uploaded
            # character-reference image embedded in it as an inline
            # "Ingredient" chip (with its own overlapping "cancel" remove
            # button) by the time we get here.
            #   - .fill() / Control+A select-and-replace the whole field,
            #     wiping the chip out.
            #   - a mouse .click() on the field lands at the element's
            #     center point, and if the chip (or its cancel overlay) sits
            #     under that point, the browser dispatches the click to the
            #     chip's cancel button instead of the empty text area —
            #     which is exactly what was silently removing the reference
            #     image.
            # .focus() sets DOM focus directly via JS with no mouse event at
            # all, so there is nothing for the chip to accidentally catch,
            # and Control+End + insert_text only appends after it.
            loc.focus(timeout=5_000)
            page.keyboard.press("Control+End")
            page.keyboard.insert_text(text)
            method = "insert_text (append, chip-safe)"

            if not verify:
                print(f"  ✓  {label}  ({method})")
                return True

            if _await_match(loc):
                print(f"  ✓  {label}  ({method}, verified {len(expected)} chars written)")
                return True

            # Didn't stabilize on the expected text — reinsert once, still
            # appending via focus() (not a click, not select-all) so the
            # reference chip survives.
            print(f"  ↻  {label}: text hasn't fully landed yet, retrying …")
            loc.focus(timeout=5_000)
            page.keyboard.press("Control+End")
            page.keyboard.insert_text(text)

            if _await_match(loc):
                print(f"  ✓  {label}  (retry, verified {len(expected)} chars written)")
                return True

            print(f"  ⚠️  {label}: could not verify full text match — proceeding with what's there")
            return True
        except Exception:
            continue
    _shot(page, f"fail_fill_{label[:20].replace(' ', '_')}")
    print(f"  ⚠️  Text field not found: {label}")
    return False


def _tile_ids(page) -> set:
    """Snapshot of identity tokens (data-media-id) for every tile currently in
    the result gallery (markup: flow-image-tile img[data-media-id], confirmed
    from the live production DOM). Used as the 'before Send' baseline so the
    freshly generated tile can be recognised as NEW afterwards."""
    try:
        return set(page.eval_on_selector_all(
            "img[data-media-id], flow-image-tile img",
            "els => els.map(e => e.getAttribute('data-media-id')).filter(v => !!v)",
        ))
    except Exception:
        return set()


_RESULT_TAG = "data-z-result"   # attribute used to tag the detected result <img>


def _img_srcs(page) -> set:
    """Snapshot of every image URL the page currently references: <img>
    currentSrc/src, <video> src/poster, AND CSS background-image urls (Flow's
    media gallery renders result cards as background-image divs, not <img>).
    Used as the 'before Send' baseline for the fallback result detector."""
    js = """
    () => {
      const out = [];
      for (const img of document.querySelectorAll('img'))
        out.push(img.currentSrc || img.getAttribute('src') || '');
      for (const v of document.querySelectorAll('video')) {
        out.push(v.currentSrc || v.src || '');
        if (v.poster) out.push(v.poster);
      }
      const els = document.querySelectorAll('div,section,a,button,li,article');
      for (const el of els) {
        const bi = getComputedStyle(el).backgroundImage;
        if (bi && bi !== 'none') {
          const m = bi.match(/url\\(["']?(.*?)['"]?\\)/);
          if (m) out.push(m[1]);
        }
      }
      return out.filter(Boolean);
    }
    """
    try:
        return set(page.evaluate(js))
    except Exception:
        return set()


def _install_img_baseline(page, srcs: set) -> None:
    """Cache the baseline src list on the page once — avoids re-serializing a
    potentially large list (signed/data URLs) on every poll."""
    try:
        page.evaluate(
            "srcs => { window.__zBaseline = srcs; }",
            sorted(s for s in srcs if s),
        )
    except Exception:
        pass


def _tag_new_result_img(page, baseline_srcs: set, min_px: int = 200) -> bool:
    """Fallback result detector — carrier-agnostic.

    Used when _find_new_result_tile() keeps coming up empty. The generation
    lands as a media card in the project gallery, frequently built as a
    CSS background-image div instead of an <img>. So this scans, newest-first:
      1. <img>     — new URL + fully decoded (complete && naturalWidth >= min_px)
      2. <video>   — new src/poster + readyState >= 2
      3. any card  — computed background-image URL not in the baseline,
                     rendered at least 120x60 px
    The winner is tagged with data-z-result so the wait loop can build a
    locator for it. The loop's stability gate (same URL on 2 consecutive
    polls) still applies before the candidate is accepted."""
    try:
        if not page.evaluate("() => !!window.__zBaseline"):
            _install_img_baseline(page, baseline_srcs)
    except Exception:
        return False
    js = """
    (minPx) => {
      if (!window.__zBaseline) return false;
      const baseline = new Set(window.__zBaseline);
      const boxOK = (el, w, h) => {
        const r = el.getBoundingClientRect();
        return r.width >= w && r.height >= h;
      };
      const imgs = document.querySelectorAll('img');
      for (let i = imgs.length - 1; i >= 0; i--) {
        const img = imgs[i];
        if (img.hasAttribute('data-z-result')) continue;
        const src = img.currentSrc || img.getAttribute('src') || '';
        if (!src || baseline.has(src)) continue;
        if (!img.complete || img.naturalWidth < minPx) continue;
        if (!boxOK(img, 10, 10)) continue;
        img.setAttribute('data-z-result', '1');
        return true;
      }
      const vids = document.querySelectorAll('video');
      for (let i = vids.length - 1; i >= 0; i--) {
        const v = vids[i];
        if (v.hasAttribute('data-z-result')) continue;
        const src = v.currentSrc || v.src || v.poster || '';
        if (!src || baseline.has(src)) continue;
        if (v.readyState < 2) continue;
        if ((v.videoWidth || 0) < minPx) continue;
        if (!boxOK(v, 10, 10)) continue;
        v.setAttribute('data-z-result', '1');
        return true;
      }
      const els = document.querySelectorAll('div,section,a,button,li,article');
      for (let i = els.length - 1; i >= 0; i--) {
        const el = els[i];
        if (el.hasAttribute('data-z-result')) continue;
        const bi = getComputedStyle(el).backgroundImage;
        if (!bi || bi === 'none') continue;
        const m = bi.match(/url\\(["']?(.*?)['"]?\\)/);
        if (!m) continue;
        const url = m[1];
        if (!url || baseline.has(url)) continue;
        if (!boxOK(el, 120, 60)) continue;
        el.setAttribute('data-z-result', '1');
        return true;
      }
      return false;
    }
    """
    try:
        return bool(page.evaluate(js, min_px))
    except Exception:
        return False


_nudged_logged: set = set()


def _nudge_lazy_imgs(page) -> None:
    """Flow lazy-loads gallery tiles that sit below the fold: their <img> has
    a src but the browser never fetches it, so it never becomes 'complete'
    and both result detectors stay empty forever (imgs-on-page grows while
    result-tiles stays 0 — exactly the observed failure). This scrolls the
    first not-yet-loaded NEW image into view to kick-start the fetch."""
    js = """
    () => {
      if (!window.__zBaseline) return null;
      const baseline = new Set(window.__zBaseline);
      const imgs = document.querySelectorAll('img');
      for (let i = imgs.length - 1; i >= 0; i--) {
        const img = imgs[i];
        if (img.hasAttribute('data-z-result')) continue;
        const src = img.currentSrc || img.getAttribute('src') || '';
        if (!src || baseline.has(src)) continue;
        if (img.complete && img.naturalWidth > 0) continue;   // healthy
        img.scrollIntoView({block: 'center', behavior: 'instant'});
        return src.slice(0, 80);
      }
      return null;
    }
    """
    try:
        src = page.evaluate(js)
    except Exception:
        return
    if src and src not in _nudged_logged:
        _nudged_logged.add(src)
        print(f"    👀  New image not loaded yet — scrolled it into view ({src}…)")


def _dump_img_state(page) -> None:
    """One-shot deep diagnostic printed right before giving up: every <img>
    on the page with the exact attributes the detectors depend on, plus the
    aria-labels of all flow tiles. Makes a silent no-result timeout fully
    debuggable from the console alone."""
    js = """
    () => {
      const tail = (u) => (u || '').split('/').pop().slice(-40);
      const rows = [];
      for (const img of document.querySelectorAll('img')) {
        const r = img.getBoundingClientRect();
        rows.push({
          src: tail(img.currentSrc || img.getAttribute('src') || ''),
          mid: img.getAttribute('data-media-id') || '',
          complete: img.complete,
          natW: img.naturalWidth,
          box: Math.round(r.width) + 'x' + Math.round(r.height),
          vis: !!(r.width && r.height) &&
               getComputedStyle(img).visibility !== 'hidden',
          tagged: img.hasAttribute('data-z-result'),
          tile: !!(img.closest('flow-image-tile, flow-grid-tile-container')),
        });
      }
      const tiles = [...document.querySelectorAll('flow-grid-tile-container')]
        .map(t => t.getAttribute('aria-label') || '(no aria-label)');
      return {rows, tiles};
    }
    """
    try:
        info = page.evaluate(js)
    except Exception:
        return
    rows, tiles = info.get("rows", []), info.get("tiles", [])
    print(f"    🔎  <img> inventory at timeout ({len(rows)} imgs, {len(tiles)} flow-tiles):")
    for r in rows:
        print(f"       • src…/{r['src']}  media-id={r['mid'][:12] or '-'}  "
              f"complete={r['complete']}  natW={r['natW']}  box={r['box']}  "
              f"visible={r['vis']}  tagged={r['tagged']}  in-flow-tile={r['tile']}")
    for t in tiles:
        print(f"       ▪ flow tile aria-label: {t}")


def _find_new_result_tile(page, baseline_ids: set, exclude_text: str,
                          prefer_text: str = ""):
    """
    Look at every gallery tile in the result area and return a Locator for the
    result <img>, or None if nothing usable is found yet.

    Handles the current Flow markup (confirmed from the live production DOM):
      flow-grid-tile-container > flow-tile-container >
      flow-image-tile > img.image[data-media-id]

    Rules, in order of priority:
      1. NEVER return a tile whose caption/aria-label mentions `exclude_text`
         — this filters out the uploaded character-reference tile, which
         shares the same markup as real results.
      2. NEVER return a tile whose media-id was already
         on the page before Send was clicked — it predates this generation.
      3. Among the remaining tiles, prefer one whose caption contains
         `prefer_text` (the video title — Flow captions generated results
         with the prompt title, e.g. aria-label="Floating face in dark
         courtroom"), then the newest one in DOM order.
    """
    candidates = {}  # media-id -> {"img": locator, "cap": caption text}
    tiles = page.locator("flow-grid-tile-container")
    try:
        count = tiles.count()
    except Exception:
        return None
    for i in range(count):
        tile = tiles.nth(i)
        try:
            img = tile.locator("img").first
            if img.count() == 0:
                continue
            tile_id = img.get_attribute("data-media-id")
            if not tile_id or tile_id in candidates:
                continue
            try:
                caption = (tile.get_attribute("aria-label") or "") + " " \
                          + tile.inner_text()
            except Exception:
                caption = ""
        except Exception:
            continue
        if exclude_text and exclude_text.lower() in caption.lower():
            continue  # the uploaded reference image — never pick this one
        candidates[tile_id] = {"img": img, "cap": caption}

    if not candidates:
        return None

    def _pick(pool):
        if prefer_text:
            hits = [c["img"] for c in pool if prefer_text.lower() in c["cap"].lower()]
            if hits:
                return hits[-1]
        return pool[-1]["img"]

    fresh = [c for tid, c in candidates.items() if tid not in baseline_ids]
    return _pick(fresh if fresh else list(candidates.values()))


# ══════════════════════════════════════════════════════════════════════
# GOOGLE AUTH
# ══════════════════════════════════════════════════════════════════════
def _is_logged_in(page) -> bool:
    """
    Three-layer check:
      1. URL: if we landed on accounts.google.com → not logged in (most reliable)
      2. Selectors: look for any known signed-in Google indicator on the page
      3. Fallback: if still on labs.google (not redirected to login) → assume logged in
    """
    # 1. Explicit redirect to sign-in page
    if "accounts.google.com" in page.url:
        print(f"    URL: {page.url[:80]}")
        return False

    # 2. Look for a signed-in indicator (avatar, account menu, etc.)
    try:
        page.wait_for_selector(
            "img[alt*='@'], [data-email], #avatar-btn, "
            "[aria-label*='Google Account'], [aria-label*='profile' i], "
            "[data-authuser], [aria-label*='account' i], "
            "a[href*='SignOutOptions'], a[href*='myaccount.google.com']",
            timeout=5_000,
        )
        return True
    except PWTimeout:
        pass

    # 3. Still on Flow URL = not redirected to login = logged in
    if "labs.google" in page.url:
        return True

    return False


def _google_login(page, email: str, pw: str):
    print("🔐  Logging in to Google …")
    page.goto(
        "https://accounts.google.com/signin",
        wait_until="domcontentloaded",
        timeout=40_000,
    )
    page.wait_for_timeout(1500)

    # ── email ──────────────────────────────────────────────────────────
    _fill(page,
          ["input[type='email']", "#identifierId", "input[name='identifier']"],
          email, "email field", timeout=20_000)
    _click(page,
           ["button:has-text('Next')", "#identifierNext", "[data-action='next']"],
           "Next (email)")
    page.wait_for_timeout(2500)

    # ── password ───────────────────────────────────────────────────────
    _fill(page,
          ["input[type='password']", "input[name='password']",
           "[aria-label*='password' i]"],
          pw, "password field", timeout=15_000)
    _click(page,
           ["button:has-text('Next')", "#passwordNext", "[data-action='next']"],
           "Next (password)")
    page.wait_for_timeout(3000)

    # ── extra verification (2FA, phone, captcha) ───────────────────────
    if "accounts.google.com" in page.url:
        print()
        print("⚠️  Google needs extra verification.")
        print("    Complete it in the browser window, then press ENTER here …")
        input()

    print("  ✓  Logged in")


# ══════════════════════════════════════════════════════════════════════
# MODEL PICKER  (toolbar dropdown — e.g. "🍌 Nano Banana 2" → choose model)
# ══════════════════════════════════════════════════════════════════════
def _select_model(page, model_name: str = "Nano Banana Pro"):
    """
    Open the model-picker dropdown (toolbar button showing the current
    model, e.g. '🍌 Nano Banana 2', aria-haspopup='menu') and select
    `model_name` from the menu that opens. Non-fatal — only warns if the
    dropdown or the target option can't be found.
    """
    print(f"  🍌  Setting model → {model_name} …")

    TRIGGER_SELS = [
        "button[aria-haspopup='menu']:has-text('Nano Banana')",
        "button[aria-haspopup='menu']:has-text('🍌')",
        "button:has-text('Nano Banana')",
    ]

    # Already on the right model? — skip opening the menu at all
    try:
        trigger = page.locator(", ".join(TRIGGER_SELS)).first
        if trigger.count() and model_name.lower() in trigger.inner_text().lower():
            print(f"  ✓  Model already set to {model_name}")
            return
    except Exception:
        pass

    opened = _click(page, TRIGGER_SELS, "model picker dropdown", timeout=8_000)
    if not opened:
        print("  ⚠️  Model picker not found — skipping (default model assumed)")
        return

    page.wait_for_timeout(150)

    picked = _click(page,
        [f"[role='menuitem']:has-text('{model_name}')",
         f"[role='menuitemradio']:has-text('{model_name}')",
         f"[role='option']:has-text('{model_name}')",
         f"li:has-text('{model_name}')",
         f"span:has-text('{model_name}')",
         f"div:has-text('{model_name}')"],
        f"'{model_name}' menu item", timeout=6_000)

    if not picked:
        print(f"  ⚠️  '{model_name}' option not found in dropdown — check .debug/ screenshots")
        _shot(page, "model_picker_no_option")
        page.keyboard.press("Escape")   # close the menu so it doesn't block later clicks
        return

    page.wait_for_timeout(150)
    print(f"  ✓  Model set to {model_name}")


# ══════════════════════════════════════════════════════════════════════
# SETTINGS PANEL  (non-fatal — only warns if panel is not found)
# ══════════════════════════════════════════════════════════════════════
def _verify_settings(page):
    print("  ⚙️   Opening settings panel …")

    opened = _click(page,
        ["button:has(i:has-text('tune'))",           # actual HTML uses <i> not mat-icon
         "button:has(.google-symbols:has-text('tune'))",
         "button[aria-label*='tune' i]",
         "button[aria-label*='setting' i]",
         "button[title*='tune' i]",
         "[data-icon='tune']"],
        "tune / settings icon", timeout=8_000)

    if not opened:
        print("  ⚠️  Settings panel not found — skipping (defaults assumed correct)")
        return

    page.wait_for_timeout(350)

    # Never confirm before generating
    _click(page,
        ["button[role='radio'][value='AUTO_APPROVE']",  # exact value from HTML
         "button[role='radio']:has-text('Never')",
         "label:has-text('Never')",
         "span:has-text('Never')",
         "[aria-label*='never' i]"],
        "Never (auto-generate)", timeout=5_000)

    # 16:9 aspect ratio — already default but click to confirm
    # Real prod markup is a <mat-button-toggle>/<button> with NO role='tab'
    # and no aria-label — just a nested <span class="toggle-text">16:9</span>.
    _click(page,
        ["button[role='tab'][id*='LANDSCAPE']:has-text('16:9')",  # from prod HTML
         "button[role='tab']:has-text('16:9')",
         "button[aria-label='16:9']",
         "label:has-text('16:9')", "[data-ratio='16:9']",
         "mat-button-toggle:has-text('16:9')",     # actual prod element (no role='tab')
         ".toggle-text:has-text('16:9')",          # exact span class captured from prod HTML
         "button:has-text('16:9')"],               # generic fallback
        "16:9 ratio", timeout=5_000)

    # 1× scale
    # Real prod markup labels this toggle "x1" (reversed), not "1x", and again
    # has no role='tab' / aria-label — it's a <span class="toggle-text">x1</span>.
    _click(page,
        ["button[role='tab']:has-text('1x')",   # from prod HTML — tab not button
         "button[role='tab'][id*='-trigger-1']",
         "button[aria-label='1x']",
         "label:has-text('1x')", "[data-scale='1x']",
         "mat-button-toggle:has-text('x1')",       # actual prod text order is "x1"
         ".toggle-text:has-text('x1')",            # exact span class captured from prod HTML
         "button:has-text('x1')",                  # generic fallback, correct text order
         "button:has-text('1x')"],                 # generic fallback, in case order varies
        "1× scale", timeout=5_000)

    # Image generation default model — lives in this same panel, right
    # below the ratio/scale tabs, so it must run while the panel is open.
    _select_model(page, "Nano Banana Pro")

    # Close / Save
    _click(page,
        ["button:has-text('Save')", "button:has-text('Done')",
         "button:has-text('Close')", "[mat-dialog-close]",
         "button[aria-label*='close' i]"],
        "Close/Save settings", timeout=5_000)

    page.wait_for_timeout(200)
    print("  ✓   Settings verified")


def _generation_started(page, wait_s: float = 8.0) -> bool:
    """post_check for the Send / Generate click — True only when Flow shows a
    concrete signal that generation actually began (a stop/pause button
    appears, the send arrow becomes disabled, the prompt box is emptied, or
    a generating/queued status text shows up).

    Guards against the click having landed on a wrong prompt-bar button
    (e.g. the 'article_spark' prompt-polish icon), which used to report ✓
    and then leave the run silently dead until the result-wait timeout."""
    # NOTE: deliberately NO 'cancel' signal here — the uploaded reference
    # ingredient chip carries its own cancel/remove button, which matched
    # button[aria-label*='cancel' i] and could fake a "generation started"
    # positive (Send reporting "effect verified" while nothing is running).
    start_sigs = [
        "button:has(.google-symbols:has-text('stop'))",
        "button:has(mat-icon:has-text('stop'))",
        "button[aria-label*='stop' i]",
        "button[aria-label*='pause' i]",
        "button[aria-disabled='true']:has(i:has-text('arrow_forward'))",
        "button[aria-disabled='true']:has(mat-icon:has-text('arrow_forward'))",
        "button[disabled]:has(i:has-text('arrow_forward'))",
        "button[disabled]:has(mat-icon:has-text('arrow_forward'))",
    ]
    status_re = re.compile(r"\b(generating|rendering|queued|in progress|working on it)\b", re.I)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        for sig in start_sigs:
            try:
                if page.locator(sig).count() > 0:
                    return True
            except Exception:
                pass
        try:
            if page.get_by_text(status_re).count() > 0:
                return True
        except Exception:
            pass
        # Prompt box emptied right after the click = Flow accepted the job
        try:
            box = page.locator(".ProseMirror[contenteditable='true']").first
            if box.count() and not (box.inner_text() or "").strip():
                return True
        except Exception:
            pass
        page.wait_for_timeout(400)
    return False


# ══════════════════════════════════════════════════════════════════════
# DOWNLOAD HARVESTING  (page-lifetime independent)
# ══════════════════════════════════════════════════════════════════════
# Google Flow closes its own tab the instant a download starts. Playwright's
# Download object (save_as / path) is bound to that page — it dies with it
# (TargetClosedError) and Playwright deletes its own artifact copy on close.
# So we take Playwright out of the loop entirely: a browser-level CDP
# Browser.setDownloadBehavior points Chromium at BROWSER_DL, and we harvest
# the finished file off disk ourselves. Chromium keeps writing even after the
# initiating tab is gone, so this survives the close.

_DL_PARTIAL = (".crdownload", ".part", ".tmp", ".download")


def _arm_browser_downloads(context, page) -> bool:
    """Force Chromium to save downloads into BROWSER_DL with real filenames."""
    for behaviour in ("allow", "allowAndName"):
        try:
            cdp = context.new_cdp_session(page)
            cdp.send("Browser.setDownloadBehavior", {
                "behavior":      behaviour,
                "downloadPath":  str(BROWSER_DL),
                "eventsEnabled": True,
            })
            print(f"  📥  Download dir armed : {BROWSER_DL}  (behavior={behaviour})")
            return True
        except Exception as e:
            last = e
    print(f"  ⚠️  Could not arm browser download dir ({type(last).__name__}) "
          f"— falling back to Playwright save_as")
    return False


def _dl_snapshot() -> set:
    try:
        return {f.name for f in BROWSER_DL.glob("*") if f.is_file()}
    except Exception:
        return set()


def _harvest_download(before: set, after_ts: float,
                      timeout: float = 120.0, min_size: int = 20_000):
    """Wait for a NEW, fully-written file to appear in BROWSER_DL.

    Returns Path or None. A file counts as done when it has no partial
    extension and its size stops changing for two consecutive polls.
    """
    deadline = time.time() + timeout
    sizes: dict = {}
    announced = False
    while time.time() < deadline:
        try:
            files = [f for f in BROWSER_DL.glob("*") if f.is_file()]
        except Exception:
            files = []

        partial = [f for f in files if f.suffix.lower() in _DL_PARTIAL]
        if partial and not announced:
            print(f"  …  transfer in progress ({partial[0].name}) …")
            announced = True

        ready = []
        for f in files:
            if f.suffix.lower() in _DL_PARTIAL or f.name in before:
                continue
            try:
                st = f.stat()
            except Exception:
                continue           # vanished mid-scan (renamed by Chromium)
            if st.st_mtime < after_ts - 30 or st.st_size < min_size:
                continue
            ready.append((st.st_mtime, st.st_size, f))

        for _mt, size, f in sorted(ready, key=lambda r: -r[0]):
            if sizes.get(f.name) == size:            # size stable → complete
                return f
            sizes[f.name] = size

        time.sleep(1.2)
    return None


# ══════════════════════════════════════════════════════════════════════
# MAIN FLOW AUTOMATION
# ══════════════════════════════════════════════════════════════════════
def run_flow(cfg: dict, img_prompt: str) -> Path:
    email = cfg["youtube"]["email"]
    pw    = cfg["youtube"]["pass"]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DL.mkdir(parents=True, exist_ok=True)
    BROWSER_DL.mkdir(parents=True, exist_ok=True)
    # Clear stale/partial files so the harvester can't pick up an old run.
    for _f in BROWSER_DL.glob("*"):
        try:
            _f.unlink()
        except Exception:
            pass

    with sync_playwright() as pw_:

        # ── build browser context ──────────────────────────────────────
        # Persistent Chrome profile — login is saved across runs.
        # Google blocks automated Chromium; using a persistent real-Chrome
        # profile means Google sees a normal browser with existing cookies.
        CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
        print(f"  🔑  Chrome profile  : {CHROME_PROFILE}")
        context = pw_.chromium.launch_persistent_context(
            user_data_dir = str(CHROME_PROFILE),
            channel       = "chrome",      # use real installed Chrome
            headless      = False,
            slow_mo       = 120,
            viewport      = {"width": 1440, "height": 900},
            accept_downloads = True,
            args          = ["--start-maximized",
                             "--disable-blink-features=AutomationControlled"],
        )
        context.set_default_timeout(30_000)

        # ── neutralize self-close ────────────────────────────────────────
        # Root cause of the download failures: Flow calls window.close() on
        # its own page right after triggering the "2K Upscaled" download.
        # Chrome normally only lets a script close a window IT opened via
        # window.open() — but that restriction is relaxed for CDP/automation
        # -driven browsers, which is exactly what Playwright is. So the call
        # succeeds, the page's target is destroyed mid-transfer, and with it
        # the in-memory blob backing the download — nothing is left on disk
        # for any rescue path to recover. Overriding window.close() to a
        # no-op on every page in this context (current and future) stops the
        # page from ever disappearing, so the download has time to finish
        # and save_as() gets a page that's still alive.
        context.add_init_script("window.close = () => {};")

        page = context.new_page()

        # ── keeper tab ─────────────────────────────────────────────────
        # Chromium exits the moment its LAST tab closes. Flow has been seen
        # closing its page right after a download starts, which killed the
        # entire browser mid-transfer (TargetClosedError on save_as). This
        # idle background tab guarantees the browser stays open until the
        # script itself closes it — only after "✅ Thumbnail saved". Kept as
        # a second line of defense even with window.close() neutralized.
        keeper = context.new_page()
        try:
            keeper.goto("about:blank")
        except Exception:
            pass
        page.bring_to_front()

        _intentional_close = False

        def _on_page_close(_p):
            if not _intentional_close:
                print("\n  ⚠️  Flow closed its page — keeper tab is holding the "
                      "browser open, the download will still be saved …")

        page.on("close", _on_page_close)

        # ── network image tracker ──────────────────────────────────
        # Every DISPLAYED image must arrive as a network response. Recording
        # image URLs + timestamps gives a generation-result trail that is
        # completely independent of Flow's DOM structure, and lets us rescue
        # the file directly if the UI detection fails.
        seen_img_urls: dict = {}

        def _on_response(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "") or ""
                if ct.startswith("image/") or resp.request.resource_type in ("image", "media"):
                    seen_img_urls[resp.url] = {"t": time.time(), "ct": ct}
            except Exception:
                pass

        page.on("response", _on_response)
        # Flow's result can land on a popup/new tab — track those too, so the
        # network rescue still has URLs after the main page is gone.
        context.on("page", lambda p: p.on("response", _on_response))

        # ── browser-level download dir ─────────────────────────────────
        # Armed on the KEEPER page: Browser.setDownloadBehavior is browser-wide
        # and the keeper never closes, so the setting outlives Flow's tab.
        dl_armed = _arm_browser_downloads(context, keeper)

        # ── 1. Navigate ────────────────────────────────────────────────
        print("\n🌐  Opening Google Flow …")
        page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(2000)
        _shot(page, "01_start")

        # ── 2. Login if needed ─────────────────────────────────────────
        print("🔍  Checking login status …")
        if _is_logged_in(page):
            print("  ✓  Already logged in — skipping login")
        else:
            print("  ⚠️  Not logged in — starting Google login …")
            _google_login(page, email, pw)
            # Profile is persistent — session already saved automatically.
            print("  ✓  Login saved to persistent Chrome profile")
            # Re-navigate after login
            page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2500)
            # Confirm login succeeded
            if _is_logged_in(page):
                print("  ✓  Login confirmed")
            else:
                print("  ⚠️  Login may have failed — check the browser window")
        _shot(page, "02_logged_in")

        # ── 3. New project ─────────────────────────────────────────────
        print("\n📁  Creating new project …")
        ok = _click(page,
            ["button:has-text('New project')",
             "a:has-text('New project')",
             "[aria-label*='New project' i]",
             "button[data-action*='new' i]",
             "button:has-text('New Project')"],
            "New project", timeout=25_000)
        if not ok:
            _shot(page, "03_no_new_project")
            raise RuntimeError("'New project' button not found — check .debug/ screenshots")
        page.wait_for_timeout(3000)
        _shot(page, "03_new_project")

        # ── 4. Agent mode ──────────────────────────────────────────────
        print("🤖  Checking Agent mode …")
        # The button itself toggles aria-pressed="true"/"false" — that's the
        # only reliable signal. (The old selector matched the "Agent" <span>
        # text alone, which is present in BOTH the on and off markup, so it
        # always reported "already active" — even when aria-pressed='false'.
        # That false positive is also why the settings panel never showed up:
        # it only renders once Agent mode is genuinely on.)
        AGENT_ON_SEL  = "button[aria-pressed='true']:has-text('Agent')"
        AGENT_OFF_SEL = "button[aria-pressed='false']:has-text('Agent')"

        already_agent = False
        try:
            page.wait_for_selector(AGENT_ON_SEL, timeout=3_000)
            already_agent = True
        except Exception:
            pass

        if already_agent:
            print("  ✓  Agent mode already active")
        else:
            _click(page,
                [AGENT_OFF_SEL,
                 "button:has-text('Agent')",
                 "[role='tab']:has-text('Agent')",
                 "mat-chip:has-text('Agent')",
                 "[aria-label*='Agent mode' i]",
                 "div[data-mode='agent']"],
                "Agent mode", timeout=12_000)

            # Confirm the toggle actually flipped on before moving on.
            try:
                page.wait_for_selector(AGENT_ON_SEL, timeout=5_000)
                print("  ✓  Agent mode activated")
            except Exception:
                print("  ⚠️  Could not confirm Agent mode turned on — check .debug/ screenshots")
        page.wait_for_timeout(1000)
        _shot(page, "04_agent_mode")

        # ── 5. Upload character reference ──────────────────────────────
        print("📎  Uploading character reference …")

        ADD_SELS = [
            "button:has(mat-icon.add-menu-icon)",  # exact: the (+) Create/attach button (current Flow DOM)
            "button:has(mat-icon:has-text('add'))",
            "button[aria-haspopup='dialog']:has(i:has-text('add_2'))",  # legacy: the (+) Create/attach button
            "button:has(i:has-text('add_2'))",
            "button[aria-controls]:has(i:has-text('add_2'))",
            "button[aria-label*='attach' i]",
            "button:has-text('+')",
        ]
        UPL_SELS = [
            "[role='menuitem']:has-text('Upload')",
            "button:has-text('Upload media')",
            "li:has-text('Upload media')",
            "span:has-text('Upload media')",
            "[role='option']:has-text('Upload')",
            "div:has-text('Upload media')",
        ]

        uploaded = False

        # Attempt A — intercept file-chooser dialog while navigating the (+) menu
        try:
            with page.expect_file_chooser(timeout=15_000) as fc_ctx:
                # Open the (+) / attach menu
                _click(page, ADD_SELS, "add (+) button", timeout=10_000)
                page.wait_for_timeout(700)
                # Click "Upload media" inside the menu
                _click(page, UPL_SELS, "Upload media menu item", timeout=8_000)
            fc_ctx.value.set_files(str(CHAR_REF))
            uploaded = True
        except (PWTimeout, Exception) as exc:
            print(f"  ↻  File-chooser intercept failed ({type(exc).__name__}) — trying fallback …")

        # Attempt B — hidden <input type="file"> already on page
        if not uploaded:
            try:
                fi = page.locator("input[type='file']")
                if fi.count() > 0:
                    fi.first.set_input_files(str(CHAR_REF))
                    uploaded = True
                    print("  ✓  Uploaded via hidden file input")
            except Exception as exc:
                print(f"  ↻  Direct file input failed ({type(exc).__name__})")

        if not uploaded:
            _shot(page, "05_upload_failed")
            raise RuntimeError(
                "Cannot upload character reference — check .debug/ screenshots"
            )

        print(f"  ✓  Uploaded        : {CHAR_REF.name}")
        print(f"  ⏳  Waiting {UPLOAD_SETTLE_WAIT}s for upload to finish processing …")
        page.wait_for_timeout(UPLOAD_SETTLE_WAIT * 1000)

        # After upload a dialog may show "Add to Prompt" — click it if present
        added = _click(page,
            ["button:has-text('Add to Prompt')",
             "button:has-text('Add to prompt')",
             "button.gecFQL:has-text('Add to Prompt')",   # literal class from prod HTML (fallback)
             "button:has-text('Add')"],
            "Add to Prompt", timeout=6_000)
        if not added:
            print("  ℹ️   No 'Add to Prompt' dialog — image already in prompt area")

        page.wait_for_timeout(2000)
        try:
            page.wait_for_selector(".ProseMirror[contenteditable='true']", state="visible", timeout=10_000)
        except Exception:
            pass
        _shot(page, "05_uploaded")

        # ── 6. Insert generated prompt ─────────────────────────────────
        print("✍️   Inserting image-generation prompt …")
        ok = _fill(page,
            ["flow-rich-text-editor .ProseMirror[contenteditable='true']:visible",  # exact prod markup, visible instance only
             "flow-rich-text-editor div[contenteditable='true']:visible",
             ".ProseMirror[contenteditable='true']:visible",
             "div.ProseMirror[contenteditable='true']:visible",
             "flow-rich-text-editor .ProseMirror[contenteditable='true']",  # non-:visible fallback, in case :visible mis-evaluates
             ".ProseMirror[contenteditable='true']",
             "div.ProseMirror[contenteditable='true']",
             "textarea[placeholder*='prompt' i]",
             "textarea[placeholder*='message' i]",
             "textarea[placeholder*='describe' i]",
             "textarea[placeholder*='type' i]",
             "div[role='textbox'][contenteditable='true']",
             "[aria-label*='prompt' i]",
             "[data-placeholder*='type' i]",
             "textarea",
             "[contenteditable='true']:visible",
             "[contenteditable='true']"],
            img_prompt, "prompt input")

        if not ok:
            _shot(page, "06_no_prompt_input")
            raise RuntimeError("Prompt text field not found — check .debug/ screenshots")

        page.wait_for_timeout(60)

        # ── 7. Verify settings ─────────────────────────────────────────
        _verify_settings(page)
        _shot(page, "07_settings_done")

        # ── 8. Send / Generate ─────────────────────────────────────────
        print("🚀  Starting generation …")
        baseline_tile_ids = _tile_ids(page)   # snapshot BEFORE generating, so the
                                               # uploaded reference tile (if already
                                               # present) doesn't look "new" later
        baseline_img_srcs = _img_srcs(page)   # broader baseline: every <img> src on
                                               # the page, for the fallback detector
                                               # in case the result-tile markup changed
        _install_img_baseline(page, baseline_img_srcs)
        baseline_net_urls = set(seen_img_urls)   # network-level image URL baseline
                                                 # (for the direct-download rescue)
        # NOTE: the prompt bar also carries AI-assist icon buttons (e.g. the
        # 'article_spark' Gemini prompt-polish button) right next to the real
        # send arrow. Two protections apply here:
        #   1. the forbidden-icon guard inside _click() refuses to click any
        #      element that is / contains a FORBIDDEN_ICONS glyph;
        #   2. post_check=_generation_started only accepts the click once Flow
        #      actually shows a generation-started signal, otherwise the next
        #      selector is tried.
        sent = _click(page,
            ["button:has(i:has-text('arrow_forward'))",                       # prod HTML (<i> icon variant)
             "button:has(mat-icon:has-text('arrow_forward'))",                # prod HTML (mat-icon variant)
             "button[aria-disabled='false']:has(i:has-text('arrow_forward'))",
             "button[aria-disabled='false']:has(mat-icon:has-text('arrow_forward'))",
             "button.sc-26b30722-5",                                          # prod class on send btn
             "button[aria-label*='send' i]",
             "button[aria-label*='generate' i]",
             "button[aria-label*='submit' i]",
             "button[type='submit']",
             "button:has-text('Generate')",
             "button:has-text('Send')",
             "button:has-text('Run')"],
            "Send / Generate", timeout=15_000,
            post_check=_generation_started)
        if not sent:
            print("  ⚠️  Could not confirm generation started — continuing to wait anyway")
            print("      (if no result appears, check .debug/ screenshots)")

        t0 = time.time()
        _shot(page, "08_sent")

        # ── 9. Wait for result image ───────────────────────────────────
        page.wait_for_timeout(10 * 1000)
        print(f"⏳  Waiting for result  (full {GEN_TIMEOUT}s window — the image is "
              "opened only after all processing completes) …")

        REF_EXCLUDE = CHAR_REF.stem   # "character_reference" — never select this tile
        title = cfg.get("video_idea", {}).get("title", "")   # Flow captions the
                              # result tile with the prompt title (aria-label) —
                              # used to prefer the right tile when in doubt
        result_loc     = None
        last_src        = None
        stable_polls     = 0
        STABLE_POLLS_REQUIRED = 2   # same fully-loaded <img> src on 2 consecutive polls
        not_ready_polls        = 0  # consecutive polls with a visible-but-undecoded tile
        NOT_READY_ACCEPT_POLLS = 20 # ~30s of polls — safety valve, see below
        deadline    = time.time() + GEN_TIMEOUT

        def _img_ready(img_loc) -> bool:
            """True once the candidate has actually finished rendering — not
            merely present/visible. For <img> that means browser-decoded with
            non-zero natural size; for other carriers (background-image cards,
            <video>) being visible with a rendered box is enough, since their
            content loading isn't observable via complete/naturalWidth."""
            try:
                return bool(img_loc.evaluate(
                    "el => (el.tagName === 'IMG')"
                    " ? (el.complete && el.naturalWidth > 0)"
                    " : true"))
            except Exception:
                return False

        # We deliberately do NOT open the image the moment it is detected.
        # Two reasons, both learned the hard way:
        #   1. Flow sometimes drops in a placeholder/partial tile before the
        #      real image has finished rendering — grabbing that one early
        #      means downloading the wrong/incomplete frame.
        #   2. Per user request: the image is opened ONLY after the FULL
        #      GEN_TIMEOUT window, so Flow has entirely finished processing.
        #      Opening it early produced a viewer with no Download button.
        # So: keep polling for the full window; a candidate is recorded once
        # its <img> reports fully decoded AND its src has stayed identical
        # across two consecutive polls (proof Flow isn't still swapping it
        # out), but the loop never breaks early.
        last_diag = -30   # elapsed-seconds value at which diagnostics last printed
        while time.time() < deadline:
            candidate = _find_new_result_tile(page, baseline_tile_ids, REF_EXCLUDE,
                                              prefer_text=title)
            if candidate is None:
                # Fallback: detect ANY freshly-rendered <img> whose src was not
                # on the page before Send was clicked — covers Flow changing the
                # result-tile markup (which is how a "✓ Send (effect verified)"
                # run can still end with zero tiles found).
                if _tag_new_result_img(page, baseline_img_srcs):
                    candidate = page.locator(f"[{_RESULT_TAG}='1']").last
                else:
                    # Neither detector fired. Flow lazy-loads gallery tiles that
                    # sit below the fold — their <img> has a src but the browser
                    # never fetches it, so it can never look "decoded". Scroll
                    # the first unloaded new image into view to kick-start it.
                    _nudge_lazy_imgs(page)
            if candidate is not None:
                try:
                    visible = candidate.is_visible()
                except Exception:
                    visible = False

                if visible and _img_ready(candidate):
                    not_ready_polls = 0
                    try:
                        # Stable identity for ANY carrier: img/video use
                        # currentSrc/src, background-image cards use their
                        # resolved CSS url(...)
                        src = candidate.evaluate(
                            "el => (el.tagName === 'IMG' || el.tagName === 'VIDEO')"
                            " ? (el.currentSrc || el.getAttribute('src') || el.poster || '')"
                            " : ((getComputedStyle(el).backgroundImage.match(/url\\([\"']?(.*?)[\"']?\\)/) || ['', ''])[1])")
                    except Exception:
                        src = None
                    if src and src == last_src:
                        stable_polls += 1
                    else:
                        stable_polls = 1
                        last_src = src
                    if stable_polls >= STABLE_POLLS_REQUIRED:
                        if result_loc is None:
                            print("\n  ✓  Result image detected — holding for the full "
                                  f"{GEN_TIMEOUT}s window so Flow can finish all "
                                  "processing, then it will be opened …")
                        result_loc = candidate
                        # Deliberately NO early break: the image is only opened
                        # AFTER the full generation window has elapsed, so Flow
                        # has entirely finished processing (opening it early
                        # showed a viewer with no Download button). If Flow
                        # swaps the preview src for a final render while we
                        # hold, the element locator keeps pointing at it.
                else:
                    stable_polls = 0
                    last_src = None
                    # Visible but not decoded yet (or hidden) — nudge it into
                    # the viewport so lazy tiles actually start loading.
                    try:
                        candidate.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    if visible:
                        not_ready_polls += 1
                        if not_ready_polls >= NOT_READY_ACCEPT_POLLS and result_loc is None:
                            # Same tile, same non-empty src, laid out for ~30s
                            # of consecutive polls, yet the browser never
                            # reports it decoded. Accept it anyway: the
                            # download step uses Flow's own 'Download media'
                            # button (a fresh server-side fetch), NOT this
                            # <img> element's pixels.
                            print("\n  ℹ️   Result tile present but never reported "
                                  "decoded — accepting it anyway")
                            result_loc = candidate
                    else:
                        not_ready_polls = 0

            elapsed   = int(time.time() - t0)
            remaining = max(0, int(deadline - time.time()))
            if elapsed - last_diag >= 30:
                # Periodic diagnostics — makes a silent no-result timeout
                # debuggable from the console alone.
                last_diag = elapsed
                try:
                    tiles_n = page.locator("flow-grid-tile-container").count()
                    media_n = page.locator("img[data-media-id], flow-image-tile img").count()
                    imgs_n  = page.locator("img").count()
                    new_net = sum(1 for u in seen_img_urls if u not in baseline_net_urls)
                    print(f"\n    ⏱  {elapsed}s: result-tiles={tiles_n}, media-imgs={media_n}, imgs-on-page={imgs_n}, new-net-imgs={new_net}")
                except Exception:
                    print(f"\n    ⏱  {elapsed}s: (diagnostics unavailable)")
                _shot(page, f"wait_{elapsed:03d}s")
            else:
                print(f"    … {elapsed}s elapsed, {remaining}s left    ", end="\r", flush=True)
            page.wait_for_timeout(1500)

        # One last check right at the deadline, in case the tile only just
        # finished rendering. Prefer a fully-loaded image; fall back to
        # whatever's there (better than raising) only if nothing else qualified.
        final_candidate = _find_new_result_tile(page, baseline_tile_ids, REF_EXCLUDE,
                                                prefer_text=title)
        if final_candidate is None:
            _nudge_lazy_imgs(page)
            if _tag_new_result_img(page, baseline_img_srcs):
                final_candidate = page.locator(f"[{_RESULT_TAG}='1']").last
        if final_candidate is not None:
            try:
                if final_candidate.is_visible():
                    if _img_ready(final_candidate) or result_loc is None:
                        result_loc = final_candidate
            except Exception:
                if result_loc is None:
                    result_loc = final_candidate

        if result_loc is None:
            # ── Network-level rescue ────────────────────────────────
            # The generation may have succeeded while Flow renders the result
            # in markup we can't locate (background-image card, canvas, …).
            # Any DISPLAYED image must have been fetched over the network, so
            # try the newest post-Send image responses and save one directly.
            print("\n🛟  DOM detection came up empty — trying network-level rescue …")
            _dump_img_state(page)
            new_urls = [
                (u, info) for u, info in seen_img_urls.items()
                if u not in baseline_net_urls and info["ct"].startswith("image/")
            ]
            print(f"    {len(new_urls)} new image URL(s) captured since Send")
            rescued_p = None
            for url, info in sorted(new_urls, key=lambda kv: -kv[1]["t"]):
                try:
                    r = context.request.get(url, timeout=30_000)
                    if not r.ok:
                        continue
                    body = r.body()
                    ct = ((r.headers or {}).get("content-type", "") or info["ct"]).split(";")[0].strip()
                    if not ct.startswith("image/") or len(body) < 20_000:
                        continue   # skip UI icons / tiny sprites
                    ext = {"image/jpeg": ".jpg", "image/png": ".png",
                           "image/webp": ".webp"}.get(ct, ".png")
                    ts2       = datetime.now().strftime("%Y%m%d_%H%M%S")
                    rescued_p = OUT_DIR / f"thumbnail_{ts2}{ext}"
                    rescued_p.write_bytes(body)
                    print(f"  ✓  Rescued result via network capture → {rescued_p.name}")
                    print("     (saved directly — UI '2K Upscaled' step skipped)")
                    break
                except Exception:
                    continue
            _shot(page, "09_generation_timeout")
            if rescued_p is not None:
                rel = rescued_p.relative_to(SCRIPT_DIR)
                print(f"\n✅  Thumbnail saved  →  {rel}")
                _intentional_close = True
                context.close()
                return rescued_p
            raise RuntimeError(
                f"Generation timed out after {GEN_TIMEOUT}s — no result image found. "
                "Check .debug/ screenshots."
            )

        print(f"\n  ✓  Full {GEN_TIMEOUT}s generation window elapsed — "
              f"result image locked in (waited {int(time.time() - t0)}s total)")
        _shot(page, "09_result")

        # ── 10. Click the generated image ──────────────────────────────
        # This runs only AFTER the full generation window, so the viewer
        # should open in its final, fully-processed state.
        print("🖼️   Opening image …")
        try:
            result_loc.scroll_into_view_if_needed(timeout=5_000)
            result_loc.click(timeout=10_000)
        except Exception:
            try:
                result_loc.locator("xpath=..").click(timeout=6_000)
            except Exception:
                try:
                    # Last resort: JS-click the whole tile container
                    result_loc.evaluate(
                        "el => { const t = el.closest('flow-grid-tile-container') "
                        "|| el.parentElement; "
                        "if (t) t.click(); }")
                except Exception:
                    pass   # continue anyway — download icon might already be visible
        page.wait_for_timeout(3500)
        _shot(page, "10_image_opened")

        # ── 11. Click download icon ────────────────────────────────────
        print("⬇️   Clicking download …")
        DL_SELS = ["button:has(i.google-symbols:has-text('download'))",  # exact prod markup
                   "button[aria-haspopup='menu']:has(i:has-text('download'))",
                   "button:has(i:has-text('download'))",
                   "button[aria-label*='download' i]",   # prod: aria-label="Download media"
                   "button:has(mat-icon:has-text('download'))",
                   "button:has(mat-icon:has-text('file_download'))",
                   "button[title*='download' i]",
                   "[data-icon='download']",
                   "[data-icon='file_download']",
                   "a[download]"]
        ok = _click(page, DL_SELS, "download icon", timeout=12_000)
        for attempt in (2, 3):
            if ok:
                break
            # The viewer may not have opened from the tile click, or its
            # toolbar is still rendering — re-open the image (alternating
            # click targets) and search for the button again.
            print(f"  ↻  Download button not visible — re-opening the image "
                  f"(attempt {attempt}/3) …")
            _shot(page, f"11_retry{attempt}_no_download")
            try:
                if attempt == 2:
                    result_loc.locator("xpath=..").click(timeout=6_000)
                else:
                    result_loc.evaluate(
                        "el => { const t = el.closest('flow-grid-tile-container') "
                        "|| el.parentElement; "
                        "if (t) t.click(); }")
            except Exception:
                pass
            page.wait_for_timeout(3500)
            ok = _click(page, DL_SELS, "download icon", timeout=12_000)
        if not ok:
            _shot(page, "11_no_download_icon")
            raise RuntimeError("Download button not found — check .debug/ screenshots")
        page.wait_for_timeout(1000)
        _shot(page, "11_download_menu")

        # ── 12. Select "2K Upscaled" and capture download ──────────────
        print("📐  Selecting 2K Upscaled …")
        t_2k       = time.time()      # timestamp — used by every rescue path
        dl_before  = _dl_snapshot()   # files already in BROWSER_DL (should be none)
        dl         = None
        dl_name    = None
        dl_url     = None

        def _grab(fn):
            """Run `fn` inside expect_download; never raise on a dead page."""
            nonlocal dl
            try:
                with page.expect_download(timeout=60_000) as ctx:
                    fn()
                dl = ctx.value
            except Exception as e:
                print(f"  ·  expect_download: {type(e).__name__} "
                      f"(harmless — file is harvested from disk)")

        _grab(lambda: _click(page,
                ["button[role='menuitem']:has-text('2K')",   # exact prod markup
                 "[role='menuitem']:has-text('2K')",
                 "button:has-text('2K Upscaled')",
                 "li:has-text('2K Upscaled')",
                 "li:has-text('2K')",
                 "span:has-text('2K Upscaled')",
                 "[role='option']:has-text('2K')",
                 "div:has-text('2K Upscaled')"],
                "2K Upscaled", timeout=15_000))

        # Some Flow builds open a confirm sub-menu after the quality choice
        # instead of downloading right away — look for a final Download item.
        if dl is None and _dl_snapshot() == dl_before:
            print("  ↻  No download yet — checking for a confirm sub-menu …")
            _shot(page, "12_after_2k_no_dl")
            _grab(lambda: _click(page,
                    ["[role='menuitem']:has-text('Download')",
                     "button:has-text('Download')",
                     "[role='menuitem']:has-text('Save')",
                     "button:has-text('Export')"],
                    "download confirm", timeout=8_000))

        if dl is not None:
            # Read these NOW — both come from the local initializer, so they
            # stay valid after Flow closes the page.
            try:
                dl_name = dl.suggested_filename
            except Exception:
                pass
            try:
                dl_url = dl.url
            except Exception:
                pass
            print(f"  ✓  Download started: {dl_name or '(unnamed)'}")

        ext   = Path(dl_name).suffix if dl_name else ".png"
        ext   = ext or ".png"
        ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_p = TMP_DL / f"tmp_{ts}{ext}"
        saved = False

        # ── save-path 1: Playwright save_as — attempt FIRST, immediately ───
        # Whichever download path actually wins internally (our CDP arm vs.
        # Playwright's own per-context interception is NOT deterministic —
        # Playwright silently overrides it about half the time), save_as()
        # can retrieve the bytes as long as the page is still alive. Flow
        # closes its page shortly after the download starts, so speed matters
        # more than which mechanism "should" be armed — try this before any
        # polling/sleeping eats the window.
        if dl is not None:
            print("⏳  Waiting for download to complete …")
            try:
                dl.save_as(str(tmp_p))
                # NOTE: save_as() can succeed without raising even when the
                # CDP arm (not Playwright) actually won the download race —
                # in that case Playwright has no bytes to give it and it
                # writes an empty/stub file. Every other save path below
                # enforces a size floor before trusting the result; this
                # one must too, or a 0-byte file ships as "successful".
                if tmp_p.exists() and tmp_p.stat().st_size > 20_000:
                    saved = True
                    print(f"  ✓  Download complete ({tmp_p.stat().st_size // 1024} KB)")
                else:
                    size = tmp_p.stat().st_size if tmp_p.exists() else 0
                    print(f"  ⚠️  save_as produced {size} bytes (too small) — trying harvest dir …")
            except Exception as e:
                print(f"  ⚠️  save_as failed ({type(e).__name__}) — trying harvest dir …")

        # ── save-path 2: harvest from Chromium's own download dir ──────────
        # Fallback — covers the runs where our CDP arm DID win the race, so
        # the file is sitting in BROWSER_DL rather than Playwright's internal
        # temp path (which save_as needs a live page to read).
        if not saved and dl_armed:
            print("⏳  Waiting for the file to land in the download dir …")
            got = _harvest_download(dl_before, t_2k, timeout=90.0)
            if got is not None:
                if not dl_name:
                    ext   = got.suffix or ext
                    tmp_p = TMP_DL / f"tmp_{ts}{ext}"
                shutil.copy2(str(got), str(tmp_p))
                saved   = True
                dl_name = dl_name or got.name
                print(f"  ✓  Harvested: {got.name}  ({got.stat().st_size // 1024} KB)")
            else:
                print("  ⚠️  Nothing landed in the download dir — trying rescues …")

        # ── settle: give a slow transfer a little more room ────────────────
        if not saved:
            print("⏳  20s settle — keeping browser open …")
            time.sleep(20)
            got = _harvest_download(dl_before, t_2k, timeout=40.0)
            if got is not None:
                if not dl_name:
                    ext   = got.suffix or ext
                    tmp_p = TMP_DL / f"tmp_{ts}{ext}"
                shutil.copy2(str(got), str(tmp_p))
                saved = True
                print(f"  ✓  Harvested after settle: {got.name}")

        # ── rescue-1: Playwright's own temp copy (page must still be alive) ─
        if not saved and dl is not None:
            try:
                temp_dl = dl.path()
                if temp_dl and Path(temp_dl).exists() and Path(temp_dl).stat().st_size > 20_000:
                    shutil.copy2(temp_dl, str(tmp_p))
                    saved = True
                    print("  ✓  Saved via Playwright temp path")
            except Exception:
                pass

        # ── rescue-2: re-fetch the download URL with the browser's cookies ──
        # dl.url survives the page close; the keeper tab keeps the context
        # (and its cookie jar) alive, so the signed URL can be fetched again.
        if not saved and dl_url and not dl_url.startswith("blob:"):
            try:
                r = context.request.get(dl_url, timeout=60_000)
                if r.ok:
                    body = r.body()
                    if len(body) > 20_000:
                        tmp_p.write_bytes(body)
                        saved = True
                        print(f"  ✓  Re-fetched download URL ({len(body) // 1024} KB)")
            except Exception as e:
                print(f"  ·  URL re-fetch failed ({type(e).__name__})")

        # ── rescue-3: last seen result image from the network trail ────────
        if not saved:
            candidates = {u: i for u, i in seen_img_urls.items()
                          if i["t"] >= t_2k - 120}
            for url, info in sorted(candidates.items(), key=lambda kv: -kv[1]["t"]):
                try:
                    r = context.request.get(url, timeout=30_000)
                    if not r.ok:
                        continue
                    body = r.body()
                    ct = ((r.headers or {}).get("content-type", "") or info["ct"]).split(";")[0].strip()
                    if not ct.startswith("image/") or len(body) < 20_000:
                        continue   # skip UI icons / tiny sprites
                    ext   = {"image/jpeg": ".jpg", "image/png": ".png",
                             "image/webp": ".webp"}.get(ct, ".jpg")
                    tmp_p = TMP_DL / f"tmp_{ts}{ext}"
                    tmp_p.write_bytes(body)
                    saved = True
                    print("  ✓  2K file rescued via network capture")
                    break
                except Exception:
                    continue

        if not saved:
            _shot(page, "12_save_failed")
            try:
                leftovers = [f.name for f in BROWSER_DL.glob("*")]
            except Exception:
                leftovers = []
            raise RuntimeError("Download captured but could not be saved — "
                               f"download dir held {leftovers or 'nothing'}; "
                               "check .debug/ screenshots")
        print(f"  ✓  Downloaded      : {dl_name or tmp_p.name}")

        # ── final guard: refuse to ship an empty/garbage file ───────────
        # This is the last line of defense — every save/rescue path above
        # SHOULD already enforce a size floor, but if any path is ever
        # added or edited without one, this catches it before a 0-byte
        # (or truncated-header) file gets moved into output_thumbnail/.
        final_size = tmp_p.stat().st_size if tmp_p.exists() else 0
        if final_size < 20_000:
            _shot(page, "12_empty_file")
            raise RuntimeError(
                f"Save path reported success but the file is only "
                f"{final_size} bytes — refusing to ship a corrupted "
                f"thumbnail. Check .debug/ screenshots.")
        with open(tmp_p, "rb") as _f:
            _header = _f.read(12)
        _valid_headers = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")
        if not any(_header.startswith(h) for h in _valid_headers):
            _shot(page, "12_bad_header")
            raise RuntimeError(
                f"Save path reported success but the file's header "
                f"({_header!r}) doesn't match a known image format — "
                f"refusing to ship a corrupted thumbnail.")

        # ── 13. Move to final output directory ─────────────────────────
        out_p = OUT_DIR / f"thumbnail_{ts}{ext}"
        shutil.move(str(tmp_p), str(out_p))

        rel = out_p.relative_to(SCRIPT_DIR)
        print(f"\n✅  Thumbnail saved  →  {rel}")

        # Everything succeeded — only NOW is it safe to close the browser.
        # (Persistent profile saves the session automatically.)
        # Chrome often exits on its own once Flow's tab closes, so the context
        # may already be dead — that's cosmetic, never a failure of the run.
        _intentional_close = True
        try:
            context.close()
        except Exception as e:
            print(f"  ·  browser already closed ({type(e).__name__}) — ignored")

        return out_p


# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
def main():
    print("═" * 62)
    print("  Thumbnail Generator  —  NVIDIA NIM  +  Google Flow")
    print("═" * 62)

    # ── load config ────────────────────────────────────────────────────
    cfg   = load_config()
    title = cfg["video_idea"]["title"]
    print(f"\n📽️   Title : {title}")

    # ── generate image prompt via NIM ──────────────────────────────────
    prompt = generate_prompt(cfg, title)

    sep = "─" * 60
    print(f"\n{sep}")
    preview = prompt[:500] + ("…" if len(prompt) > 500 else "")
    print(preview)
    print(f"{sep}\n")

    # ── automate Google Flow ───────────────────────────────────────────
    run_flow(cfg, prompt)
    print("\n🎉  All done!")


if __name__ == "__main__":
    main()