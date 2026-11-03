#!/usr/bin/env python3
"""Slant Meter pipeline: RSS -> dedupe -> Claude scoring -> stories.json

Usage:
  python fetch_and_score.py                       # live: feeds.json + Anthropic API
  python fetch_and_score.py --mock --feeds-dir fixtures   # offline test, no API/network
Env: ANTHROPIC_API_KEY, SLANT_MODEL (default claude-sonnet-5-5)
"""
import argparse, glob, hashlib, html, json, os, re, sys, time
try:  # use the operating system's certificate store (fixes SSL errors behind corporate proxies / on some Windows setups)
    import truststore; truststore.inject_into_ssl()
except Exception:
    pass
import urllib.request, urllib.parse, urllib.robotparser
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

MODEL = os.environ.get("SLANT_MODEL", "claude-sonnet-5-5")
UA = "SlantMeterBot/0.1 (personal project)"
PROMPT = """You are a careful media-bias analyst. Analyze this political news item. TEXT BASIS below says whether you have only a headline and feed summary, or also the first ~500 words of the article.
You MUST report your analysis by calling the report_score tool (do not answer in prose). Its fields: {"political": boolean (true ONLY if this is news about government, elections, politicians, public policy or political events; false for ads, sponsored content, loan/mortgage/credit offers, shopping, lifestyle, sports, entertainment or anything else; if false, every other field may be null),
"opinion": boolean (true if this is a columnist, opinion, editorial or commentary piece rather than straight news reporting; cues include labels like OPINION, a columnist surname prefix such as "ROOKE:", first-person argument, or a thesis headline),
"scorable": boolean (false if the text is too thin to judge slant, for example only a headline plus a fragment of a sentence and no article excerpt; if false, strength and spin must be null and signals empty),
"favors": "left"|"right"|"neither" (whose viewpoint the WRITING favors: left = Democratic/progressive side, right = Republican/conservative side; judge framing, word choice and sourcing, not the topic or who won),
"strength": integer 0..100 (how strongly the writing favors that side; 0 if neither),
"spin": integer 0..100 (how far the text pushes a conclusion beyond its facts),
"leanWhy": string (2 sentences),
"signals": [{"type": string (Loaded language, Framing, Source selection, Omission, Headline mismatch...), "quote": exact short quote from the text, "why": one sentence}],
"confidence": "low"|"medium"|"high"}
Rules: 0-4 signals, listing ONLY real evidence of slant or spin (none for neutral reporting, and never list balancing or neutral features as signals); quotes must appear verbatim in the input; do not infer from the outlet's reputation; if only a headline and a short blurb are available, say so in leanWhy and set confidence low or medium; if an article excerpt is provided, use all of it (sourcing, attribution, context) and confidence may be medium or high, but remember it is only the opening of the article. Never invent omissions you cannot see from the text; name an omission only if the text itself implies it.
Judge the WRITING, not the news. A story reporting an event that is bad for one side (a court ruling against a politician, poor poll numbers, an arrest) is not biased for that reason alone. Standard attribution verbs such as "claims", "alleges", "says" or "suspects" are not slant by themselves. Do not move the lean score because of who won or lost; move it only for framing, word choice, sourcing and emphasis. Neutral items should get lean near 0 and spin under 15.

HEADLINE: {title}
SUMMARY: {summary}
TEXT BASIS: {basis}
ARTICLE EXCERPT: {excerpt}
"""

SCORE_TOOL = {"name": "report_score", "description": "Report the bias analysis of one news item.",
  "input_schema": {"type": "object", "required": ["political"], "properties": {
    "political": {"type": "boolean"}, "opinion": {"type": "boolean"}, "scorable": {"type": "boolean"},
    "favors": {"type": ["string", "null"], "description": "left, right or neither"},
    "strength": {"type": ["integer", "null"], "description": "0-100"},
    "spin": {"type": ["integer", "null"], "description": "0-100"},
    "leanWhy": {"type": ["string", "null"]},
    "signals": {"type": "array", "items": {"type": "object", "required": ["type", "quote", "why"],
        "properties": {"type": {"type": "string"}, "quote": {"type": "string"}, "why": {"type": "string"}}}},
    "confidence": {"type": ["string", "null"], "description": "low, medium or high"}}}}
GROUP_TOOL = {"name": "report_groups", "description": "Report which news items cover the same event.",
  "input_schema": {"type": "object", "required": ["groups"], "properties": {
    "groups": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}}}}

def strip(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()

def trim(t, n=320):
    if len(t) <= n: return t
    cut = t[:n]
    k = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    cut = cut[: k + 1] if k > n * 0.5 else cut[: cut.rfind(" ")] + "\u2026"
    return cut

def parse_feed(xml_bytes, outlet):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):  # RSS 2.0; Atom handled below
        g = lambda t: (it.findtext(t) or "")
        items.append((g("title"), g("link"), g("description"), g("pubDate")))
    ns = "{http://www.w3.org/2005/Atom}"
    for e in root.iter(ns + "entry"):
        l = e.find(ns + "link")
        items.append((e.findtext(ns + "title") or "", l.get("href") if l is not None else "",
                      e.findtext(ns + "summary") or "", e.findtext(ns + "updated") or ""))
    out = []
    for title, link, desc, date in items:
        try:
            ts = parsedate_to_datetime(date).astimezone(timezone.utc) if date else datetime.now(timezone.utc)
        except Exception:
            try: ts = datetime.fromisoformat(date.replace("Z", "+00:00"))
            except Exception: ts = datetime.now(timezone.utc)
        if title.strip():
            out.append({"outlet": outlet, "title": strip(title), "link": link.strip(),
                        "summary": trim(strip(desc)), "published": ts.isoformat()})
    return out

def load_items(args):
    items = []
    if args.feeds_dir:  # offline fixtures: files named "<Outlet>.xml"
        for p in sorted(glob.glob(os.path.join(args.feeds_dir, "*.xml"))):
            items += parse_feed(open(p, "rb").read(), os.path.splitext(os.path.basename(p))[0])
        return items
    for f in json.load(open(args.feeds, encoding="utf-8"))["feeds"]:
        try:
            req = urllib.request.Request(f["url"], headers={"User-Agent": UA})
            got = parse_feed(urllib.request.urlopen(req, timeout=20).read(), f["outlet"])
            for x in got: x["fulltext"] = bool(f.get("fulltext"))
            items += got
        except Exception as e:
            print(f"! {f['outlet']}: {e}", file=sys.stderr)
    return items

def sid(it): return hashlib.sha1((it["outlet"] + it["link"] + it["title"]).encode()).hexdigest()[:12]

class _Article(HTMLParser):
    """Tiny dependency-free article extractor: collects paragraph text, preferring <article>.
    Skips page chrome: nav/footer/aside, embeds (blockquote/tweets), and containers whose class or id marks them as
    related stories, promos, newsletters, share bars, ads, comments, captions, etc."""
    SKIP = {"script", "style", "noscript", "nav", "footer", "aside", "header", "form", "figure", "figcaption", "svg", "iframe", "button", "blockquote", "select", "template"}
    VOID = {"br", "img", "hr", "meta", "link", "input", "source", "wbr", "area", "base", "col", "embed", "param", "track"}
    BADTOK = {"related", "promo", "promoted", "recommended", "recommendation", "recommendations", "trending", "popular", "newsletter",
              "subscribe", "subscription", "signup", "share", "sharing", "social", "sidebar", "comments", "comment", "advert",
              "advertisement", "ads", "ad", "outbrain", "taboola", "embed", "tweet", "twitter", "teaser", "caption", "credit", "readmore", "morestories"}
    BADSTART = ("sign up", "get our", "subscribe", "click here", "read more", "download", "follow us", "advertisement", "related:", "watch:", "listen:")
    def __init__(self):
        super().__init__(); self.stack = []; self.skip = 0; self.art_depth = 0; self.in_p = False; self.cur = []; self.art = []; self.all = []
    def handle_starttag(self, t, a):
        if t in self.VOID: return
        toks = set(re.split(r"[^a-z0-9]+", " ".join(v or "" for k, v in a if k in ("class", "id")).lower()))
        bad = t in self.SKIP or bool(toks & self.BADTOK)
        self.stack.append((t, bad))
        if bad: self.skip += 1
        if t == "article": self.art_depth += 1
        if t == "p" and not self.skip: self.in_p = True; self.cur = []
    def _finish_p(self):
        self.in_p = False
        txt = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        letters = [c for c in txt if c.isalpha()]
        shouty = letters and sum(c.isupper() for c in letters) / len(letters) > 0.7 and len(txt.split()) < 30  # ALL-CAPS teaser headlines
        if len(txt) >= 40 and not shouty and not txt.lower().startswith(self.BADSTART):
            self.all.append(txt)
            if self.art_depth: self.art.append(txt)
    def handle_endtag(self, t):
        if t == "p" and self.in_p: self._finish_p()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == t:
                for tt, bad in self.stack[i:]:
                    if bad: self.skip -= 1
                    if tt == "article": self.art_depth -= 1
                del self.stack[i:]
                break
    def handle_data(self, d):
        if self.in_p and not self.skip: self.cur.append(d)

_robots = {}
def allowed(url):
    """Respect robots.txt. If robots.txt can't be read, fall through to the fetch (which fails if the site blocks us)."""
    p = urllib.parse.urlparse(url); base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": UA})
            rp.parse(urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore").splitlines())
            _robots[base] = rp
        except Exception:
            _robots[base] = None
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(UA, url)

def dbg(msg):
    if os.environ.get("SLANT_DEBUG"): print("  [fulltext] " + msg, file=sys.stderr)

def fetch_excerpt(url, words=500):
    """First ~500 words of the article body, or None (blocked, paywalled/too short, not allowed by robots.txt). Never stored.
    Set SLANT_DEBUG=1 to see why a story fell back to headline + summary."""
    if not url.startswith("http"): return None
    if not allowed(url): dbg("robots.txt disallows " + url); return None
    try:
        for attempt in (1, 2):  # slow sites sometimes time out once
            try:
                raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30).read(); break
            except Exception as e:
                if attempt == 2: raise
                dbg(f"retrying after {e}")
        p = _Article(); p.feed(raw.decode("utf-8", "replace"))
    except Exception as e:
        dbg(f"fetch failed ({e}) for {url}"); return None
    seen, paras = set(), []
    for t in (p.art or p.all):
        if t not in seen: seen.add(t); paras.append(t)
    w = " ".join(paras).split()
    if len(w) < 60: dbg(f"only {len(w)} words extracted (paywall, video page or JavaScript-rendered) for {url}"); return None
    return " ".join(w[:words])

def norm(t):
    t = t.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"').replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", t).lower()

def score_live(client, it):
    basis = "headline, feed summary and article excerpt (first ~500 words)" if it.get("_excerpt") else "headline and feed summary only"
    prompt_text = (PROMPT.replace("{title}", it["title"]).replace("{summary}", it["summary"] or "(none)")
                   .replace("{basis}", basis).replace("{excerpt}", it.get("_excerpt") or "(not available)"))
    for attempt in (1, 2):  # one retry if the reply has no usable structured answer
        msg = client.messages.create(model=MODEL, max_tokens=1500, tools=[SCORE_TOOL],
                                     messages=[{"role": "user", "content": prompt_text + ("" if attempt == 1 else "\n\nCall the report_score tool now.")}])
        try:
            return tool_input(msg, "scoring")
        except ValueError:
            if attempt == 2: raise

def score_mock(it):
    if re.search(r"mortgage|loan|credit card|sponsored", it["title"], re.I):
        return {"political": False}
    h = int(hashlib.sha1(it["title"].encode()).hexdigest(), 16)
    return {"opinion": it["title"].upper().startswith("OPINION"), "scorable": True, "lean": h % 121 - 60, "spin": h % 70 + 10, "leanWhy": "MOCK score for pipeline testing only.",
            "signals": [{"type": "Mock", "quote": it["title"][:40], "why": "Placeholder."}], "confidence": "low"}

STOP = set("the a an and or of to in on for with as at by from is are was were be after over amid says say new this that its it his her their into about".split())
def tokens(t): return {w for w in re.findall(r"[a-z0-9']+", t.lower()) if len(w) > 3 and w not in STOP}

def cluster(stories, thresh=0.34, window_h=72):
    """Group stories about the same event (title word overlap, within a time window) so we can count distinct outlets covering it.
    Crude on purpose; swap for embeddings later."""
    stories.sort(key=lambda x: x["published"])  # oldest first
    reps = []  # (cluster_id, tokens, published_dt)
    for s in stories:
        tk, dt = tokens(s["title"]), datetime.fromisoformat(s["published"])
        best = None
        for cid, rt, rdt in reps:
            if abs((dt - rdt).total_seconds()) > window_h * 3600: continue
            j = len(tk & rt) / max(1, len(tk | rt))
            if j >= thresh and (best is None or j > best[0]): best = (j, cid)
        if best: s["cluster"] = best[1]
        else: s["cluster"] = s["id"]; reps.append((s["id"], tk, dt))
    outlets = {}
    for s in stories: outlets.setdefault(s["cluster"], set()).add(s["outlet"])
    for s in stories: s["coverage"] = len(outlets[s["cluster"]])
    return stories

def apply_groups(stories, groups):
    byid = {s["id"]: s for s in stories}
    for g in groups:
        ids = [i for i in g if i in byid]
        if len(ids) < 2: continue
        for i in ids: byid[i]["cluster"] = ids[0]
    outlets = {}
    for s in stories: outlets.setdefault(s["cluster"], set()).add(s["outlet"])
    for s in stories: s["coverage"] = len(outlets[s["cluster"]])
    return stories

def cluster_llm(client, stories, n=80):
    """One extra API call per run: ask Claude which headlines are about the SAME event."""
    recent = sorted(stories, key=lambda x: x["published"], reverse=True)[:n]
    lines = "\n".join(f'{s["id"]} | {s["outlet"]} | {s["title"]}' for s in recent)
    prompt = ("Group the news items below that report the SAME specific event or story (not merely the same broad topic). "
              'You MUST answer by calling the report_groups tool, with groups as lists of ids, only groups of 2 or more items. Use the ids exactly as given.\n\n' + lines)
    msg = client.messages.create(model=MODEL, max_tokens=2000, tools=[GROUP_TOOL],
                                 messages=[{"role": "user", "content": prompt}])
    return tool_input(msg, "event grouping")["groups"]

def tool_input(msg, what):
    """Forced tool use returns the arguments as an already-parsed object, so stray quotes can't break the JSON."""
    for b in msg.content:
        if getattr(b, "type", "") == "tool_use":
            return b.input
    return parse_json_reply(msg, what)  # fallback if the model answered in plain text

def parse_json_reply(msg, what):
    """Pull the JSON object out of a model reply, with a readable error if the reply has none (refusal, empty, cut off)."""
    text = "".join(b.text for b in msg.content if b.type == "text")
    if "{" not in text or "}" not in text:
        raise ValueError(f"{what}: no JSON in reply (stop_reason={getattr(msg, 'stop_reason', None)}, reply starts {text[:100]!r})")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])  # ignores any text after the JSON object
        return obj
    except json.JSONDecodeError as e:
        raise ValueError(f"{what}: reply was not valid JSON ({e}); stop_reason={getattr(msg, 'stop_reason', None)}")

def lean_from(r):
    """Direction and strength are asked for separately and combined here, so the sign can't contradict the explanation."""
    if "favors" in r or "strength" in r:
        if r.get("strength") is None: return None
        f = str(r.get("favors", "")).lower()
        return (-1 if f == "left" else 1 if f == "right" else 0) * float(r["strength"])
    return r.get("lean")  # older / mock format

def validate(r, it):
    if r.get("political") is False:
        return {"political": False}
    opinion = bool(r.get("opinion"))
    lean_raw = lean_from(r)
    thin = it.get("basis") != "article" and len((it.get("summary") or "").split()) < 8
    if r.get("scorable") is False or lean_raw is None or r.get("spin") is None or thin:
        return {"political": True, "scorable": False, "opinion": opinion, "lean": None, "spin": None, "signals": [],
                "leanWhy": "Not enough text was available to judge slant fairly.", "confidence": "low"}
    clamp = lambda n, a, b: max(a, min(b, int(round(float(n)))))
    hay = norm(it["title"] + " " + it["summary"] + " " + it.get("_excerpt", ""))
    sigs = []
    for s in r.get("signals", [])[:5]:
        q = str(s.get("quote", ""))
        # keep only signals whose quote really appears in the source text (guards against invented evidence)
        if q and (norm(q).strip("()\"' ") in hay or s.get("type") == "Mock"):
            sigs.append({"type": str(s.get("type", "")), "quote": q, "why": str(s.get("why", ""))})
    return {"political": True, "scorable": True, "opinion": opinion, "lean": clamp(lean_raw, -100, 100), "spin": clamp(r["spin"], 0, 100),
            "leanWhy": str(r.get("leanWhy") or ""), "signals": sigs, "confidence": r.get("confidence") or "low"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", default="feeds.json"); ap.add_argument("--feeds-dir")
    ap.add_argument("--out", default="stories.json"); ap.add_argument("--mock", action="store_true")
    ap.add_argument("--limit", type=int, default=40, help="max new stories scored per run (cost cap)")
    a = ap.parse_args()
    old = {s["id"]: s for s in (json.load(open(a.out, encoding="utf-8")) if os.path.exists(a.out) else [])}
    skip_path = a.out + ".skipped"  # ids of items judged non-political, so we never pay to score them twice
    skipped = set(json.load(open(skip_path, encoding="utf-8"))) if os.path.exists(skip_path) else set()
    items = load_items(a)
    seen, fresh = set(), []
    for it in sorted(items, key=lambda x: x["published"], reverse=True):
        i = sid(it)
        if i in old or i in seen or i in skipped: continue
        seen.add(i); it["id"] = i; fresh.append(it)
    by_outlet = {}
    for it in fresh: by_outlet.setdefault(it["outlet"], []).append(it)
    fresh = []  # round-robin so one noisy feed can't use up the whole --limit
    while any(by_outlet.values()) and len(fresh) < a.limit:
        for o in by_outlet:
            if by_outlet[o] and len(fresh) < a.limit: fresh.append(by_outlet[o].pop(0))
    client = None
    if not a.mock:
        import anthropic; client = anthropic.Anthropic()
    for it in fresh:
        try:
            if it.get("fulltext"):
                it["_excerpt"] = fetch_excerpt(it["link"]) or ""
                time.sleep(1)  # be polite to the outlet's server
            it["basis"] = "article" if it.get("_excerpt") else "summary"
            r = score_mock(it) if a.mock else score_live(client, it)
            v = validate(r, it)
            if not v.get("political", True):
                skipped.add(it["id"]); print(f"  skipped (not political): {it['outlet']}: {it['title'][:60]}"); continue
            it.update(v); it.pop("_excerpt", None); it.pop("fulltext", None)  # article text is never saved
            old[it["id"]] = it
        except Exception as e:
            it.pop("_excerpt", None)
            print(f"! scoring failed for {it['outlet']}: {it['title'][:50]!r}: {type(e).__name__}: {e}", file=sys.stderr)
        if not a.mock: time.sleep(0.5)
    out = sorted(old.values(), key=lambda x: x["published"], reverse=True)[:300]
    out = sorted(cluster(out), key=lambda x: x["published"], reverse=True)
    if client and out:
        for attempt in (1, 2):  # one retry; a transient hiccup shouldn't cost the Hot tags
            try: out = apply_groups(out, cluster_llm(client, out)); break
            except Exception as e: print(f"! event grouping attempt {attempt} failed: {type(e).__name__}: {e}", file=sys.stderr)
    json.dump(out, open(a.out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    json.dump(sorted(skipped), open(skip_path, "w", encoding="utf-8"))
    print(f"{len(fresh)} new, {len(out)} total -> {a.out}")

if __name__ == "__main__":
    main()
