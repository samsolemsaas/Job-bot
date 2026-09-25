"""
Resolve ATS boards for companies listed in state/not_found.txt.

For each company it:
  1. Tries slug variations against Greenhouse, Lever, Ashby,
     Workable, SmartRecruiters and Recruitee public job APIs.
  2. If nothing matches, looks for a careers page on likely domains
     and reads which job platform it links to.

Writes state/ats_suggestions.txt with:
  - override lines ready to paste into companies.txt
  - companies on platforms the bot can't read yet, grouped by platform
  - companies still unresolved
"""

import re
import concurrent.futures as cf
from pathlib import Path

import requests

NOT_FOUND = Path("state/not_found.txt")
OUT = Path("state/ats_suggestions.txt")
TIMEOUT = 8
HEADERS = {"User-Agent": "Mozilla/5.0 (job-bot ats resolver)"}

SUPPORTED = {"greenhouse", "lever", "ashby"}

SUFFIXES = [" security", " ai", " labs", " inc", " systems", " identity", ".io", ".ai"]
ADD_ONS = ["", "hq", "inc", "labs", "security", "ai", "io"]


def read_companies():
    names = []
    for line in NOT_FOUND.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("Companies not found") or line.startswith("Fix with"):
            continue
        names.append(line)
    return names


MAX_SLUGS = 12


def slug_variants(name):
    base = name.lower().strip()
    stems = [base]
    for suf in SUFFIXES:
        if base.endswith(suf):
            stripped = base[: -len(suf)].strip()
            if stripped and stripped not in stems:
                stems.append(stripped)
    cores = []
    for stem in stems:
        for core in (re.sub(r"[^a-z0-9]", "", stem),
                     re.sub(r"[^a-z0-9]+", "-", stem).strip("-")):
            if core and core not in cores:
                cores.append(core)
    # Plain names first, then add-ons, capped to keep runs fast
    out = list(cores)
    for add in ADD_ONS[1:]:
        for core in cores:
            for s in (core + add, f"{core}-{add}"):
                if s not in out:
                    out.append(s)
    return out[:MAX_SLUGS]


def get(url):
    try:
        return requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None


def check_greenhouse(slug):
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return r is not None and r.status_code == 200 and "jobs" in r.text


def check_lever(slug):
    r = get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return r is not None and r.status_code == 200 and r.text.strip().startswith("[")


def check_ashby(slug):
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return r is not None and r.status_code == 200 and '"jobs"' in r.text


def check_workable(slug):
    r = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    return r is not None and r.status_code == 200 and '"jobs"' in r.text


def check_smartrecruiters(slug):
    r = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1")
    if r is None or r.status_code != 200:
        return False
    try:
        return r.json().get("totalFound", 0) > 0
    except ValueError:
        return False


def check_recruitee(slug):
    r = get(f"https://{slug}.recruitee.com/api/offers/")
    return r is not None and r.status_code == 200 and '"offers"' in r.text


API_CHECKS = [
    ("ashby", check_ashby),
    ("greenhouse", check_greenhouse),
    ("lever", check_lever),
    ("workable", check_workable),
    ("smartrecruiters", check_smartrecruiters),
    ("recruitee", check_recruitee),
]

# Patterns for job platforms linked from a careers page
LINK_PATTERNS = [
    ("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("recruitee", r"([A-Za-z0-9_-]+)\.recruitee\.com"),
    ("workday", r"([A-Za-z0-9_-]+\.wd\d+\.myworkdayjobs\.com/[A-Za-z0-9_/-]+)"),
    ("rippling", r"ats\.rippling\.com/([A-Za-z0-9_-]+)"),
    ("bamboohr", r"([A-Za-z0-9_-]+)\.bamboohr\.com"),
    ("icims", r"([A-Za-z0-9_-]+)\.icims\.com"),
    ("jobvite", r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)"),
    ("teamtailor", r"([A-Za-z0-9_-]+)\.teamtailor\.com"),
    ("pinpoint", r"([A-Za-z0-9_-]+)\.pinpointhq\.com"),
]

IGNORE_SLUGS = {"embed", "js", "static", "api", "v1", "www"}


def careers_page_scan(name):
    stems = []
    for s in slug_variants(name)[:6]:
        if s not in stems:
            stems.append(s)
    urls = []
    for stem in stems[:3]:
        for tld in (".com", ".ai", ".io"):
            for path in ("/careers", "/company/careers", "/jobs", "/about/careers"):
                urls.append(f"https://www.{stem}{tld}{path}")
    for url in urls:
        r = get(url)
        if r is None or r.status_code != 200:
            continue
        html = r.text
        for platform, pattern in LINK_PATTERNS:
            m = re.search(pattern, html)
            if m and m.group(1).lower() not in IGNORE_SLUGS:
                return platform, m.group(1), url
    return None


def resolve(name):
    for slug in slug_variants(name):
        for platform, check in API_CHECKS:
            if check(slug):
                return name, platform, slug, "api"
    found = careers_page_scan(name)
    if found:
        platform, slug, url = found
        return name, platform, slug, url
    return name, None, None, None


def main():
    companies = read_companies()
    print(f"Resolving {len(companies)} companies...")
    results = []
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        for res in pool.map(resolve, companies):
            name, platform, slug, source = res
            print(f"{name}: {platform or 'not found'} {slug or ''}")
            results.append(res)

    overrides, other, unresolved = [], {}, []
    for name, platform, slug, source in sorted(results, key=lambda r: r[0].lower()):
        if platform in SUPPORTED:
            overrides.append(f"{name} | {platform}:{slug}")
        elif platform:
            other.setdefault(platform, []).append(f"{name} ({slug})")
        else:
            unresolved.append(name)

    lines = [
        "ATS resolver results",
        "",
        f"PASTE INTO companies.txt ({len(overrides)})",
        "Replace each company's existing line with the override below.",
        "",
        *overrides,
        "",
        "ON PLATFORMS THE BOT CAN'T READ YET",
        "",
    ]
    for platform in sorted(other, key=lambda p: -len(other[p])):
        lines.append(f"{platform} ({len(other[platform])})")
        lines.extend(f"  {entry}" for entry in other[platform])
        lines.append("")
    lines += [f"STILL UNRESOLVED ({len(unresolved)})", "", *unresolved, ""]

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {OUT}: {len(overrides)} overrides, "
          f"{sum(len(v) for v in other.values())} on other platforms, "
          f"{len(unresolved)} unresolved")


if __name__ == "__main__":
    main()
