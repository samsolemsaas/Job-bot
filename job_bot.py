#!/usr/bin/env python3
"""Samantha's job bot.

Checks company job boards on Greenhouse, Lever and Ashby through their public
job-board APIs, keeps marketing and GTM roles that are fully remote in the US or
in the Seattle metro, and emails a digest of roles it has not seen before.

Run locally without email:  python job_bot.py --dry-run
"""
import csv
import json
import os
import re
import smtplib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape, unescape

ROOT = os.path.dirname(os.path.abspath(__file__))
COMPANIES_FILE = os.path.join(ROOT, "companies.txt")
STATE_DIR = os.path.join(ROOT, "state")
SEEN_FILE = os.path.join(STATE_DIR, "seen.json")
BOARDS_FILE = os.path.join(STATE_DIR, "boards.json")
LOG_FILE = os.path.join(STATE_DIR, "jobs_log.csv")
NOT_FOUND_FILE = os.path.join(STATE_DIR, "not_found.txt")

RECHECK_MISSING_DAYS = 7

# ---------- Role filter: edit these lists to widen or narrow matches ----------
TITLE_INCLUDE = [
    "demand gen", "demand generation", "growth market", "growth manager",
    "growth lead", "head of growth", "director of growth", "growth director",
    "campaign", "integrated marketing", "product marketing", "field marketing",
    "lifecycle", "abm", "account-based", "account based", "marketing operations",
    "marketing ops", "revenue operations", "revops", "gtm engineer",
    "go-to-market engineer", "gtm operations", "gtm strategy", "partner marketing",
    "channel marketing", "content marketing", "pipeline marketing",
    "performance marketing", "digital marketing", "marketing manager",
    "marketing director", "director of marketing", "director, marketing",
    "head of marketing", "vp marketing", "vp, marketing", "vice president, marketing",
    "vice president of marketing",
]
TITLE_EXCLUDE = [
    "intern", "apprentice", "coordinator", "sales development",
    "business development representative", "account executive", "recruit",
]

# ---------- Location filter ----------
SEATTLE_METRO = ["seattle", "bellevue", "kirkland", "redmond"]
US_PATTERN = re.compile(
    r"\b(us|usa|u\.s\.?|united states|north america|americas|us-remote|remote-us)\b")
NON_US = [
    "canada", "toronto", "vancouver", "united kingdom", "uk", "london", "emea",
    "europe", "eu", "germany", "berlin", "france", "paris", "ireland", "dublin",
    "netherlands", "amsterdam", "spain", "portugal", "poland", "sweden", "denmark",
    "israel", "tel aviv", "india", "bangalore", "bengaluru", "australia", "sydney",
    "singapore", "japan", "tokyo", "apac", "latam", "brazil", "mexico",
    "argentina", "colombia", "philippines",
]
IN_OFFICE_WORDS = ["hybrid", "on-site", "onsite", "in office", "in-office"]


def title_matches(title):
    t = title.lower()
    if any(word in t for word in TITLE_EXCLUDE):
        return False
    return any(word in t for word in TITLE_INCLUDE)


def classify_location(location_text, remote_flag=False, workplace=""):
    """Return a display label if the location passes, else None."""
    t = (location_text or "").lower()
    w = (workplace or "").lower()
    if any(city in t for city in SEATTLE_METRO):
        if "remote" in w or remote_flag:
            return "Remote or Seattle metro"
        if "hybrid" in w or "hybrid" in t:
            return "Seattle metro (hybrid)"
        return "Seattle metro"
    remote = remote_flag or "remote" in t or "remote" in w or "anywhere" in t
    country_only = re.sub(r"[^a-z ]", "", t).strip() in {
        "united states", "usa", "us", "united states of america", "north america"}
    if not remote and country_only and not any(x in w for x in ("hybrid", "onsite", "on-site")):
        return "Remote (US, country-level listing)"
    if not remote:
        return None
    if "hybrid" in w or "onsite" in w or "on-site" in w:
        return None
    if any(word in t for word in IN_OFFICE_WORDS) and "remote" not in t:
        return None
    mentions_us = bool(US_PATTERN.search(t))
    mentions_non_us = any(re.search(r"\b" + re.escape(n) + r"\b", t) for n in NON_US)
    if mentions_non_us and not mentions_us:
        return None
    if mentions_us:
        return "Remote (US)"
    return "Remote (confirm US eligibility)"


# ---------- Compensation ----------
COMP_PATTERN = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kK]?\s*(?:-|–|—|to)\s*\$?\s?\d[\d,]*(?:\.\d+)?\s?[kK]?")


def comp_from_text(text):
    if not text:
        return ""
    plain = re.sub(r"<[^>]+>", " ", unescape(unescape(text)))
    for match in COMP_PATTERN.finditer(plain):
        digits = re.sub(r"[^\d]", "", match.group(0).split("-")[0].split("to")[0])
        if len(digits) >= 5 or "k" in match.group(0).lower():
            return " ".join(match.group(0).split())
    return ""


# ---------- Dates ----------
def parse_date(value):
    """Return a UTC datetime from an ISO string or millisecond timestamp, else None."""
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def posted_label(dt):
    if not dt:
        return "Not listed"
    days = (datetime.now(timezone.utc) - dt).days
    ago = "today" if days <= 0 else "1 day ago" if days == 1 else f"{days} days ago"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year} ({ago})"


# ---------- HTTP ----------
def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "job-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return None


# ---------- Board adapters ----------
def fetch_greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not data or "jobs" not in data:
        return None
    jobs = []
    for j in data["jobs"]:
        offices = ", ".join(o.get("name", "") for o in j.get("offices", []) or [])
        loc = (j.get("location") or {}).get("name", "")
        jobs.append({
            "id": f"gh-{slug}-{j['id']}",
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": ", ".join(x for x in [loc, offices] if x),
            "remote": False,
            "workplace": "",
            "comp": comp_from_text(j.get("content", "")),
            "posted": parse_date(j.get("first_published") or j.get("updated_at")),
        })
    return jobs


def fetch_lever(slug):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return None
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        comp = ""
        sr = j.get("salaryRange")
        if sr and sr.get("min") and sr.get("max"):
            comp = f"${sr['min']:,.0f} to ${sr['max']:,.0f} {sr.get('currency', '')}".strip()
        if not comp:
            comp = comp_from_text((j.get("additionalPlain") or "") + " " +
                                  (j.get("descriptionPlain") or ""))
        jobs.append({
            "id": f"lv-{slug}-{j['id']}",
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "location": ", ".join(l for l in locs if l),
            "remote": j.get("workplaceType") == "remote",
            "workplace": j.get("workplaceType", ""),
            "comp": comp,
            "posted": parse_date(j.get("createdAt")),
        })
    return jobs


def fetch_ashby(slug):
    data = get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    if not data or "jobs" not in data:
        return None
    jobs = []
    for j in data["jobs"]:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [
            s.get("location", "") for s in j.get("secondaryLocations", []) or []]
        comp_obj = j.get("compensation") or {}
        comp = (comp_obj.get("scrapeableCompensationSalarySummary")
                or comp_obj.get("compensationTierSummary") or "")
        if not comp:
            comp = comp_from_text(j.get("descriptionPlain", ""))
        jobs.append({
            "id": f"ab-{slug}-{j['id']}",
            "title": j.get("title", ""),
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "location": ", ".join(l for l in locs if l),
            "remote": bool(j.get("isRemote")),
            "workplace": j.get("workplaceType", ""),
            "comp": comp,
            "posted": parse_date(j.get("publishedAt")),
        })
    return jobs


ADAPTERS = {"greenhouse": fetch_greenhouse, "ashby": fetch_ashby, "lever": fetch_lever}


# ---------- Companies and board detection ----------
def load_companies():
    companies = []
    with open(COMPANIES_FILE, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, _, override = line.partition("|")
            name, override = name.strip(), override.strip()
            forced = None
            if override and ":" in override:
                ats, slug = override.split(":", 1)
                forced = (ats.strip().lower(), slug.strip())
            companies.append((name, forced))
    return companies


def slug_candidates(name):
    base = name.lower().replace("&", "and")
    base = re.sub(r"\(.*?\)", "", base).strip()
    words = re.findall(r"[a-z0-9]+", base)
    trimmed = [w for w in words if w not in
               {"ai", "io", "inc", "labs", "security", "technologies", "networks", "software"}]
    cands = ["".join(words), "-".join(words)]
    if trimmed and trimmed != words:
        cands += ["".join(trimmed), "-".join(trimmed)]
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def greenhouse_name_ok(slug, company):
    meta = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    if not meta or not meta.get("name"):
        return True
    board = re.sub(r"[^a-z0-9]", "", meta["name"].lower())
    first = re.sub(r"[^a-z0-9]", "", company.lower().split()[0])
    return first in board or board in re.sub(r"[^a-z0-9]", "", company.lower())


def detect_board(name):
    for slug in slug_candidates(name):
        for ats in ("greenhouse", "ashby", "lever"):
            jobs = ADAPTERS[ats](slug)
            time.sleep(0.15)
            if jobs is None:
                continue
            if ats == "greenhouse" and not greenhouse_name_ok(slug, name):
                continue
            return ats, slug, jobs
    return None


# ---------- State ----------
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)


# ---------- Email ----------
def build_email(matches, first_run, scanned, missing, resend=False):
    today = datetime.now().strftime("%b %d")
    label = "first run" if first_run else "full resend" if resend else today
    kind = "current" if resend else "new"
    subject = f"Job bot: {len(matches)} {kind} role{'s' if len(matches) != 1 else ''} ({label})"
    text_parts, html_parts = [], []
    for m in matches:
        comp = m["comp"] or "Not listed"
        posted = posted_label(m.get("posted"))
        text_parts.append(
            f"{m['title']} at {m['company']}\nPosted: {posted}\nComp: {comp}\n"
            f"Location: {m['label']} ({m['location']})\nApply: {m['url']}\n")
        html_parts.append(
            f"<p><b>{escape(m['title'])}</b> at {escape(m['company'])}<br>"
            f"Posted: {escape(posted)}<br>"
            f"Comp: {escape(comp)}<br>"
            f"Location: {escape(m['label'])} <span style='color:#777'>"
            f"({escape(m['location'])})</span><br>"
            f"<a href='{escape(m['url'])}'>View listing</a></p>")
    footer = (f"Scanned {scanned} company boards. "
              f"{missing} companies not on Greenhouse, Lever or Ashby (see state/not_found.txt).")
    text = "\n".join(text_parts) + "\n" + footer
    html = "".join(html_parts) + f"<hr><p style='color:#777'>{escape(footer)}</p>"
    return subject, text, html


def send_email(subject, text, html):
    sender = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("DIGEST_TO") or sender
    if not sender or not password:
        if os.environ.get("GITHUB_ACTIONS"):
            sys.exit("GMAIL_ADDRESS or GMAIL_APP_PASSWORD secret is missing. Add them in repo Settings.")
        print("Email secrets missing; printing digest instead.\n")
        print(subject + "\n\n" + text)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, password)
        s.sendmail(sender, [to], msg.as_string())
    print(f"Sent: {subject}")


def upgrade_log_header():
    """Add the date_posted column to logs written by the first version."""
    if not os.path.exists(LOG_FILE):
        return
    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows or "date_posted" in rows[0]:
        return
    rows = [[r[0], "date_posted" if i == 0 else ""] + r[1:] for i, r in enumerate(rows) if r]
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


# ---------- Main ----------
def main():
    dry_run = "--dry-run" in sys.argv
    resend = os.environ.get("RESEND_ALL", "").lower() == "true" or "--resend-all" in sys.argv
    os.makedirs(STATE_DIR, exist_ok=True)
    seen = load_json(SEEN_FILE, {})
    boards = load_json(BOARDS_FILE, {})
    first_run = not seen
    now = time.time()
    matches, missing, scanned = [], [], 0

    for name, forced in load_companies():
        info = boards.get(name)
        jobs = None
        if forced:
            ats, slug = forced
            jobs = ADAPTERS.get(ats, lambda s: None)(slug)
            if jobs is not None:
                boards[name] = {"ats": ats, "slug": slug}
        elif info and info.get("ats"):
            jobs = ADAPTERS[info["ats"]](info["slug"])
        elif info and info.get("missing") and now - info["checked"] < RECHECK_MISSING_DAYS * 86400:
            missing.append(name)
            continue
        if jobs is None and not forced:
            found = detect_board(name)
            if found:
                ats, slug, jobs = found
                boards[name] = {"ats": ats, "slug": slug}
            else:
                boards[name] = {"missing": True, "checked": now}
        if jobs is None:
            missing.append(name)
            continue
        scanned += 1
        for j in jobs:
            if (j["id"] in seen and not resend) or not title_matches(j["title"]):
                continue
            label = classify_location(j["location"], j["remote"], j["workplace"])
            if not label:
                continue
            matches.append({**j, "company": name, "label": label})
        time.sleep(0.1)

    found_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    oldest = datetime(1970, 1, 1, tzinfo=timezone.utc)
    matches.sort(key=lambda m: m.get("posted") or oldest, reverse=True)
    new_ids = {m["id"] for m in matches if m["id"] not in seen}
    for m in matches:
        seen.setdefault(m["id"], found_date)

    if matches:
        subject, text, html = build_email(matches, first_run, scanned, len(missing), resend)
        if dry_run:
            print(subject + "\n\n" + text)
        else:
            send_email(subject, text, html)
        upgrade_log_header()
        new_log = not os.path.exists(LOG_FILE)
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_log:
                w.writerow(["date_found", "date_posted", "company", "title", "comp",
                            "location", "url"])
            for m in (m for m in matches if m["id"] in new_ids):
                posted = m["posted"].strftime("%Y-%m-%d") if m.get("posted") else ""
                w.writerow([found_date, posted, m["company"], m["title"], m["comp"],
                            f"{m['label']} ({m['location']})", m["url"]])
    else:
        print(f"No new matches. Scanned {scanned} boards; {len(missing)} not found.")

    if not dry_run:
        save_json(SEEN_FILE, seen)
    save_json(BOARDS_FILE, boards)
    with open(NOT_FOUND_FILE, "w", encoding="utf-8") as f:
        f.write("Companies not found on Greenhouse, Lever or Ashby.\n"
                "Fix with an override in companies.txt, e.g.  Company Name | ashby:slug\n\n")
        f.write("\n".join(sorted(missing)) + "\n")


if __name__ == "__main__":
    main()
