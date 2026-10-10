'use strict';

/**
 * YouTube Auto-Uploader  —  SEO Sniper Edition  v2.1
 *
 * Pipeline:
 *   Agent 1  →  Script Parser        (NVIDIA NIM — Llama 3.3 70B)
 *   Agent 2  →  Trend Intelligence   (Reddit + Google Trends + DuckDuckGo — parallel, free)
 *   Agent 3  →  SEO Generator        (NVIDIA NIM — Mistral Small)
 *   Agent 4  →  Viral Predictor      (NVIDIA NIM — Mistral Small)
 *   Upload   →  youtube-videos-uploader
 *
 * Install / update:
 *   npm install duck-duck-scrape
 *   npm install youtube-videos-uploader@latest   ← also fixes the visibility error
 *
 * v2.1 improvements over v2.0:
 *  1. Agent 1 system prompt now includes the "one string per line" rule — prevents
 *     multi-line fields from breaking JSON.parse() before anything downstream runs.
 *  2. estimated_duration_mins computed deterministically from FULL script word count
 *     in JS code, not guessed by the model from a partial slice — long scripts were
 *     getting wrong durations before.
 *  3. Agent 1 now samples HEAD + TAIL (4 000 + 2 000 chars) instead of head-only,
 *     so the model sees how the video actually resolves, not just the hook.
 *  4. Three new Agent 1 fields (hook_line, payoff_or_twist, best_quote) are wired
 *     into Agent 3's prompt so titles stay grounded in what the video delivers.
 *     A curiosity-gap title that doesn't pay off tanks average view duration.
 *  5. Tag count fixed: old schema asked for "40 tags" but 5+10+10+5+5 = 35.
 *     long_tail_tags bumped 10→15 (highest ROI, lowest competition tag type).
 *  6. Tag output capped at YouTube's hard 500-char limit — enforced both in the
 *     prompt AND verified/trimmed in code; tags can no longer silently get cut
 *     after upload with zero warning.
 *  7. Numeric examples in all three schemas are now flagged as format placeholders
 *     to prevent LLM anchoring bias on example values.
 *  8. Agent 4's viral_score re-derived in code from clamped score_breakdown
 *     components — the prompt defines it as their sum, so code now enforces that
 *     instead of trusting the model's arithmetic.
 *  9. Shared nimJsonRetry() wrapper retries once with a stricter reminder on an
 *     empty/invalid/throwing completion — one bad call no longer silently pushes
 *     empty titles/tags/scores downstream.
 * 10. Agent 3 gains thumbnail_text_suggestions and a pinned_comment suggestion —
 *     title + thumbnail is really one CTR decision; pinned comment seeds replies.
 */

const { upload }    = require('youtube-videos-uploader');
const fs            = require('fs');
const path          = require('path');
const mammoth       = require('mammoth');
const axios         = require('axios');
const cliProgress   = require('cli-progress');
const googleTrends  = require('google-trends-api');

// duck-duck-scrape is optional — DDG news is skipped if not installed
let ddgSearchNews = null;
try { ({ searchNews: ddgSearchNews } = require('duck-duck-scrape')); }
catch { /* npm install duck-duck-scrape  to enable */ }

// Reddit OAuth token — fetched once per run, cached for the process lifetime (tokens last 1 h)
let _redditToken = null;


// ─────────────────────────────────────────────────────────────────────────────
// CONFIG
// ─────────────────────────────────────────────────────────────────────────────
const CONFIG_PATH = path.join(__dirname, 'upload.config.json');
if (!fs.existsSync(CONFIG_PATH)) {
  console.error(
    '\n❌  upload.config.json not found.\n' +
    '    Copy upload.config.example.json → upload.config.json and fill it in.\n'
  );
  process.exit(1);
}
const cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));

const REQUIRED = [
  ['youtube.email',      cfg.youtube?.email],
  ['youtube.pass',       cfg.youtube?.pass],
  ['google.api_key', cfg.google?.api_key],
  ['paths.script',       cfg.paths?.script],
  ['paths.video_folder', cfg.paths?.video_folder],
];
for (const [k, v] of REQUIRED) {
  if (!v || String(v).startsWith('YOUR_')) {
    console.error(`❌  Missing or placeholder value for "${k}" in upload.config.json`);
    process.exit(1);
  }
}

// NIM model routing — matches seo_sniper.py provider config
const MODEL_PARSE = cfg.google?.model_parse ?? 'gemini-3.1-flash-lite';
const MODEL_SEO   = cfg.google?.model_seo   ?? 'gemini-3.1-flash-lite';
const MODEL_VIRAL = cfg.google?.model_viral ?? 'gemini-3.1-flash-lite';


// ─────────────────────────────────────────────────────────────────────────────
// SMALL HELPERS
// ─────────────────────────────────────────────────────────────────────────────
const sleep = ms => new Promise(r => setTimeout(r, ms));

// Progress bar
const bar = new cliProgress.SingleBar(
  {
    format:            '  [{bar}] {percentage}%  ·  {stage}',
    barCompleteChar:   '█',
    barIncompleteChar: '░',
    hideCursor:        true,
  },
  cliProgress.Presets.shades_grey
);
const BS = { active: false, value: 0, stage: '' };   // bar state

function barStart(value = 0, stage = 'Starting…') {
  Object.assign(BS, { active: true, value, stage });
  bar.start(100, value, { stage });
}
function barUpdate(value, stage) {
  Object.assign(BS, { value, stage });
  if (BS.active) bar.update(value, { stage });
}
function barStop() {
  BS.active = false;
  bar.stop();
}

/** Log a line without corrupting the progress bar. */
function log(msg) {
  if (BS.active) bar.stop();
  console.log(msg);
  if (BS.active) bar.start(100, BS.value, { stage: BS.stage });
}


// ─────────────────────────────────────────────────────────────────────────────
// NVIDIA NIM CLIENT  (NIM-only, with retry + rate-limit handling)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Raw NIM call — SSE streaming POST.
 *
 * timeout is the time to receive the FIRST byte from the API.  On the NIM
 * free-tier, large models (Llama 3.3 70B) can queue for 30-60 s before the
 * first token arrives — 90 s gives comfortable headroom.  Once the stream
 * starts, the rolling 60 s stall-timer in the data handler takes over so a
 * slow-but-alive response never fires the connection timeout.
 *
 * Error taxonomy used in the catch block:
 *   status=429  → rate-limit  → back off 15-60 s
 *   status≥500  → server err  → back off 3-6 s
 *   ECONNABORTED/ETIMEDOUT/aborted/timeout → transient network → back off 8-20 s
 *   anything else → back off 4 s, then throw on last attempt
 */
async function nimCall(model, messages, maxTokens = 1400,
                       temperature = 0.3, retries = 3) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const res = await axios.post(
        'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
        { model, messages, max_tokens: maxTokens, temperature, stream: true,
        response_format: { type: 'json_object' } },
        {
          headers: {
            Authorization: `Bearer ${cfg.google.api_key}`,
            'Content-Type': 'application/json',
          },
          responseType:   'stream',
          timeout:        90_000,    // 90 s to receive first byte — 70 B models under free-tier load need this
          validateStatus: null,      // inspect status manually so the error body is still readable
        }
      );

      // HTTP error — read the (small) JSON error body from the stream, then surface it
      if (res.status >= 400) {
        const errBody = await new Promise(resolve => {
          let body = '';
          res.data.on('data',  c  => { body += c.toString(); });
          res.data.on('end',   () => resolve(body));
          res.data.on('error', () => resolve(''));
        });
        let detail;
        try { const p = JSON.parse(errBody); detail = p.error?.message ?? p.detail ?? p.message; } catch { /* */ }
        const isQuotaErr = errBody.includes('RESOURCE_EXHAUSTED') || errBody.includes('exhausted');
        const httpErr    = new Error(detail ?? `HTTP ${res.status}`);
        httpErr.response = { status: res.status };
        httpErr.isQuotaExhausted = isQuotaErr;
        throw httpErr;
      }

      // 200 OK — accumulate SSE chunks.  Rolling stall-timer resets on every
      // chunk so a slow-but-alive stream never fires it.
      return await new Promise((resolve, reject) => {
        let text = '';
        let buf  = '';
        let stallTimer;

        const resetStall = () => {
          clearTimeout(stallTimer);
          stallTimer = setTimeout(
            () => reject(new Error('SSE stream stalled — no data for 60 s')),
            60_000
          );
        };
        resetStall();

        res.data.on('data', chunk => {
          resetStall();
          buf += chunk.toString();
          const lines = buf.split('\n');
          buf = lines.pop();         // keep the last (possibly incomplete) line in the buffer

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed || trimmed === 'data: [DONE]') continue;
            if (!trimmed.startsWith('data: '))          continue;
            try {
              const parsed = JSON.parse(trimmed.slice(6));
              const delta  = parsed.choices?.[0]?.delta?.content;
              if (delta) text += delta;
            } catch { /* malformed SSE chunk — skip silently */ }
          }
        });

        res.data.on('end', () => {
          clearTimeout(stallTimer);
          resolve(text.trim());
        });

        res.data.on('error', err => {
          clearTimeout(stallTimer);
          reject(err);
        });
      });

    } catch (err) {
      const status = err.response?.status ?? 0;
      const detail = err.message;
      const isLast = attempt >= retries;

      // Classify the error so we wait an appropriate length of time.
      // Network-level errors (timeout / abort / reset) are transient and need
      // a longer cool-down — the 70 B model may still be spinning up on the
      // free tier when the first byte timeout fires.
      const isNetworkErr =
        ['ECONNABORTED', 'ETIMEDOUT', 'ECONNRESET', 'EPIPE', 'ENOTFOUND'].includes(err.code) ||
        detail.includes('timeout') || detail.includes('aborted') || detail.includes('reset');

      log(`⚠️   Google attempt ${attempt}/${retries}: ...`);

      if (err.isQuotaExhausted) {
        throw new Error('❌  Daily quota exhausted — wait for reset or enable billing: https://aistudio.google.com/apikey');
      }
      if (isLast) {
        throw new Error(`Google Gemini error (all ${retries} attempts failed): ${detail}`);
      }

      let waitMs;
      if (status === 429) waitMs = Math.min(120_000, 30_000 * attempt);  // rate-limit
      else if (status >= 500)   waitMs = 3_000 * attempt;                      // server error
      else if (isNetworkErr)    waitMs = Math.min(30_000, 8_000 * attempt);   // timeout / abort
      else                      waitMs = 4_000 * attempt;                      // unknown

      log(`⏳  Waiting ${(waitMs / 1000).toFixed(0)} s before retry…`);
      await sleep(waitMs);
    }
  }
  return '';
}

/** Wrapper that calls NIM and robustly parses the JSON response. */
async function nimJson(model, systemPrompt, userPrompt, maxTokens = 1400) {
  const messages = [
    { role: 'system', content: systemPrompt },
    { role: 'user',   content: userPrompt  },
  ];
  // temperature=0.15 instead of 0.3: lower temperature means the model is
  // more deterministic, which (a) produces cleaner JSON with fewer repairs
  // needed, and (b) slightly reduces time-to-first-token under load because
  // the model doesn't have to sample as broadly across the token distribution.
  const raw = await nimCall(model, messages, maxTokens, 0.15);
  return raw ? repairJson(raw) : {};
}


// ─────────────────────────────────────────────────────────────────────────────
// SHARED HELPERS  (used by Agents 1, 3, 4)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Wraps nimJson with one retry on a missing/invalid/throwing completion.
 * `isValid` is a cheap shape-check you provide per agent.
 */
async function nimJsonRetry(model, sys, prompt, maxTokens, isValid, label, retries = 1) {
  async function attempt(p) {
    try {
      return await nimJson(model, sys, p, maxTokens);
    } catch (err) {
      log(`❌  ${label} call failed: ${err.message}`);
      return null;
    }
  }

  let result = await attempt(prompt);
  let n = 0;
  while ((!result || !isValid(result)) && n < retries) {
    n++;
    log(`⚠️   ${label} returned invalid/empty JSON — retrying (${n}/${retries})…`);
    result = await attempt(
      `${prompt}\n\nREMINDER: your previous response was invalid, incomplete, or not ` +
      `parseable JSON. Output ONLY valid JSON matching the schema above — no markdown ` +
      `fences, no commentary, every string value on one line.`
    );
  }
  return result;
}

/** Guarantees an array even if the model returns a string/null for a list field. */
function coerceArray(val) {
  if (Array.isArray(val)) return val.map(v => String(v ?? '').trim()).filter(Boolean);
  if (typeof val === 'string' && val.trim()) return val.split(',').map(v => v.trim()).filter(Boolean);
  return [];
}

/**
 * Deterministic duration estimate from the FULL script — not the
 * truncated slice sent to the model, which can't see enough of a long
 * script to know its real runtime.
 */
function estimateDurationMinutes(fullScriptText, wordsPerMinute = 145) {
  const wordCount = (fullScriptText.match(/\S+/g) || []).length;
  return Math.max(1, Math.round(wordCount / wordsPerMinute));
}

/**
 * Samples head + tail of a long script instead of head-only, so the
 * model can see how the video resolves (twist/payoff/CTA), not just the
 * opening. Keeps total chars roughly comparable to a flat head-slice.
 */
function sampleScriptForAnalysis(scriptText, headChars = 4000, tailChars = 2000) {
  if (scriptText.length <= headChars + tailChars) return scriptText;
  const head = scriptText.slice(0, headChars);
  const tail = scriptText.slice(-tailChars);
  return `${head}\n\n[...middle section omitted for length...]\n\n${tail}`;
}

/**
 * YouTube hard-caps the combined tag string at 500 characters; anything
 * over that gets silently dropped/truncated with no error on upload.
 * This trims `all_tags` from the low-priority end (model is asked to
 * priority-order them, highest-value first) until the joined string fits.
 * Conservative: counts tags joined with ", " (separator overhead included).
 */
function enforceTagBudget(dataObj, maxChars = 480) {
  if (!dataObj?.tags?.all_tags) return dataObj;

  let all = coerceArray(dataObj.tags.all_tags);

  // Deduplicate case-insensitively
  const seen = new Set();
  all = all.filter(t => {
    const key = t.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  // Trim from the low-priority tail until it fits
  const fits = (arr) => arr.join(', ').length <= maxChars;
  while (all.length > 1 && !fits(all)) all.pop();

  dataObj.tags.all_tags = all;
  dataObj.tags._tag_string_length = all.join(', ').length; // sanity field — strip before upload if desired
  return dataObj;
}


// ─────────────────────────────────────────────────────────────────────────────
// 5-STRATEGY JSON REPAIR  (ported from seo_sniper.py _repair_json)
// ─────────────────────────────────────────────────────────────────────────────
function repairJson(raw) {
  const s = raw.trim();

  // ── Strategy 1: direct parse
  try { return JSON.parse(s); } catch { /* continue */ }

  // ── Strategy 2: strip markdown fences  ``` json ... ```
  const clean = s.replace(/```(?:json|JSON)?\s*|\s*```/g, '').trim();
  try { return JSON.parse(clean); } catch { /* continue */ }

  // ── Strategy 3: extract first complete { } block
  const blockMatch = clean.match(/\{[\s\S]*\}/);
  if (blockMatch) { try { return JSON.parse(blockMatch[0]); } catch { /* continue */ } }

  // ── Strategy 4: syntax fixes + char-by-char control-char repair
  let fixed = clean
    .replace(/,\s*([}\]])/g, '$1')   // trailing commas
    .replace(/\bTrue\b/g,  'true')   // Python/AI literals
    .replace(/\bFalse\b/g, 'false')
    .replace(/\bNone\b/g,  'null');

  // Walk char-by-char: escape literal \n \r \t only inside JSON string values
  let repaired = '';
  let inStr = false, esc = false;
  for (const ch of fixed) {
    if (esc)             { repaired += ch; esc = false;         continue; }
    if (ch === '\\' && inStr) { repaired += ch; esc = true;     continue; }
    if (ch === '"')      { repaired += ch; inStr = !inStr;      continue; }
    if (inStr) {
      if      (ch === '\n') { repaired += '\\n';  continue; }
      else if (ch === '\r') { repaired += '\\r';  continue; }
      else if (ch === '\t') { repaired += '\\t';  continue; }
      else if (ch < ' ')    { /* drop other control chars */     continue; }
    }
    repaired += ch;
  }
  try { return JSON.parse(repaired); } catch { /* continue */ }

  // ── Strategy 5: truncation recovery — count unclosed braces and close them
  if (repaired.trimStart().startsWith('{')) {
    let depth = 0, inS = false, e = false;
    for (const ch of repaired) {
      if (e)          { e = false;          continue; }
      if (ch === '\\') { e = true;           continue; }
      if (ch === '"')  { inS = !inS;         continue; }
      if (!inS) {
        if      (ch === '{' || ch === '[') depth++;
        else if (ch === '}' || ch === ']') depth--;
      }
    }
    if (depth > 0) {
      try { return JSON.parse(repaired + '}'.repeat(depth)); } catch { /* fall through */ }
    }
  }

  log('⚠️   JSON repair exhausted all 5 strategies — returning {}');
  return {};
}


// ─────────────────────────────────────────────────────────────────────────────
// DOCX READER
// ─────────────────────────────────────────────────────────────────────────────
async function readDocxScript() {
  // Resolve relative to the config file's directory so "./pod1.docx" works
  const p = path.resolve(path.dirname(CONFIG_PATH), cfg.paths.script);
  if (!fs.existsSync(p)) throw new Error(`Script not found: ${p}`);
  const { value } = await mammoth.extractRawText({ path: p });
  if (!value.trim()) throw new Error('Script file is empty.');
  return value.trim();
}


// ─────────────────────────────────────────────────────────────────────────────
// SUBREDDIT MAP  
// ─────────────────────────────────────────────────────────────────────────────
const SUBREDDIT_MAP = {
  'psychology': [
    'relationship_advice', 'AmItheAsshole', 'TrueOffMyChest', 'confession',
    'AITAH', 'offmychest', 'raisedbynarcissists', 'NarcissisticAbuse',
    'JUSTNOMIL', 'survivinginfidelity', 'MaliciousCompliance', 'pettyrevenge',
    'socialpsychology', 'psychology', 'BestofRedditorUpdates',
  ],
  'default': ['videos', 'youtube', 'OutOfTheLoop', 'TIL'],
};

function getSubreddits(niche = '') {
  const lc = niche.toLowerCase();
  const entry = Object.entries(SUBREDDIT_MAP).find(([k]) => lc.includes(k));
  return entry ? entry[1] : SUBREDDIT_MAP.default;
}


// ─────────────────────────────────────────────────────────────────────────────
// REDDIT OAUTH  (application-only client_credentials flow — no user login)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Exchange client_id + client_secret for a Bearer token.
 * Returns null (and logs a warning) when credentials are absent or the
 * request fails — callers fall back to the unauthenticated public API.
 */
async function getRedditToken() {
  if (_redditToken) return _redditToken;   // already fetched this run

  const { client_id, client_secret, user_agent } = cfg.reddit ?? {};
  if (!client_id || !client_secret) {
    log('ℹ️   Reddit credentials not in config — using public API (may 403)');
    return null;
  }

  try {
    const res = await axios.post(
      'https://www.reddit.com/api/v1/access_token',
      'grant_type=client_credentials',
      {
        auth:    { username: client_id, password: client_secret },
        headers: {
          'User-Agent':   user_agent ?? 'upload-youtube-seo-sniper/2.1',
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        timeout: 10_000,
      }
    );
    _redditToken = res.data.access_token;
    log('✅  Reddit OAuth token acquired');
    return _redditToken;
  } catch (e) {
    log(`⚠️   Reddit OAuth failed: ${e.message} — falling back to public API`);
    return null;
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// REDDIT SCRAPER  (free JSON API — no key needed)
// ─────────────────────────────────────────────────────────────────────────────
async function scrapeReddit(queries, niche) {
  log('🟠  Reddit: searching hot posts…');

  const token     = await getRedditToken();
  const userAgent = cfg.reddit?.user_agent ?? 'Mozilla/5.0 (compatible; upload-youtube-seo-sniper/2.1; +research)';

  const headers = {
    'User-Agent': userAgent,
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };

  // Authenticated requests go to oauth.reddit.com (proper API, no bot-detection).
  // Unauthenticated fall back to www.reddit.com with raw_json=1 to force JSON.
  const base   = token ? 'https://oauth.reddit.com' : 'https://www.reddit.com';
  const suffix = token ? '' : '&raw_json=1';

  const subs      = getSubreddits(niche);
  const endpoints = [
    // Subreddit hot feeds first — more reliable than search for unauthenticated requests.
    // Pulling all of them (not just the first 3) since this list is now a curated,
    // niche-specific set rather than a generic catch-all — the 429 check below
    // still stops early if Reddit starts rate-limiting mid-run.
    ...subs.map(sub =>
      `${base}/r/${sub}/hot.json?limit=10${suffix}`
    ),
    // Keyword search as secondary
    ...queries.slice(0, 2).map(q =>
      `${base}/search.json?q=${encodeURIComponent(q)}&sort=hot&limit=8&t=week${suffix}`
    ),
  ];

  const results = [];
  for (const url of endpoints) {
    try {
      const r = await axios.get(url, { headers, timeout: 12_000 });
      if (r.status === 429) { log('⚠️   Reddit rate-limited — stopping early'); break; }
      // Guard: Reddit sometimes serves HTML (bot-detection wall) as a 200; data won't have .data
      if (typeof r.data !== 'object') { log('⚠️   Reddit: non-JSON response (bot-detection?) — skipping'); continue; }
      for (const p of (r.data?.data?.children ?? [])) {
        const d = p.data ?? {};
        if (d.title) results.push({
          title:       d.title,
          score:       d.score       ?? 0,
          subreddit:   d.subreddit   ?? '',
          numComments: d.num_comments ?? 0,
        });
      }
    } catch (e) { log(`⚠️   Reddit endpoint failed: ${e.message}`); }
    await sleep(800);   // polite delay between requests
  }

  // Deduplicate by title, sort by Reddit score
  const seen = new Set();
  return results
    .filter(r => { if (seen.has(r.title)) return false; seen.add(r.title); return true; })
    .sort((a, b) => b.score - a.score)
    .slice(0, 18);
}


// ─────────────────────────────────────────────────────────────────────────────
// GOOGLE TRENDS  (google-trends-api — no key needed)
// ─────────────────────────────────────────────────────────────────────────────
async function scrapeGoogleTrends(keywords) {
  log('📈  Google Trends: fetching rising + top queries…');
  await sleep(3000);   // 3 s pause reduces Google 429 bot-detection (from seo_sniper.py)

  const out = { available: false, risingQueries: [], topQueries: [] };

  for (const kw of (keywords ?? []).filter(Boolean).slice(0, 5)) {
    try {
      const raw   = await googleTrends.relatedQueries({ keyword: kw });
      const lists = JSON.parse(raw)?.default?.rankedList ?? [];

      // rankedList[0] = top queries, rankedList[1] = rising queries
      for (const item of (lists[0]?.rankedKeyword ?? []).slice(0, 6)) {
        if (item.query) out.topQueries.push(item.query);
      }
      for (const item of (lists[1]?.rankedKeyword ?? []).slice(0, 6)) {
        if (item.query) out.risingQueries.push(item.query);
      }
    } catch { /* skip per-keyword failures */ }
  }

  out.risingQueries = [...new Set(out.risingQueries)];
  out.topQueries    = [...new Set(out.topQueries)];
  out.available     = (out.risingQueries.length + out.topQueries.length) > 0;
  return out;
}


// ─────────────────────────────────────────────────────────────────────────────
// DUCKDUCKGO NEWS  (duck-duck-scrape — no key needed)
// ─────────────────────────────────────────────────────────────────────────────
async function scrapeDDGNews(queries) {
  if (!ddgSearchNews) {
    log('⚠️   duck-duck-scrape not installed → DDG skipped  (run: npm install duck-duck-scrape)');
    return [];
  }
  log('🦆  DuckDuckGo: fetching news headlines…');

  const results     = [];
  const seenQueries = new Set();

  // Pre-filter: DDG 503s on single-word queries — require at least 2 words
  const multiWord = queries.filter(q => q && q.trim().split(/\s+/).length >= 2);

  // Deduplicate queries case-insensitively (seo_sniper.py: prevents instant 403)
  const unique = multiWord.slice(0, 3).filter(q => {
    if (seenQueries.has(q.toLowerCase())) return false;
    seenQueries.add(q.toLowerCase());
    return true;
  });

  if (!unique.length) {
    log('ℹ️   DDG skipped — no multi-word queries in this set');
    return [];
  }

  for (const q of unique.slice(0, 2)) {   // max 2 queries to stay under DDG rate limit
    let attempt = 0;
    while (attempt < 2) {
      try {
        const news = await ddgSearchNews(q, { safeSearch: -2 });
        for (const item of (news?.results ?? []).slice(0, 8)) {
          results.push({
            title:  item.title        ?? '',
            source: item.syndicate    ?? '',
            date:   item.relativeTime ?? '',
            url:    item.url          ?? '',
          });
        }
        break;   // success — move to next query
      } catch (e) {
        attempt++;
        const isServerErr = e.message.toLowerCase().includes('server error');
        if (isServerErr && attempt < 2) {
          log(`⚠️   DDG server error for "${q}" — retrying in 5 s…`);
          await sleep(5_000);
        } else {
          log(`⚠️   DDG error for "${q}": ${e.message}`);
          break;   // give up on this query — avoid cascading 403s
        }
      }
    }
    await sleep(2500);   // 2.5 s between DDG queries (from seo_sniper.py)
  }

  return results.slice(0, 20);
}


// ─────────────────────────────────────────────────────────────────────────────
// AGENT 1 — SCRIPT PARSER
// ─────────────────────────────────────────────────────────────────────────────
const A1_SYS =
  'You are a professional YouTube content analyst specializing in viral content ' +
  'strategy, audience psychology, and niche targeting. ' +
  'Your job is to extract every structural and emotional dimension of a video script ' +
  'so downstream agents can make precise, data-grounded SEO decisions. ' +
  'Be specific and concrete — vague extractions produce weak titles and tags. ' +
  'This script is for a podcast-style animated channel — two cartoon hosts ' +
  'discussing psychology, often reacting to a trending story or confession. ' +
  'Extract metadata with that two-host, reactive discussion format in mind. ' +
  'Respond with valid JSON only — no markdown, no preamble. ' +
  'Every string value MUST be on ONE LINE — use \\n for paragraph breaks if needed.';

function isValidScriptMeta(r) {
  return Boolean(r?.main_topic && r?.niche);
}

async function agent1_parseScript(scriptText) {
  log('\n🔍  [Agent 1/4] Script Parser — analyzing content…');

  // Compute duration deterministically from FULL script word count (fix #2)
  const durationEstimate = estimateDurationMinutes(scriptText);

  // Sample head + tail so the model sees opening hook AND resolution (fix #3)
  const sample = sampleScriptForAnalysis(scriptText, 4000, 2000);

  const result = await nimJsonRetry(MODEL_PARSE, A1_SYS,
`Analyze this YouTube video script and extract ALL metadata fields listed below.
Be specific — generic extractions produce weak SEO. Study the script carefully.

SCRIPT (head + tail sample — middle omitted for length):
${sample}

RUNTIME NOTE: The video's estimated duration is ~${durationEstimate} min (computed
separately from the full word count). Do NOT output it — it is attached automatically.

Return EXACTLY this JSON object. Fill every field with real, specific values from
the script — do NOT use the placeholder text shown here as your actual answer:

{
  "main_topic": "specific topic name — not just the niche label",
  "content_summary": "2-3 sentence summary covering the core argument, key evidence, and takeaway",
  "niche": "primary niche (e.g. dark psychology, self-improvement, finance)",
  "sub_niche": "specific sub-niche (e.g. covert manipulation tactics, debt payoff strategies)",
  "content_type": "educational|story|tutorial|commentary|review|entertainment",
  "tone": "dark|inspirational|informative|shocking|calm|humorous",
  "target_audience": "specific description — age, interest, problem they have (not just 'general audience')",
  "key_entities": ["real people, brands, organizations, or places explicitly named in the script"],
  "main_topics": ["3 primary topics the video covers — be specific"],
  "subtopics": ["4 supporting or tangential topics that get meaningful airtime"],
  "core_message": "the single most important idea the viewer should leave with",
  "emotional_triggers": ["specific emotions this content activates: curiosity|fear|anger|hope|disgust|surprise|trust|urgency"],
  "unique_angle": "what makes this video's treatment genuinely different from 10 other videos on the same topic",
  "content_pillars": ["2 foundational themes that anchor the whole video"],
  "seed_keywords": ["8 keywords that naturally appear in or perfectly describe the script — mix broad and specific"],
  "related_searches": ["6 phrases viewers would actually search on YouTube after watching — be realistic"],
  "competitor_topics": ["4 example titles of similar videos already on YouTube this would compete with"],
  "difficulty_level": "beginner|intermediate|advanced",
  "hook_line": "the exact opening line or hook from the script — copy it verbatim if possible",
  "payoff_or_twist": "the key revelation, resolution, or twist near the end of the script — be specific",
  "best_quote": "the single most quotable, clip-worthy, or share-worthy line from anywhere in the script",
  "calls_to_action": ["subscribe CTA|comment question|like request|share ask — list what the script explicitly includes"],
  "pacing": "slow-burn|moderate|fast-paced",
  "narrative_structure": "listicle|story-arc|problem-solution|case-study|comparison|monologue"
}`, 2500, isValidScriptMeta, 'Agent 1 (Script Parser)');

  if (!result?.main_topic) {
    log('⚠️   Script parsing returned minimal data — using fallback values');
    return {
      main_topic: 'general', niche: 'general', sub_niche: 'general',
      content_type: 'educational', tone: 'informative',
      target_audience: 'general audience',
      estimated_duration_mins: durationEstimate,
      core_message: '', unique_angle: '', content_summary: '',
      seed_keywords: [], subtopics: [], emotional_triggers: [],
      main_topics: [], key_entities: [],
      hook_line: '', payoff_or_twist: '', best_quote: '',
      calls_to_action: [], pacing: 'moderate', narrative_structure: 'monologue',
    };
  }

  // Defensive cleanup — guard against model returning a string instead of
  // an array for any list field (a known LLM JSON quirk)
  ['key_entities', 'main_topics', 'subtopics', 'emotional_triggers', 'seed_keywords',
   'related_searches', 'competitor_topics', 'content_pillars', 'calls_to_action']
    .forEach(field => { result[field] = coerceArray(result[field]); });

  // Always use the code-computed value — exact math beats an LLM guess
  // made from a partial sample (fix #2)
  result.estimated_duration_mins = durationEstimate;

  log(`📌  Topic    : ${result.main_topic}  ·  Niche: ${result.niche}`);
  log(`🎯  Message  : ${result.core_message || '(not extracted)'}`);
  log(`😮  Emotions : ${result.emotional_triggers.join(', ') || '(none)'}`);
  log(`🪝   Hook     : ${result.hook_line ? result.hook_line.slice(0, 80) + '…' : '(not extracted)'}`);
  log(`⏱️   Duration : ${result.estimated_duration_mins} min (from word count)`);
  return result;
}


// ─────────────────────────────────────────────────────────────────────────────
// AGENT 2 — TREND INTELLIGENCE  (3 sources, fully parallel)
// ─────────────────────────────────────────────────────────────────────────────
async function agent2_collectTrends(scriptData) {
  log('\n🌐  [Agent 2/4] Trend Intelligence — running all sources in parallel…');

  const mainTopic = scriptData.main_topic   ?? '';
  const niche     = 'psychology';   // single-niche channel — don't trust the LLM's guess here
  const seedKws   = scriptData.seed_keywords ?? [];
  const subtopics = scriptData.subtopics     ?? [];

  // Build deduplicated query list (same logic as seo_sniper.py agent_collect_trends)
  const queries = [
    ...new Set([mainTopic, ...seedKws.slice(0, 3), ...subtopics.slice(0, 2)].filter(Boolean))
  ].slice(0, 5);

  // Run all 3 scrapers simultaneously — total time ≈ slowest single source
  const [rReddit, rTrends, rDDG] = await Promise.allSettled([
    scrapeReddit(queries, niche),
    scrapeGoogleTrends(seedKws.length ? seedKws.slice(0, 5) : [mainTopic]),
    scrapeDDGNews(queries),
  ]);

  const reddit  = rReddit.status  === 'fulfilled' ? rReddit.value  : [];
  const gtrends = rTrends.status  === 'fulfilled' ? rTrends.value  :
                  { available: false, risingQueries: [], topQueries: [] };
  const ddgNews = rDDG.status     === 'fulfilled' ? rDDG.value     : [];

  log(`\n📊  Sources: Reddit ${reddit.length} posts  ·  Trends ${gtrends.available ? '✓' : '✗'}  ·  DDG ${ddgNews.length} articles`);

  return {
    queriesUsed:   queries,
    reddit,
    googleTrends:  gtrends,
    ddgNews,
    sourceCounts:  { reddit: reddit.length, ddg: ddgNews.length },
  };
}


// ─────────────────────────────────────────────────────────────────────────────
// AGENT 3 — SEO GENERATOR
// ─────────────────────────────────────────────────────────────────────────────
const A3_SYS =
  'You are a senior YouTube SEO strategist with 10+ years growing channels ' +
  'to millions of subscribers. You have deep expertise in CTR psychology, ' +
  'YouTube search algorithm signals, keyword clustering, tag architecture, ' +
  'and thumbnail-to-title synergy. ' +
  'Your titles are grounded in what the video ACTUALLY delivers — you never ' +
  'write clickbait that overpromises and causes viewers to bounce early. ' +
  'You understand that a curiosity gap only helps retention if it pays off, ' +
  'and that the title + thumbnail together form a single CTR decision. ' +
  'This is for a podcast-style animated channel — two cartoon hosts discussing ' +
  'psychology, often reacting to a trending story or confession. Thumbnail text ' +
  'and titles should account for host reactions/banter, not a single-presenter framing. ' +
  'Respond with valid JSON only — no markdown, no preamble. ' +
  'Every string value MUST be on ONE LINE — use \\n\\n for paragraph breaks inside full_text.';

function isValidSEO(r) {
  return Boolean(r?.titles?.primary && r?.tags?.all_tags?.length);
}

async function agent3_generateSEO(scriptData, trends) {
  log('\n🎯  [Agent 3/4] SEO Generator — crafting titles, tags & description…');

  // Sort Reddit posts by score before slicing — highest-engagement first (fix: sort before slice)
  const redditSorted = [...(trends.reddit ?? [])].sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  const redditTitles = redditSorted.slice(0, 6).map(r => r.title);
  const risingQry    = trends.googleTrends?.risingQueries ?? [];
  const topQry       = trends.googleTrends?.topQueries    ?? [];
  const newsTitles   = (trends.ddgNews ?? []).slice(0, 5).map(n => n.title);

  const raw = await nimJsonRetry(MODEL_SEO, A3_SYS,
`Generate complete YouTube SEO optimization for this video.

== SCRIPT ANALYSIS ==
Topic             : ${scriptData.main_topic}
Niche             : ${scriptData.niche} / ${scriptData.sub_niche}
Content Type      : ${scriptData.content_type}
Narrative         : ${scriptData.narrative_structure ?? 'not specified'}
Tone              : ${scriptData.tone}
Core Message      : ${scriptData.core_message}
Hook Line         : ${scriptData.hook_line || '(not provided)'}
Payoff / Twist    : ${scriptData.payoff_or_twist || '(not provided)'}
Best Quote        : ${scriptData.best_quote || '(not provided)'}
Emotional Triggers: ${JSON.stringify(scriptData.emotional_triggers ?? [])}
Seed Keywords     : ${JSON.stringify(scriptData.seed_keywords ?? [])}
Subtopics         : ${JSON.stringify(scriptData.subtopics ?? [])}
Unique Angle      : ${scriptData.unique_angle}
Audience          : ${scriptData.target_audience}
Difficulty Level  : ${scriptData.difficulty_level ?? 'not specified'}
Est. Duration     : ${scriptData.estimated_duration_mins} min

== LIVE TREND DATA ==
Reddit hot titles (sorted high→low score) : ${JSON.stringify(redditTitles)}
Rising Google queries                     : ${JSON.stringify(risingQry)}
Top Google queries                        : ${JSON.stringify(topQry)}
Recent news headlines                     : ${JSON.stringify(newsTitles)}

== TITLE RULES ==
- Primary title: under 70 chars. Front-load the MAIN keyword. Use proven power
  words (e.g. secret, revealed, nobody, finally, dark truth, real reason).
- The title MUST be true to the Hook Line and Payoff above — if the hook is
  curiosity-gap, the viewer must actually get the answer. A misleading title tanks
  average view duration, which is the #1 long-term ranking signal on YouTube.
- If the hook is emotional (fear, anger, disgust), reflect that tone in the title.
- Curiosity-gap titles only work if paired with a genuinely surprising payoff.
- Analyze the Reddit titles above for what vocabulary, framing, and emotional
  triggers are resonating with the audience RIGHT NOW and borrow that energy.

== DESCRIPTION RULES ==
- 450-500 words total. First 150 chars must contain the primary keyword and
  a compelling reason to watch.
- Include a CHAPTER / TIMESTAMP skeleton (placeholder times like "[00:00] Intro"
  for editors to fill with real cut points — these are NOT real timestamps yet).
- End with 3-5 relevant hashtags.
- Weave in 8-12 keywords naturally — do not keyword-stuff.

== TAG RULES ==
- Generate tags in the 5 categories below. The merged "all_tags" array,
  joined as "tag1, tag2, tag3…" MUST be ≤ 480 characters total.
  YouTube hard-caps tags at 500 chars and SILENTLY DROPS the rest — budget
  carefully and put your highest-value tags FIRST in all_tags.
- long_tail_tags: 3-5 word phrases that match exactly how viewers phrase
  searches (not generic topic words). These are the highest ROI, lowest
  competition tag type — make them count.
- competitor_gap_tags: specific niche + format combos your competitors
  consistently miss (not just synonyms). E.g. "dark psychology manipulation
  signs" instead of just "dark psychology".
- all_tags: merged, deduplicated, priority-ordered (best tags first).

== THUMBNAIL TEXT RULES ==
- thumbnail_text_suggestions must PAIR with the primary title — together they
  form one CTR decision. Do NOT repeat the title verbatim.
- 3-5 words max. Should add context or emotion the title didn't convey.
- Think: what would stop a thumb mid-scroll alongside this title?

IMPORTANT — avoid anchoring: the numeric examples below (title_score: 85) are
FORMAT PLACEHOLDERS ONLY. Your real scores must come from genuine analysis and
can honestly land anywhere from 30 to 95.

CRITICAL JSON RULES:
- Every string value on ONE LINE (no literal newlines inside strings)
- Use \\n\\n for paragraph breaks inside full_text only
- No markdown fences, no commentary before or after the JSON
- Output must pass JSON.parse() without modification

{
  "titles": {
    "primary": "best title under 70 chars — front-loaded keyword, power words",
    "alternatives": [
      "curiosity-gap variant (must deliver on the Payoff above — no empty gaps)",
      "number or list format e.g. '7 Signs Your Boss Is Manipulating You'",
      "shock or controversy angle rooted in a real claim from the script",
      "direct benefit or promise — viewer gets X by watching",
      "question format that the video genuinely answers"
    ],
    "title_score": 85,
    "title_analysis": "specific explanation: which keywords are front-loaded, which power words are used, why this beats alternative framings, and which CTR psychology principle it leverages"
  },
  "thumbnail_text_suggestions": [
    "3-5 word overlay that pairs with primary title, adds new info or emotion",
    "alternative overlay — stronger emotional punch",
    "alternative overlay — creates urgency or intrigue without the title words"
  ],
  "description": {
    "full_text": "450-500 word SEO description. Paragraph 1 (150 chars max): primary keyword + compelling hook. Then body sections. Include timestamp skeleton. End with CTA and 3-5 hashtags. Use \\n\\n between paragraphs.",
    "first_line_hook": "verbatim first 150 chars of full_text — must contain primary keyword",
    "hashtags": ["#tag1", "#tag2", "#tag3", "#tag4", "#tag5"]
  },
  "tags": {
    "primary_tags":        ["5 exact-match high-priority tags — the 5 most important searches this video should rank for"],
    "secondary_tags":      ["10 related broad topic tags — next tier of relevance"],
    "long_tail_tags":      ["15 specific 3-5 word phrases viewers actually type — these are your highest ROI tags"],
    "trending_tags":       ["5 tags derived directly from the trending data above"],
    "competitor_gap_tags": ["5 niche+format combos competitors in this space consistently miss"],
    "all_tags":            ["complete merged deduplicated list of all tags, priority-ordered highest-value first, kept ≤ 480 chars when joined with commas"]
  },
  "engagement": {
    "pinned_comment": "short comment the creator pins immediately after upload — ideally an open question that invites replies and seeds engagement velocity in the first hour"
  }
}`, 2400, isValidSEO, 'Agent 3 (SEO Generator)') ?? {};

  // Defensive cleanup for fields the pipeline reads directly
  if (raw.tags) {
    ['primary_tags', 'secondary_tags', 'long_tail_tags', 'trending_tags', 'competitor_gap_tags']
      .forEach(k => { raw.tags[k] = coerceArray(raw.tags[k]); });
  }
  if (raw.titles) {
    raw.titles.alternatives = coerceArray(raw.titles.alternatives);
  }
  if (raw.description) {
    raw.description.hashtags = coerceArray(raw.description.hashtags);
  }
  raw.thumbnail_text_suggestions = coerceArray(raw.thumbnail_text_suggestions);

  // Enforce YouTube's 480-char budget (code-level safety net on top of the prompt instruction)
  return enforceTagBudget(raw, 480);
}


// ─────────────────────────────────────────────────────────────────────────────
// AGENT 4 — VIRAL PREDICTOR
// ─────────────────────────────────────────────────────────────────────────────
const A4_SYS =
  'You are a viral content analyst who has studied 50,000+ YouTube videos ' +
  'across every major niche. You understand the psychological triggers, ' +
  'algorithmic signals, audience timing, and competitive positioning that ' +
  'separate a 500-view video from a 500,000-view one. ' +
  'You are BRUTALLY honest — you do not flatter creators, you give them the ' +
  'truth they need to improve. You explain exactly WHY something will or will ' +
  'not work, not just that it will or will not. ' +
  'This video is for a podcast-style animated channel — two cartoon hosts ' +
  'discussing psychology, often reacting to a trending story or confession. ' +
  'Judge title strength, thumbnail synergy, and emotional impact with that ' +
  'two-host, reactive format in mind, not a single-presenter video. ' +
  'Respond with valid JSON only — no markdown, no preamble. ' +
  'Every string value MUST be on ONE LINE — use \\n for line breaks if needed.';

function isValidViral(r) {
  return Boolean(r?.score_breakdown?.trend_alignment !== undefined);
}

async function agent4_predictViral(scriptData, seoData, trends) {
  log('\n🚀  [Agent 4/4] Viral Predictor — scoring viral potential…');

  const primaryTitle = seoData?.titles?.primary      ?? 'N/A';
  const titleScore   = seoData?.titles?.title_score  ?? 'N/A';
  const viralTags    = seoData?.tags?.trending_tags  ?? [];
  const risingQry    = trends.googleTrends?.risingQueries ?? [];
  const redditScores = (trends.reddit ?? []).slice(0, 5).map(r => [r.title, r.score]);
  const topReddit    = (trends.reddit ?? []).slice(0, 3);

  const result = await nimJsonRetry(MODEL_VIRAL, A4_SYS,
`Analyze this YouTube video's viral potential with brutal, channel-growth-focused honesty.

== VIDEO METADATA ==
Topic             : ${scriptData.main_topic}
Niche             : ${scriptData.niche} / ${scriptData.sub_niche}
Content Type      : ${scriptData.content_type}
Tone              : ${scriptData.tone}
Primary Title     : ${primaryTitle}
Title Score (SEO) : ${titleScore}/100 (from Agent 3 analysis)
Hook Line         : ${scriptData.hook_line || '(not provided)'}
Payoff / Twist    : ${scriptData.payoff_or_twist || '(not provided)'}
Core Message      : ${scriptData.core_message}
Unique Angle      : ${scriptData.unique_angle}
Emotional Hooks   : ${JSON.stringify(scriptData.emotional_triggers ?? [])}
Duration (est.)   : ${scriptData.estimated_duration_mins} min
Difficulty Level  : ${scriptData.difficulty_level ?? 'not specified'}
Pacing            : ${scriptData.pacing ?? 'not specified'}

== TREND ALIGNMENT ==
Rising Google searches : ${JSON.stringify(risingQry)}
Top Reddit posts       : ${JSON.stringify(redditScores)}
Trending tags used     : ${JSON.stringify(viralTags)}
Top Reddit engagement  : ${JSON.stringify(topReddit.map(r => ({ title: r.title, score: r.score, comments: r.numComments })))}

== HOW TO SCORE (be honest — most videos are NOT viral) ==
Rate each component on its OWN merits. Do NOT anchor on the example numbers below.
Use the FULL 0-20 range: a score of 0-7 = weak, 8-13 = average, 14-17 = strong, 18-20 = exceptional.
Most videos should score 6-13 per component. Only genuine outliers get 16+.

- trend_alignment  (0-20): How precisely does this topic map to what is ACTUALLY
  trending in the data above — not just loosely related, but the exact conversation?
  A video that matches a specific rising query scores higher than one that matches
  the broad niche.

- title_strength   (0-20): CTR potential of the primary title. Penalize: generic
  phrasing, buried keywords, no power words, curiosity gaps that won't pay off,
  or misalignment between the title and the hook/payoff.

- emotional_impact (0-20): How strongly does the content activate a specific
  emotion? Weak = "interesting info". Strong = viewer feels something they didn't
  expect and wants to share.

- search_demand    (0-20): How many people are actively searching for this? Weigh
  search volume AGAINST competition. A niche with 10k monthly searches and 5
  competitors scores higher than one with 1M searches and 10,000 competitors.

- timing_score     (0-20): Is this topic timely, seasonal, or evergreen right now?
  Bonus if it taps a recent news cycle or cultural moment visible in the trend data.

viral_score = sum of all five (automatically computed from your component scores).

AVOID: clustering all scores in the 12-15 range. Force yourself to identify
genuine strengths AND genuine weaknesses per component.

CRITICAL JSON RULES: every string value on ONE LINE, no markdown, no preamble.

IMPORTANT — the numbers below (78, 14, 16 etc.) are FORMAT EXAMPLES ONLY.
Do not anchor on them. Score honestly.

{
  "viral_score": 78,
  "confidence": "high|medium|low",
  "verdict": "one direct sentence: what this video's realistic ceiling is and why",
  "score_breakdown": {
    "trend_alignment":  14,
    "title_strength":   16,
    "emotional_impact": 18,
    "search_demand":    15,
    "timing_score":     15,
    "analysis": "2-3 sentences: explain each score briefly — what raised it, what capped it, and what the most actionable lever is"
  },
  "biggest_risk": "the single most likely reason this video underperforms — be specific (not just 'low CTR' but WHY)",
  "hidden_opportunity": "one overlooked angle or keyword gap that could 2-5x the views if exploited",
  "best_upload_times": {
    "best_days":   ["Thursday", "Friday"],
    "time_window": "14:00-17:00 UTC",
    "avoid_days":  ["Monday", "Tuesday"],
    "reasoning":   "specific reason why this niche's audience is active at those times"
  },
  "predicted_performance": {
    "views_48h":        "realistic 48-hour range based on current trend alignment",
    "views_7d":         "realistic 7-day range",
    "ctr_estimate":     "realistic CTR range for this niche and title quality",
    "subscriber_conversion": "estimated % of viewers who will subscribe",
    "growth_catalyst":  "the specific event, share, or algorithm trigger that could push this 10x"
  },
  "critical_improvements": [
    "most impactful single change — specific and actionable, not generic",
    "second most impactful — different dimension from #1",
    "third most impactful — different dimension from #1 and #2"
  ],
  "retention_risk": "where in the video viewers are most likely to drop off and why"
}`, 1800, isValidViral, 'Agent 4 (Viral Predictor)') ?? {};

  // Re-derive viral_score from clamped components — the prompt defines it as
  // their sum, so code now enforces that instead of trusting the model's math (fix #8)
  const b = result.score_breakdown ?? {};
  const clamp20 = (v) => Math.max(0, Math.min(20, parseInt(v) || 0));
  const components = {
    trend_alignment:  clamp20(b.trend_alignment),
    title_strength:   clamp20(b.title_strength),
    emotional_impact: clamp20(b.emotional_impact),
    search_demand:    clamp20(b.search_demand),
    timing_score:     clamp20(b.timing_score),
  };
  result.score_breakdown = { ...b, ...components };
  result.viral_score     = Object.values(components).reduce((sum, v) => sum + v, 0);

  // Defensive coercion for fields read elsewhere as arrays / enums
  if (result.best_upload_times) {
    result.best_upload_times.best_days  = coerceArray(result.best_upload_times.best_days);
    result.best_upload_times.avoid_days = coerceArray(result.best_upload_times.avoid_days);
  }
  result.critical_improvements = coerceArray(result.critical_improvements);
  if (!['high', 'medium', 'low'].includes(result.confidence)) {
    result.confidence = 'medium';
  }

  log(`🔥  Viral score : ${result.viral_score}/100  (${result.confidence} confidence)`);
  log(`⚡  Verdict     : ${result.verdict || '(not provided)'}`);
  log(`🎯  Top fix     : ${result.critical_improvements[0] || '(not provided)'}`);
  return result;
}


// ─────────────────────────────────────────────────────────────────────────────
// MAIN  —  full pipeline + upload
// ─────────────────────────────────────────────────────────────────────────────
async function uploadProcess() {
  console.log('\n🚀  YouTube Auto-Uploader  ·  SEO Sniper Edition  v2.1\n');
  barStart(0, 'Starting…');

  try {
    // ── Stage 1: Read script ───────────────────────────────────────────────
    barUpdate(5, 'Reading script…');
    const scriptText = await readDocxScript();
    barUpdate(12, 'Script loaded');
    log(`✅  Script: ${scriptText.length.toLocaleString()} chars`);

    barStop();   // pause bar — agents produce their own log lines

    // ── Agent 1 ────────────────────────────────────────────────────────────
    const scriptData = await agent1_parseScript(scriptText);
    barStart(25, 'Agent 1 done');

    // ── Agent 2 ────────────────────────────────────────────────────────────
    barUpdate(25, 'Collecting trends…');
    barStop();
    const trends = await agent2_collectTrends(scriptData);
    barStart(50, 'Trends collected');

    // ── Agent 3 ────────────────────────────────────────────────────────────
    barUpdate(50, 'Generating SEO…');
    barStop();
    await sleep(8_000);   
    const seoData = await agent3_generateSEO(scriptData, trends);
    barStart(65, 'SEO done');

    // ── Agent 4 ────────────────────────────────────────────────────────────
    barUpdate(65, 'Predicting virality…');
    barStop();
    await sleep(8_000);  
    const viralData = await agent4_predictViral(scriptData, seoData, trends);
    barStart(75, 'Analysis complete');

    // ── Extract upload metadata ────────────────────────────────────────────
    // Enforce title character limit (from seo_sniper.py _validate_output)
    let title = seoData?.titles?.primary ?? scriptData.main_topic ?? 'Untitled';
    if (title.length > 70) {
      const cut = title.slice(0, 70);
      const sp  = cut.lastIndexOf(' ');
      title = sp > 40 ? cut.slice(0, sp) : cut;
    }

    const description = seoData?.description?.full_text ?? '';

    // Enforce YouTube's 500-character hard limit on total tag string.
    // enforceTagBudget already ran in Agent 3, but a final greedy pass
    // here guards against anything that slipped through.
    const rawTags = seoData?.tags?.all_tags ?? [];
    const tags = [];
    let tagChars = 0;
    for (const t of rawTags) {
      const cost = (tags.length ? 2 : 0) + t.length;   // 2 = ", " separator
      if (tagChars + cost > 500) break;
      tags.push(t);
      tagChars += cost;
    }
    if (rawTags.length > tags.length)
      log(`ℹ️   Tags capped: kept ${tags.length}/${rawTags.length} (${tagChars}/500 chars)`);

    const viralScore  = viralData?.viral_score ?? '?';
    const confidence  = viralData?.confidence  ?? '';
    const verdict     = viralData?.verdict     ?? '';
    const bestDay     = viralData?.best_upload_times?.best_days?.[0]     ?? 'N/A';
    const timeWindow  = viralData?.best_upload_times?.time_window        ?? '';
    const views7d     = viralData?.predicted_performance?.views_7d       ?? 'N/A';
    const topFix      = viralData?.critical_improvements?.[0]            ?? 'N/A';
    const topTag      = tags[0] ?? 'N/A';
    const thumbText   = seoData?.thumbnail_text_suggestions?.[0]         ?? 'N/A';
    const pinnedCmt   = seoData?.engagement?.pinned_comment              ?? '';

    // Summary banner
    console.log('\n' + '═'.repeat(68));
    console.log(`  🏆  TITLE       ${title}`);
    console.log(`  🖼️   THUMBNAIL   ${thumbText}`);
    console.log(`  🔥  VIRAL SCORE ${viralScore}/100  (${confidence})  —  ${verdict}`);
    console.log(`  🏷️   TOP TAG     ${topTag}`);
    console.log(`  📅  UPLOAD      ${bestDay}  ${timeWindow}`);
    console.log(`  📈  VIEWS (7d)  ${views7d}`);
    console.log(`  💡  TOP FIX     ${topFix}`);
    if (pinnedCmt) console.log(`  📌  PIN COMMENT ${pinnedCmt.slice(0, 80)}…`);
    console.log('');

    // ── Select video file ──────────────────────────────────────────────────
    barUpdate(78, 'Selecting video…');
    const videoDir  = path.resolve(path.dirname(CONFIG_PATH), cfg.paths.video_folder);
    const videoList = fs.readdirSync(videoDir).filter(f => f.toLowerCase().endsWith('.mp4'));
    if (!videoList.length) throw new Error(`No .mp4 files found in: ${videoDir}`);

    const chosen    = videoList[Math.floor(Math.random() * videoList.length)];
    const videoPath = path.join(videoDir, chosen);
    log(`🎬  Selected: ${chosen}`);

    // ── Find thumbnail ─────────────────────────────────────────────────────
    const thumbDir = path.resolve(
      path.dirname(CONFIG_PATH),
      path.join('thumbnail', 'output_thumbnail')
    );
    const THUMB_EXTS = ['.jpg', '.jpeg', '.png'];
    let thumbPath = null;
    if (fs.existsSync(thumbDir)) {
      const thumbFile = fs.readdirSync(thumbDir)
        .find(f => THUMB_EXTS.includes(path.extname(f).toLowerCase()));
      if (thumbFile) {
        thumbPath = path.join(thumbDir, thumbFile);
        log(`🖼️   Thumbnail : ${thumbFile}`);
      } else {
        log('⚠️   No thumbnail image found — uploading without.');
      }
    } else {
      log(`⚠️   Thumbnail folder not found (${thumbDir}) — uploading without.`);
    }

    // ── Upload ─────────────────────────────────────────────────────────────
    barUpdate(80, 'Uploading to YouTube…');
    barStop();
    console.log('\n⏳  Uploading — live % is shown in the browser window.\n');

    let videoUrls;
    try {
      videoUrls = await upload(
        { email: cfg.youtube.email, pass: cfg.youtube.pass, recoveryemail: cfg.youtube.recoveryemail },
        [{
          path:               videoPath,
          title,
          description,
          tags,
          ...(thumbPath ? { thumbnail: thumbPath } : {}),
          language:           cfg.upload?.language            ?? 'english',
          channelName:        cfg.upload?.channel_name        ?? '',
          skipProcessingWait: true,
          publishType:        cfg.upload?.publish_type        ?? 'PUBLIC',
          isAgeRestriction:   cfg.upload?.is_age_restriction  ?? false,
          isNotForKid:        cfg.upload?.is_not_for_kid      ?? true,
        }],
        {
          headless: false,
          timeout:  300_000,   // 5 min — more time for YouTube UI to load
        }
      );

    } catch (uploadErr) {
      // ── Visibility error: YouTube Studio setup is missing ────────────────
      // The library cannot find the publish-type dropdown. This is not a code
      // bug — it means YouTube Studio's upload defaults are not configured.
      if (uploadErr.message.toLowerCase().includes('visibility')) {
        throw new Error(
          'YouTube visibility setup error — the library cannot find the publish dropdown.\n\n' +
          '  Fix (takes 2 minutes):\n' +
          '  1.  Open  https://studio.youtube.com\n' +
          '  2.  Click  Settings  →  Upload defaults  →  Basic info\n' +
          '  3.  Set the "Visibility" field to "Public"  →  Save\n' +
          '  4.  Re-run this script.\n\n' +
          '  If the error persists, the YouTube UI may have changed —\n' +
          '  update the library:  npm install youtube-videos-uploader@latest'
        );
      }
      throw uploadErr;
    }

    // ── Archive video ──────────────────────────────────────────────────────
    barStart(90, 'Archiving…');
    const uploadedDir = path.resolve(path.dirname(CONFIG_PATH), cfg.paths.uploaded_folder);
    fs.mkdirSync(uploadedDir, { recursive: true });

    // Sanitize the SEO title for use as a filename (Windows-safe)
    const safeName = (title ?? 'uploaded_video')
      .replace(/[\\/:*?"<>|]/g, '')   // strip characters illegal on Windows
      .replace(/\s+/g, ' ')           // collapse whitespace runs
      .trim()
      .replace(/[. ]+$/, '')          // trailing dots/spaces are illegal on Windows
      || 'uploaded_video';             // fallback if the entire title was stripped

    // Determine next Video_N folder number
    const existingFolders = fs.readdirSync(uploadedDir)
      .filter(f => /^Video_\d+$/.test(f) &&
                   fs.statSync(path.join(uploadedDir, f)).isDirectory());
    const nextNum = existingFolders.length
      ? Math.max(...existingFolders.map(f => parseInt(f.split('_')[1], 10))) + 1
      : 1;
    const videoArchiveFolder = path.join(uploadedDir, `Video_${nextNum}`);
    fs.mkdirSync(videoArchiveFolder, { recursive: true });

    // Move video
    const archiveName = `${safeName}.mp4`;
    fs.renameSync(videoPath, path.join(videoArchiveFolder, archiveName));

    // Move thumbnail (if one was used)
    if (thumbPath && fs.existsSync(thumbPath)) {
      const thumbExt = path.extname(thumbPath).toLowerCase();
      fs.renameSync(thumbPath, path.join(videoArchiveFolder, `${safeName}${thumbExt}`));
    }

    barUpdate(100, '✅  Done!');
    barStop();

    console.log(`\n✅  Upload complete!  Archived → Video_${nextNum}/${archiveName}`);
    console.log(`📺  URL(s): ${Array.isArray(videoUrls) ? videoUrls.join(', ') : videoUrls}\n`);

  } catch (err) {
    if (BS.active) barStop();
    // Indent every line of the error message for readability
    console.error('\n❌  Error:\n' + err.message.split('\n').map(l => '    ' + l).join('\n') + '\n');
    process.exit(1);
  }
}

uploadProcess();