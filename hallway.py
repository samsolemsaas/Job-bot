#!/usr/bin/env python3
"""Hallway Track: find the buyer conversations a company should weigh in on.

It listens to buyers, not competitors. Each run it:
  1. Collects public conversations from Hacker News, Information Security Stack
     Exchange, Mastodon (infosec.exchange), Google News and, with keys, Reddit.
  2. For each lens in lenses/ (one per company), keeps on-topic conversations and
     tags who is talking and which buyer problem they raise.
  3. Scores every conversation for how worth it is to weigh in: is it a problem the
     company can speak to, is the person asking, is anyone answering, is it fresh.
  4. Rolls conversations up into topics to own (buyer problems the company can
     credibly speak to that are getting louder or going unanswered), emerging topics
     nobody has named yet, the rooms to be in and the words buyers use.
  5. Uses GitHub Models (free) to draft replies, name emerging topics and write a
     weekly brief.
  6. Saves everything to hallway.json for the dashboard (index.html).

Privacy: public posts only, no usernames stored, short excerpts that link back.

Run locally:  python hallway.py --dry-run
"""
import base64
import hashlib
import json
import math
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(ROOT, "hallway.json")
NOW = datetime.now(timezone.utc)
UA = {"User-Agent": "hallway-track/2.0 (public research; +https://github.com)"}
MODEL_URL = "https://models.github.ai/inference/chat/completions"
MODELS = [m for m in [os.environ.get("HALLWAY_MODEL"), "openai/gpt-4.1-mini", "openai/gpt-4o-mini"] if m]

STOP = set("""a about above after again against all am an and any are as at be because been before
being below between both but by can could did do does doing down during each few for from further
had has have having he her here hers him his how i if in into is it its itself just me more most my
no nor not now of off on once only or other our ours out over own same she should so some such than
that the their them then there these they this those through to too under until up very was we were
what when where which while who whom why will with would you your yours i'm it's don't can't we're
they're you're i've we've isn't doesn't didn't won't also really like get got one two us use used
using any anyone thing things lot much many even still well way make made want need know think see
going go new time year years day days people every something someone etc via re http https www com
gt lt amp quot nbsp slack discord""".split())


# ---------------- Helpers ----------------
def load_toml(path):
    with open(os.path.join(ROOT, path), "rb") as f:
        return tomllib.load(f)


def http(url, headers=None, data=None, timeout=25):
    h = dict(UA)
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        print(f"  {e.code} from {url[:80]}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"  fetch failed: {url[:80]} ({e})")
    return None


def clean(text):
    text = unescape(unescape(re.sub(r"<[^>]+>", " ", text or "")))
    return re.sub(r"\s+", " ", text).strip()


def hit(text, phrase):
    p = phrase.lower().strip()
    if not p:
        return False
    if len(p) <= 3:
        return re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", text) is not None
    return re.search(r"(?<![a-z0-9])" + re.escape(p), text) is not None


def day(s):
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def age(c):
    d = day(c.get("date"))
    return (NOW - d).days if d else 999


def cid(source, key):
    return hashlib.sha1(f"{source}|{key}".encode()).hexdigest()[:12]


# ---------------- Collectors ----------------
def from_hn(queries, since):
    out = []
    for q in queries:
        for tag in ("story", "comment"):
            url = ("https://hn.algolia.com/api/v1/search_by_date?hitsPerPage=50&tags=" + tag
                   + "&query=" + urllib.parse.quote(q)
                   + f"&numericFilters=created_at_i>{int(since.timestamp())}")
            raw = http(url)
            if not raw:
                continue
            for h in json.loads(raw).get("hits", []):
                oid = h.get("objectID")
                out.append({"id": cid("hn", oid), "source": "Hacker News", "room": "Hacker News",
                            "title": clean(h.get("title") or h.get("story_title") or ""),
                            "text": clean(h.get("comment_text") or h.get("story_text") or ""),
                            "url": f"https://news.ycombinator.com/item?id={oid}",
                            "date": (h.get("created_at") or "")[:10],
                            "kind": "comment" if tag == "comment" else "thread",
                            "replies": h.get("num_comments") if tag == "story" else None})
            time.sleep(0.3)
    return out


def from_stackexchange(queries, since):
    out = []
    for q in queries:
        url = ("https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=creation"
               "&site=security&filter=withbody&pagesize=30&q=" + urllib.parse.quote(q)
               + f"&fromdate={int(since.timestamp())}")
        raw = http(url, headers={"Accept-Encoding": "identity"})
        if not raw:
            continue
        try:
            items = json.loads(raw).get("items", [])
        except ValueError:
            continue
        for it in items:
            out.append({"id": cid("se", it.get("question_id")), "source": "Security Stack Exchange",
                        "room": "Security Stack Exchange", "title": clean(it.get("title")),
                        "text": clean(it.get("body")), "url": it.get("link", ""),
                        "date": datetime.fromtimestamp(it.get("creation_date", 0), timezone.utc).strftime("%Y-%m-%d"),
                        "kind": "question", "replies": it.get("answer_count"),
                        "answered": bool(it.get("is_answered"))})
        time.sleep(0.5)
    return out


def from_mastodon(tags, since):
    out = []
    for tag in tags:
        raw = http(f"https://infosec.exchange/tags/{urllib.parse.quote(tag)}.rss")
        if not raw:
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        for item in root.iter("item"):
            link = item.findtext("link") or ""
            try:
                dt = parsedate_to_datetime(item.findtext("pubDate"))
            except (TypeError, ValueError):
                dt = None
            if dt and dt < since:
                continue
            out.append({"id": cid("masto", link), "source": "Mastodon", "room": "infosec.exchange",
                        "title": "", "text": clean(item.findtext("description")), "url": link,
                        "date": dt.strftime("%Y-%m-%d") if dt else "", "kind": "post", "replies": None})
        time.sleep(0.3)
    return out


def from_news(queries, since):
    out = []
    days = max(1, (NOW - since).days)
    for q in queries:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{q} when:{days}d")
               + "&hl=en-US&gl=US&ceid=US:en")
        raw = http(url)
        if not raw:
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        for item in root.iter("item"):
            title = clean(item.findtext("title"))
            src = item.find("source")
            outlet = clean(src.text) if src is not None else "News"
            if title.endswith(" - " + outlet):
                title = title[: -len(outlet) - 3]
            try:
                dt = parsedate_to_datetime(item.findtext("pubDate"))
            except (TypeError, ValueError):
                dt = None
            out.append({"id": cid("news", title.lower()), "source": "Press", "room": outlet,
                        "title": title, "text": "", "url": item.findtext("link") or "",
                        "date": dt.strftime("%Y-%m-%d") if dt else "", "kind": "article", "replies": None})
        time.sleep(0.5)
    return out


def reddit_token():
    cid_, sec = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid_ and sec):
        return None
    auth = base64.b64encode(f"{cid_}:{sec}".encode()).decode()
    raw = http("https://www.reddit.com/api/v1/access_token",
               headers={"Authorization": f"Basic {auth}"}, data=b"grant_type=client_credentials")
    try:
        return json.loads(raw)["access_token"] if raw else None
    except (ValueError, KeyError):
        return None


def from_reddit(pairs, since):
    token = reddit_token()
    if not token:
        print("Reddit: no keys set, skipping")
        return []
    out, hdr = [], {"Authorization": f"bearer {token}"}
    for sub, q in pairs:
        url = (f"https://oauth.reddit.com/r/{sub}/search?restrict_sr=1&sort=new&t=month&limit=50&q="
               + urllib.parse.quote(q))
        raw = http(url, headers=hdr)
        if not raw:
            continue
        for ch in json.loads(raw).get("data", {}).get("children", []):
            d = ch.get("data", {})
            dt = datetime.fromtimestamp(d.get("created_utc", 0), timezone.utc)
            if dt < since:
                continue
            out.append({"id": cid("reddit", d.get("id")), "source": "Reddit", "room": f"r/{sub}",
                        "title": clean(d.get("title")), "text": clean(d.get("selftext")),
                        "url": "https://www.reddit.com" + d.get("permalink", ""),
                        "date": dt.strftime("%Y-%m-%d"), "kind": "thread",
                        "replies": d.get("num_comments")})
        time.sleep(1.1)
    return out


def collect(lenses, since):
    """One pass over all sources, using the union of every lens's searches."""
    def union(key):
        seen, out = set(), []
        for L in lenses.values():
            for q in L.get("sources", {}).get(key, []):
                if q.lower() not in seen:
                    seen.add(q.lower())
                    out.append(q)
        return out
    pairs = []
    for L in lenses.values():
        s = L.get("sources", {})
        pairs += [(sub, q) for sub in s.get("subreddits", []) for q in s.get("reddit_queries", [])]
    pairs = list(dict.fromkeys(pairs))
    raw = []
    raw += from_hn(union("hn_queries"), since)
    raw += from_stackexchange(union("stackexchange_queries"), since)
    raw += from_mastodon(union("mastodon_tags"), since)
    raw += from_news(union("news_queries"), since)
    raw += from_reddit(pairs, since)
    return raw


# ---------------- Rooms mentioned in conversations ----------------
INVITE = [
    (re.compile(r"discord\.(?:gg|com/invite)/([a-z0-9-]+)", re.I), "Discord", "https://discord.gg/{}"),
    (re.compile(r"join\.slack\.com/t/([a-z0-9-]+)", re.I), "Slack", "https://join.slack.com/t/{}"),
    (re.compile(r"([a-z0-9-]+)\.slack\.com", re.I), "Slack", "https://{}.slack.com"),
    (re.compile(r"(?<![a-z0-9])/?r/([a-z0-9_]{3,21})\b", re.I), "Reddit", "https://www.reddit.com/r/{}"),
]
NAMED = re.compile(r"([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,4})\s+((?i:slack|discord))(?:\s+(?i:group|community|server|workspace|channel))?")
NOT_NAMES = {"Our", "The", "A", "An", "My", "This", "That", "Company", "Work", "Internal", "Private", "Join",
             "On", "In", "Your", "Also", "And", "But", "Check", "Try", "Via", "See", "Use", "We", "I",
             "So", "Then", "Plus", "Or", "Their", "His", "Her", "Its", "Any", "Some", "Every", "Each",
             "Official", "Free", "Public", "New", "Great", "Good", "Best", "Popular", "Local", "Same"}


def room_key(name):
    base = re.sub(r"\((?:slack|discord|reddit)\)|\b(?:slack|discord)\b", "", name.lower())
    return re.sub(r"[^a-z0-9]", "", base)


def find_rooms(conv, directory):
    raw = f" {conv['title']} {conv['text']} "
    low = raw.lower()
    found = {}

    def add(name, known, platform="", url=""):
        k = room_key(name)
        if not k:
            return
        cur = found.get(k)
        if cur is None or (known and not cur["known"]) or (not cur["known"] and not known and len(name.split()) > len(cur["name"].split())):
            found[k] = {"name": name, "known": known, "platform": platform or (cur or {}).get("platform", ""),
                        "url": url or (cur or {}).get("url", "")}
        elif url and not cur.get("url"):
            cur["url"] = url

    for r in directory:
        if any(hit(low, m) for m in r.get("match", [])):
            add(r["name"], True, r.get("platform", ""), r.get("url", ""))
    for rx, platform, tmpl in INVITE:
        for m in rx.finditer(raw):
            slug = m.group(1)
            if platform == "Slack" and slug.lower() in ("app", "www", "api", "join", "status", "hooks"):
                continue
            name = f"r/{slug}" if platform == "Reddit" else f"{slug} {platform}"
            known = next((r for r in directory if room_key(r["name"]) == room_key(name)), None)
            if known:
                add(known["name"], True, known.get("platform", ""), known.get("url", ""))
            else:
                add(name, False, platform, tmpl.format(slug))
    for m in NAMED.finditer(raw):
        words = m.group(1).split()
        while words and words[0] in NOT_NAMES:
            words = words[1:]
        if not words:
            continue
        plat = m.group(2).capitalize()
        name = " ".join(words) + " " + plat
        known = next((r for r in directory if room_key(r["name"]) == room_key(name)), None)
        if known:
            add(known["name"], True, known.get("platform", ""), known.get("url", ""))
        else:
            add(name, False, plat)
    return list(found.values())


# ---------------- Lens tagging and weigh-in scoring ----------------
QUESTION = re.compile(r"\?|^(how|what|which|why|is|are|does|do|can|should|has|have|anyone)\b|\b(any advice|any tips|help me|need help|looking for)\b", re.I)


def read_for_lens(c, L, cfg):
    """Tag a conversation for one lens. Returns None when it's off-topic for that lens."""
    full = f" {c['title']} {c['text']} ".lower()
    if any(x in full for x in cfg["exclude"]["phrases"]):
        return None
    if not any(hit(full, p) for p in L["relevance"]["phrases"]):
        return None
    topics = [k for k, t in L["topics"].items() if any(hit(full, p) for p in t["phrases"])]
    personas = [k for k, b in L["buyers"].items() if any(hit(full, p) for p in b["phrases"])]
    if not topics and not personas:
        return None
    mentions = any(hit(full, a) for a in L["company"].get("aliases", []))
    asking = bool(QUESTION.search(c["title"] or "")) or "?" in c["text"][:600] or bool(QUESTION.search(c["text"][:160]))
    shopping = any(hit(full, p) for p in cfg["intent"]["phrases"])
    return {"topics": topics, "personas": personas, "mentions": mentions,
            "asking": asking, "shopping": shopping}


def weigh_in(c, r, L, cfg):
    w = cfg["weigh_in"]
    why, pts = [], 0
    fits = [L["topics"][t]["fit"] for t in r["topics"]]
    best = max(fits) if fits else 0
    if best:
        top = max(r["topics"], key=lambda t: L["topics"][t]["fit"])
        add = {3: w["topic_core"], 2: w["topic_adjacent"], 1: w["topic_listen"]}[best]
        pts += add
        why.append(f"+{add} {'core' if best == 3 else 'adjacent' if best == 2 else 'listen-only'} topic: {L['topics'][top]['label']}")
    bw = max([L["buyers"][p]["weight"] for p in r["personas"]], default=0)
    if bw:
        pts += bw
        why.append(f"+{bw} buyer: {L['buyers'][max(r['personas'], key=lambda p: L['buyers'][p]['weight'])]['label']}")
    if r["asking"]:
        pts += w["asking"]
        why.append(f"+{w['asking']} asking for help")
    if r["shopping"]:
        pts += w["shopping"]
        why.append(f"+{w['shopping']} shopping for tools")
    rep = c.get("replies")
    if rep is not None and c["kind"] in ("thread", "question"):
        if rep == 0:
            pts += w["unanswered"]
            why.append(f"+{w['unanswered']} nobody has answered")
        elif rep <= 3:
            pts += w["thin"]
            why.append(f"+{w['thin']} only {rep} repl{'y' if rep == 1 else 'ies'}")
        elif rep >= 15:
            pts += w["crowded"]
            why.append(f"{w['crowded']} crowded thread ({rep} replies)")
    a = age(c)
    if a <= 2:
        pts += w["fresh_2_days"]
        why.append(f"+{w['fresh_2_days']} posted in the last 2 days")
    elif a <= 7:
        pts += w["fresh_7_days"]
        why.append(f"+{w['fresh_7_days']} posted this week")
    if r["mentions"]:
        pts += w["mentions_company"]
        why.append(f"+{w['mentions_company']} mentions {L['company']['name']}")
    replyable = (c["source"] != "Press" and a <= cfg.get("reply_window_days", 14) and best >= 2)
    if r["mentions"] and c["source"] != "Press" and a <= cfg.get("reply_window_days", 14):
        replyable = True
    band = ("today" if replyable and pts >= w["today"] else
            "worth" if replyable and pts >= w["worth"] else "listen")
    top = max(r["topics"], key=lambda t: L["topics"][t]["fit"]) if r["topics"] else None
    return {**r, "score": pts, "why": why, "band": band,
            "who": (L["topics"][top].get("who", "") if top and best >= 2 else
                    L["company"].get("brand_owner", "Founder or account executive") if r["mentions"] else ""),
            "angle": (L["topics"][top].get("angle", "") if top and best >= 2 else
                      "They're asking about you by name. Answer plainly, say you work there, and offer specifics." if r["mentions"] else ""),
            "topic": top}


# ---------------- Roll-ups ----------------
def topics_to_own(convs, key, L, window):
    out = []
    for k, t in L["topics"].items():
        mine = [c for c in convs if key in c["lens"] and k in c["lens"][key]["topics"] and age(c) <= window]
        talk = [c for c in mine if c["source"] != "Press"]
        recent = sum(1 for c in mine if age(c) <= 14)
        prior = sum(1 for c in mine if 14 < age(c) <= 28)
        open_q = [c for c in talk if c["lens"][key]["asking"] and c.get("replies") is not None and c["replies"] <= 3]
        weekly = [0] * 12
        for c in mine:
            wk = age(c) // 7
            if 0 <= wk < 12:
                weekly[11 - wk] += 1
        rooms = Counter(c["room"] for c in talk)
        change = round((recent - prior) / prior * 100) if prior else (100 if recent else 0)
        momentum = 1 + max(0, min(change, 200)) / 100
        score = round(t["fit"] * math.log2(1 + len(mine)) * momentum + len(open_q) * 0.5 * t["fit"], 1)
        out.append({"key": k, "label": t["label"], "fit": t["fit"], "angle": t.get("angle", ""),
                    "who": t.get("who", ""), "total": len(mine), "recent": recent, "prior": prior,
                    "change": change, "weekly": weekly, "open": len(open_q),
                    "open_examples": [{"title": c["title"] or c["text"][:90], "url": c["url"], "room": c["room"],
                                       "date": c["date"], "replies": c.get("replies")}
                                      for c in sorted(open_q, key=age)[:3]],
                    "rooms": [r for r, _ in rooms.most_common(3)], "score": score})
    out.sort(key=lambda x: -x["score"])
    return out


def rank_rooms(convs, key, L, directory, window):
    weights = L.get("rooms", {})
    evidence, buyers, hosted, meta = defaultdict(list), Counter(), Counter(), {}
    for c in convs:
        if key not in c["lens"] or age(c) > window:
            continue
        hosted[c["room"]] += 1
        for r in c.get("rooms", []):
            evidence[r["name"]].append(c)
            if c["lens"][key]["personas"]:
                buyers[r["name"]] += 1
            if not r["known"]:
                meta.setdefault(r["name"], r)
    out = []
    for r in directory + [dict(v, audiences=[], access="unknown") for v in meta.values()]:
        name = r["name"]
        fit = max([weights.get(a, 0) for a in r.get("audiences", [])], default=0)
        known = name not in meta
        if known and fit == 0:
            continue
        mentions = len(evidence[name])
        if not known and mentions < 2:
            continue
        fit_pts = fit * 2
        talk_pts = round(math.log2(1 + mentions + hosted[name]) * 2, 1)
        buyer_pts = round(math.log2(1 + buyers[name]) * 2, 1)
        access_pts = 1 if r.get("access") in ("open", "event") else 0
        why = []
        if fit:
            why.append(f"+{fit_pts} audience fit ({fit} of 3)")
        if mentions or hosted[name]:
            parts = []
            if hosted[name]:
                parts.append(f"{hosted[name]} relevant conversations there")
            if mentions:
                parts.append(f"mentioned in {mentions}")
            why.append(f"+{talk_pts} " + ", ".join(parts))
        if buyers[name]:
            why.append(f"+{buyer_pts} buyers point to it")
        if access_pts:
            why.append("+1 easy to join")
        out.append({"name": name, "known": known, "platform": r.get("platform", ""),
                    "audience": r.get("audience", "Discovered in buyer conversations"),
                    "access": r.get("access", "unknown"),
                    "who": r.get("who", "Find out who runs it and whether vendors are welcome before joining."),
                    "norms": r.get("norms", ""), "url": r.get("url", ""),
                    "score": round(fit_pts + talk_pts + buyer_pts + access_pts, 1), "why": why,
                    "hosted": hosted[name], "mentions": mentions,
                    "evidence": [{"title": c["title"] or c["text"][:90], "url": c["url"], "date": c["date"]}
                                 for c in evidence[name][:5]]})
    out.sort(key=lambda x: -x["score"])
    return out


def buyer_phrases(convs, key, window=45, top=30):
    grams, seen_in = Counter(), defaultdict(set)
    for c in convs:
        if key not in c["lens"] or age(c) > window or c["source"] == "Press":
            continue
        words = re.findall(r"[a-z][a-z0-9'\-]+", f"{c['title']} {c['text']}".lower())
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                g = words[i:i + n]
                if g[0] in STOP or g[-1] in STOP or any(len(x) < 3 for x in g):
                    continue
                p = " ".join(g)
                if c["id"] not in seen_in[p]:
                    seen_in[p].add(c["id"])
                    grams[p] += 1
    out = []
    for p, n in grams.most_common(top * 3):
        if n < 2:
            break
        if any(p in o["phrase"] or o["phrase"] in p for o in out):
            continue
        out.append({"phrase": p, "count": n})
        if len(out) >= top:
            break
    return out


def quotes_for(convs, key, L, per_topic=2):
    q = defaultdict(list)
    for c in sorted(convs, key=age):
        if key not in c["lens"] or c["source"] == "Press":
            continue
        for sent in re.split(r"(?<=[.!?])\s+", c["text"]):
            if not 6 <= len(sent.split()) <= 30:
                continue
            low = f" {sent.lower()} "
            for k in c["lens"][key]["topics"]:
                if len(q[k]) < per_topic and any(hit(low, p) for p in L["topics"][k]["phrases"]):
                    q[k].append({"text": sent.strip(), "url": c["url"], "room": c["room"], "date": c["date"]})
                    break
    return q


# ---------------- AI (GitHub Models, free tier) ----------------
def ai(prompt, token, max_tokens=700):
    for model in MODELS:
        body = json.dumps({"model": model, "temperature": 0.3, "max_tokens": max_tokens,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(MODEL_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            print(f"  AI call failed on {model}: HTTP {e.code} {e.read()[:300].decode('utf-8', 'replace')}")
            if e.code == 429:
                return None
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as e:
            print(f"  AI call failed on {model}: {e}")
    return None


STYLE = ("Plain text. No markdown, no asterisks, no bullets, no em dashes. Sound like a sharp, "
         "helpful practitioner. Never invent facts, numbers, customers or features.")


def reply_prompt(c, r, L, room):
    co = L["company"]["name"]
    return (f"A buyer posted this in {c['room']}:\nTitle: {c['title']}\nText: {c['text'][:900]}\n\n"
            f"You are the {r['who'] or 'subject matter expert'} at {co}, a {L['company']['category'].lower()} company. "
            f"The point of view {co} brings to this topic: {r['angle']}\n"
            f"Room norms: {room.get('norms', 'Answer as a practitioner and disclose where you work.')}\n\n"
            "Write two labeled lines.\nRead: one sentence on what this person actually needs.\n"
            "Reply: under 90 words. Answer their question first with something genuinely useful. Bring the "
            f"point of view only where it helps. If you mention {co}, say you work there. Never pitch. " + STYLE)


def emerging_prompt(items, L):
    lines = "\n".join(f"{c['id']}: {c['title'] or c['text'][:140]}" for c in items)
    known = ", ".join(t["label"] for t in L["topics"].values())
    return (f"These are recent posts from {L['company']['category'].lower()} buyers that don't fit any of these "
            f"known topics: {known}.\n{lines}\n\nGroup them into up to 5 emerging buyer topics. Only make a group "
            "if at least 2 posts share a real problem. Return JSON only, no prose, in this shape: "
            '[{"label": "short buyer problem in plain words", "why": "one sentence", "ids": ["id1", "id2"]}]')


def brief_prompt(key, L, convs, topics, emerging):
    co = L["company"]["name"]
    queue = [c for c in convs if key in c["lens"] and c["lens"][key]["band"] != "listen"][:12]
    q = "\n".join(f"- [{c['room']}] {c['title'] or c['text'][:120]}" for c in queue)
    t = "\n".join(f"- {x['label']}: {x['recent']} mentions in 14 days (was {x['prior']}), {x['open']} open questions, fit {x['fit']} of 3" for x in topics[:6])
    e = "\n".join(f"- {x['label']}" for x in emerging) or "none yet"
    return (f"You advise the CEO and CMO of {co} ({L['company']['category'].lower()}). This is what their buyers "
            f"are discussing in public.\nConversations worth joining:\n{q or 'none this week'}\n\nTopics:\n{t}\n\n"
            f"Emerging topics nobody has named yet:\n{e}\n\nWrite a weekly brief with these labeled sections, each two "
            "or three sentences: Where buyers are this week. Conversations to join. Topics to own. Three moves "
            "(numbered 1 to 3: one for content, one for the field team, one for community). Talk about buyers, not "
            "competitors. Say plainly if the sample is thin. " + STYLE)


# ---------------- Main ----------------
def main():
    dry = "--dry-run" in sys.argv
    cfg = load_toml("config.toml")
    lenses = {k: load_toml(f"lenses/{k}.toml") for k in cfg["lenses"]}
    directory = load_toml("communities.toml").get("rooms", [])
    rooms_by_name = {r["name"]: r for r in directory}
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = {}
    convs = data.get("conversations", [])
    known = {c["id"] for c in convs}
    since = NOW - timedelta(days=cfg.get("first_run_days", 30) if not convs else 3)

    raw = collect(lenses, since) if (not dry or os.environ.get("HALLWAY_COLLECT")) else []
    new, by_source = [], Counter()
    for c in raw:
        if c["id"] in known:
            continue
        known.add(c["id"])
        c["text"] = c["text"][:1200]
        c["found"] = NOW.strftime("%Y-%m-%d")
        new.append(c)

    keep = cfg.get("window_days", 90) + 30
    pool = [c for c in convs + new if age(c) <= keep or not c.get("date")]
    kept, new_ids = [], {c["id"] for c in new}
    for c in pool:
        c["lens"] = {}
        for k, L in lenses.items():
            r = read_for_lens(c, L, cfg)
            if r:
                c["lens"][k] = weigh_in(c, r, L, cfg)
        if c["lens"]:
            c["rooms"] = find_rooms(c, directory)
            kept.append(c)
            if c["id"] in new_ids:
                by_source[c["source"]] += 1
    kept.sort(key=lambda c: c.get("date") or "", reverse=True)
    print("New on-topic conversations:", dict(by_source) or 0)

    token = os.environ.get("GITHUB_TOKEN")
    budget = cfg.get("max_ai_calls", 24) if (token and not dry) else 0
    prev = data.get("lenses", {})
    win = cfg.get("window_days", 90)
    out_lenses = {}
    for k, L in lenses.items():
        topics = topics_to_own(kept, k, L, win)
        emerging = prev.get(k, {}).get("emerging", [])
        brief = prev.get(k, {}).get("brief", {})
        if budget:
            # Name emerging topics from conversations no known topic covers (weekly).
            last = day(prev.get(k, {}).get("emerging_date", ""))
            if not last or (NOW - last).days >= 6:
                loose = [c for c in kept if k in c["lens"] and not c["lens"][k]["topics"]
                         and c["source"] != "Press" and age(c) <= 30][:60]
                if len(loose) >= 4:
                    txt = ai(emerging_prompt(loose, L), token, 900)
                    budget -= 1
                    try:
                        groups = json.loads(re.search(r"\[.*\]", txt or "", re.S).group(0))
                        by_id = {c["id"]: c for c in loose}
                        emerging = [{"label": g["label"], "why": g.get("why", ""),
                                     "examples": [{"title": by_id[i]["title"] or by_id[i]["text"][:90],
                                                   "url": by_id[i]["url"], "room": by_id[i]["room"]}
                                                  for i in g.get("ids", []) if i in by_id][:4]}
                                    for g in groups if len([i for i in g.get("ids", []) if i in by_id]) >= 2]
                    except (AttributeError, ValueError, KeyError, TypeError):
                        print("  couldn't read emerging topics")
                    prev.setdefault(k, {})["emerging_date"] = NOW.strftime("%Y-%m-%d")
            # Suggested replies for the top of the weigh-in queue.
            queue = sorted([c for c in kept if k in c["lens"] and c["lens"][k]["band"] != "listen"
                            and not c["lens"][k].get("reply")], key=lambda c: -c["lens"][k]["score"])
            old = {c["id"]: c for c in convs}
            for c in queue:
                prior = old.get(c["id"], {}).get("lens", {}).get(k, {}).get("reply")
                if prior:
                    c["lens"][k]["reply"] = prior
                    continue
                if budget <= 2:
                    break
                txt = ai(reply_prompt(c, c["lens"][k], L, rooms_by_name.get(c["room"], {})), token, 300)
                budget -= 1
                if txt:
                    c["lens"][k]["reply"] = txt
                time.sleep(2)
            last = day(brief.get("date", ""))
            if (not last or (NOW - last).days >= 6) and budget > 0:
                txt = ai(brief_prompt(k, L, kept, topics, emerging), token)
                budget -= 1
                if txt:
                    brief = {"date": NOW.strftime("%Y-%m-%d"), "text": txt}
        else:
            old = {c["id"]: c for c in convs}
            for c in kept:
                prior = old.get(c["id"], {}).get("lens", {}).get(k, {}).get("reply")
                if k in c["lens"] and prior:
                    c["lens"][k]["reply"] = prior
        out_lenses[k] = {
            "company": L["company"], "buyers": {b: v["label"] for b, v in L["buyers"].items()},
            "topics_meta": {t: {"label": v["label"], "fit": v["fit"]} for t, v in L["topics"].items()},
            "topics": topics, "emerging": emerging,
            "emerging_date": prev.get(k, {}).get("emerging_date", ""),
            "rooms": rank_rooms(kept, k, L, directory, win),
            "phrases": buyer_phrases(kept, k), "quotes": quotes_for(kept, k, L), "brief": brief,
            "count": sum(1 for c in kept if k in c["lens"]),
        }
    print(f"AI calls used: {cfg.get('max_ai_calls', 24) - budget if (token and not dry) else 0}")

    out = {"generated": NOW.strftime("%Y-%m-%dT%H:%MZ"), "default_lens": cfg.get("default_lens", cfg["lenses"][0]),
           "window_days": win, "reply_window_days": cfg.get("reply_window_days", 14),
           "last_run": {"new": sum(by_source.values()), "by_source": dict(by_source)},
           "rooms_norms": {r["name"]: r.get("norms", "") for r in directory if r.get("norms")},
           "lenses": out_lenses, "conversations": kept}
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(f"Saved {len(kept)} conversations across {len(lenses)} lenses.")


if __name__ == "__main__":
    main()
