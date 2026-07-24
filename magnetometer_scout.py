#!/usr/bin/env python3
"""
Magnetometer Scout — harsh-environment Hall effect sensor & magnetometer digest.

Companion to paper_scout.py, run as a fully separate script/workflow so it
can't affect the shared Hone Lab digest. Tracks literature on Hall effect
sensors and novel magnetometers (any material, 2D materials prioritized)
built for two harsh-environment application spaces:
  - Fusion / fission reactors (tokamak plasma diagnostics, ITER, etc.)
  - Space (radiation environment, spacecraft/CubeSat magnetometers)

Sends a single daily email to one recipient (no subscriber list).

Usage:
    python magnetometer_scout.py            # production run
    python magnetometer_scout.py --test      # dry-run: print digest, no email
    python magnetometer_scout.py --lookback 7
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ─── Configuration ────────────────────────────────────────────────────────────

CONFIG = {
    # Sent via the same Apps Script endpoint (Gmail/MailApp) as the main
    # PaperScout digest — SendGrid's free tier ran out of credits in June 2026.
    "apps_script_url": os.getenv("APPS_SCRIPT_URL", ""),
    "apps_script_key": os.getenv("APPS_SCRIPT_KEY", ""),
    "from_name": "Magnetometer Scout",
    "to_email": os.getenv("MAGNETOMETER_TO_EMAIL", "mb5159@columbia.edu"),

    # Adaptive lookback — this niche is narrower, so target fewer papers/day
    "min_papers": 1,
    "max_papers": 5,
    "max_lookback_days": 180,
    "lookback_steps": [1, 3, 7, 14, 30, 60, 90, 180],

    "min_score": 20,
    "max_per_source": 5,

    "state_file": Path(__file__).parent / "seen_ids_magnetometer.json",
}

# ─── arXiv categories & base queries ──────────────────────────────────────────

ARXIV_CATEGORIES = [
    "cond-mat.mes-hall",
    "cond-mat.mtrl-sci",
    "physics.app-ph",
    "physics.ins-det",   # instrumentation & detectors — key for sensor papers
    "physics.plasm-ph",  # plasma physics — key for tokamak/fusion diagnostics
    "physics.space-ph",  # space physics — key for spacecraft magnetometers
]

ARXIV_TOPIC_QUERIES = [
    '("Hall sensor" OR "Hall effect sensor" OR "Hall probe" OR "Hall bar" OR '
    '"magnetoresistive sensor" OR "GMR sensor" OR "TMR sensor" OR "AMR sensor" OR '
    '"magnetometer") AND '
    '("tokamak" OR "fusion reactor" OR "fusion diagnostic" OR "plasma diagnostic" OR '
    '"ITER" OR "stellarator" OR "magnetic confinement fusion" OR "fission reactor" OR '
    '"nuclear reactor")',

    '("Hall sensor" OR "Hall effect sensor" OR "Hall probe" OR "magnetometer" OR '
    '"magnetic field sensor" OR "magnetoresistive sensor") AND '
    '("space radiation" OR "radiation hardness" OR "radiation tolerance" OR '
    '"proton irradiation" OR "neutron irradiation" OR "total ionizing dose" OR '
    '"cosmic ray" OR "spacecraft" OR "CubeSat" OR "low earth orbit" OR '
    '"space environment" OR "space application")',

    '("2D material" OR "van der Waals" OR "graphene" OR "TMD" OR "MoS2" OR "WSe2" OR '
    '"hBN" OR "black phosphorus" OR "topological insulator") AND '
    '("Hall sensor" OR "Hall effect" OR "magnetometer" OR "magnetic field sensor")',

    '("NV center" OR "nitrogen-vacancy" OR "diamond magnetometer" OR '
    '"SQUID magnetometer" OR "fluxgate magnetometer" OR "optically pumped magnetometer" OR '
    '"spin Hall" OR "quantum sensor") AND '
    '("radiation" OR "fusion" OR "tokamak" OR "space" OR "harsh environment" OR '
    '"high temperature")',
]

# ─── PubMed ────────────────────────────────────────────────────────────────────

PUBMED_TERMS = (
    '(("Hall sensor"[tiab] OR "Hall effect sensor"[tiab] OR "magnetometer"[tiab] OR '
    '"magnetoresistive sensor"[tiab]) AND '
    '("tokamak"[tiab] OR "fusion reactor"[tiab] OR "space radiation"[tiab] OR '
    '"radiation hardness"[tiab] OR "spacecraft"[tiab] OR "harsh environment"[tiab]))'
)


def fetch_pubmed(days_back: int) -> list[dict]:
    date_from = (datetime.date.today() - datetime.timedelta(days=days_back + 1)).strftime("%Y/%m/%d")
    date_to = datetime.date.today().strftime("%Y/%m/%d")
    search_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={urllib.parse.quote(PUBMED_TERMS)}"
        f"&mindate={date_from}&maxdate={date_to}&datetype=pdat"
        "&retmax=40&retmode=json&tool=magnetometerscout&email=scout@example.com"
    )
    print(f" [PubMed] Searching (lookback={days_back}d)...")
    try:
        with urllib.request.urlopen(search_url, timeout=30) as resp:
            ids = json.loads(resp.read())["esearchresult"]["idlist"]
    except Exception as e:
        print(f" [PubMed] Search ERROR: {e}")
        return []

    if not ids:
        print(" [PubMed] 0 papers found.")
        return []

    fetch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        f"?db=pubmed&id={','.join(ids)}&retmode=json&tool=magnetometerscout&email=scout@example.com"
    )
    try:
        with urllib.request.urlopen(fetch_url, timeout=30) as resp:
            summaries = json.loads(resp.read())["result"]
    except Exception as e:
        print(f" [PubMed] Fetch ERROR: {e}")
        return []

    papers = []
    for pmid in ids:
        s = summaries.get(pmid, {})
        papers.append({
            "id": f"pubmed:{pmid}",
            "source": s.get("source", "PubMed"),
            "title": s.get("title", ""),
            "abstract": "",
            "authors": [a.get("name", "") for a in s.get("authors", [])],
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "published": s.get("pubdate", "")[:10],
        })
    print(f" [PubMed] {len(papers)} papers fetched.")
    return papers


# ─── arXiv Fetch ─────────────────────────────────────────────────────────────

def _parse_arxiv_entries(data: bytes) -> list[dict]:
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(data)
    papers = []
    for entry in root.findall("atom:entry", ns):
        id_el = entry.find("atom:id", ns)
        if id_el is None:
            continue
        paper_id = id_el.text.strip().split("/abs/")[-1]
        title = entry.find("atom:title", ns).text.strip().replace("\n", " ")
        summary = entry.find("atom:summary", ns).text.strip().replace("\n", " ")
        authors = [a.find("atom:name", ns).text for a in entry.findall("atom:author", ns)]
        published = entry.find("atom:published", ns).text[:10]
        papers.append({
            "id": f"arxiv:{paper_id}",
            "source": "arXiv",
            "title": title,
            "abstract": summary,
            "authors": authors,
            "url": f"https://arxiv.org/abs/{paper_id}",
            "published": published,
        })
    return papers


def fetch_arxiv(days_back: int) -> list[dict]:
    date_from = (datetime.date.today() - datetime.timedelta(days=days_back + 1)).strftime("%Y%m%d")
    date_to = datetime.date.today().strftime("%Y%m%d")
    cat_filter = " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES)

    all_papers: list[dict] = []
    seen_ids: set[str] = set()
    print(f" [arXiv] {len(ARXIV_TOPIC_QUERIES)} queries (lookback={days_back}d)...")

    for i, topic in enumerate(ARXIV_TOPIC_QUERIES):
        query = (
            f"({cat_filter}) AND ({topic}) "
            f"AND submittedDate:[{date_from}0000 TO {date_to}2359]"
        )
        params = urllib.parse.urlencode({
            "search_query": query, "start": 0, "max_results": 100,
            "sortBy": "submittedDate", "sortOrder": "descending",
        })
        url = f"https://export.arxiv.org/api/query?{params}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MagnetometerScout/1.0 (mb5159@columbia.edu)"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            papers = _parse_arxiv_entries(data)
            new = [p for p in papers if p["id"] not in seen_ids]
            seen_ids.update(p["id"] for p in new)
            all_papers.extend(new)
            print(f" [arXiv] Query {i+1}/{len(ARXIV_TOPIC_QUERIES)}: +{len(new)}")
        except Exception as e:
            print(f" [arXiv] Query {i+1} error: {e}")
        if i < len(ARXIV_TOPIC_QUERIES) - 1:
            time.sleep(3)

    print(f" [arXiv] {len(all_papers)} unique papers total.")
    return all_papers


# ─── Crossref Journal Fetch ───────────────────────────────────────────────────

CROSSREF_JOURNALS = [
    ("0029-5515", "Nuclear Fusion"),
    ("0034-6748", "Review of Scientific Instruments"),
    ("0920-3796", "Fusion Engineering and Design"),
    ("0741-3335", "Plasma Physics and Controlled Fusion"),
    ("1530-437X", "IEEE Sensors Journal"),
    ("0018-9499", "IEEE Transactions on Nuclear Science"),
    ("1361-648X", "Journal of Physics: Condensed Matter"),
    ("2053-1583", "2D Materials"),
    ("0003-6951", "Applied Physics Letters"),
    ("0021-8979", "Journal of Applied Physics"),
    ("2520-1131", "Nature Electronics"),
]

CROSSREF_KEYWORDS = (
    "\\\"Hall sensor\\\" OR \\\"Hall effect sensor\\\" OR \\\"magnetometer\\\" OR "
    "\\\"magnetoresistive sensor\\\" OR \\\"magnetic field sensor\\\" OR "
    "\\\"tokamak\\\" OR \\\"fusion diagnostic\\\" OR \\\"radiation hard sensor\\\""
)


def fetch_crossref(days_back: int) -> list[dict]:
    from_date = (datetime.date.today() - datetime.timedelta(days=days_back + 1)).strftime("%Y-%m-%d")
    all_papers: list[dict] = []
    seen_ids: set[str] = set()
    print(f" [Crossref] Searching {len(CROSSREF_JOURNALS)} journals (lookback={days_back}d)...")

    for issn, journal_name in CROSSREF_JOURNALS:
        url = (
            "https://api.crossref.org/works"
            f"?query={urllib.parse.quote(CROSSREF_KEYWORDS)}"
            f"&filter=issn:{issn},from-pub-date:{from_date}"
            "&rows=20&sort=relevance&select=DOI,title,author,published,abstract,container-title"
        )
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "MagnetometerScout/1.0 (mb5159@columbia.edu)"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            items = data.get("message", {}).get("items", [])
            new_count = 0
            for item in items:
                doi = item.get("DOI", "")
                if not doi or doi in seen_ids:
                    continue
                seen_ids.add(doi)
                title_parts = item.get("title", [])
                title = title_parts[0] if title_parts else ""
                if not title:
                    continue
                authors = [
                    f"{a.get('given', '')} {a.get('family', '')}".strip()
                    for a in item.get("author", [])
                ]
                pub = item.get("published", {}).get("date-parts", [[""]])[0]
                pub_date = "-".join(str(p).zfill(2) for p in pub) if pub and pub[0] else ""
                if len(pub_date) == 4:
                    pub_date += "-01-01"
                elif len(pub_date) == 7:
                    pub_date += "-01"
                abstract = re.sub(r"<[^>]+>", "", item.get("abstract", ""))
                all_papers.append({
                    "id": f"doi:{doi}",
                    "source": journal_name,
                    "title": title,
                    "abstract": abstract,
                    "authors": authors,
                    "url": f"https://doi.org/{doi}",
                    "published": pub_date[:10],
                })
                new_count += 1
            print(f" [Crossref] {journal_name}: +{new_count}")
        except Exception as e:
            print(f" [Crossref] {journal_name} error: {e}")
        time.sleep(0.5)

    print(f" [Crossref] {len(all_papers)} total papers.")
    return all_papers


# ─── Semantic Scholar Fetch (fallback) ────────────────────────────────────────

SS_QUERIES = [
    "Hall effect sensor fusion reactor tokamak radiation tolerant",
    "2D material Hall sensor space radiation hardness",
    "magnetometer radiation hard spacecraft harsh environment",
    "giant magnetoresistance sensor tokamak plasma diagnostic",
    "diamond NV center magnetometer high temperature radiation",
]


def fetch_semantic_scholar(days_back: int) -> list[dict]:
    papers_seen: set[str] = set()
    all_papers: list[dict] = []
    print(f" [S2] {len(SS_QUERIES)} queries...")

    for q in SS_QUERIES:
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={urllib.parse.quote(q)}&limit=20"
            "&fields=paperId,title,abstract,authors,year,externalIds,publicationDate,venue"
        )
        data: dict = {}
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MagnetometerScout/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                break
            except Exception as e:
                if "429" in str(e):
                    wait = 15 * (2 ** attempt)
                    print(f" [S2] Rate limited — waiting {wait}s...")
                    time.sleep(wait)
                else:
                    print(f" [S2] Error: {e}")
                    break

        for p in (data.get("data") or []):
            pid = p.get("paperId", "")
            if pid in papers_seen:
                continue
            papers_seen.add(pid)
            doi = (p.get("externalIds") or {}).get("DOI", "")
            url_out = f"https://doi.org/{doi}" if doi else f"https://www.semanticscholar.org/paper/{pid}"
            year = p.get("year") or ""
            pub_date = p.get("publicationDate") or (f"{year}-01-01" if year else "1900-01-01")
            all_papers.append({
                "id": f"s2:{pid}",
                "source": p.get("venue") or "Semantic Scholar",
                "title": p.get("title", ""),
                "abstract": p.get("abstract") or "",
                "authors": [a["name"] for a in p.get("authors", [])],
                "url": url_out,
                "published": pub_date[:10],
            })
        time.sleep(3.0)

    print(f" [S2] {len(all_papers)} papers fetched.")
    return all_papers


# ─── Relevance Scorer ─────────────────────────────────────────────────────────

SENSOR_KWS = [
    "hall sensor", "hall effect sensor", "hall probe", "hall bar", "hall element",
    "magnetometer", "vector magnetometer", "fluxgate magnetometer",
    "squid magnetometer", "optically pumped magnetometer", "search coil magnetometer",
    "nv center magnetometer", "nitrogen-vacancy magnetometer", "diamond magnetometer",
    "magnetoresistive sensor", "giant magnetoresistance", "gmr sensor",
    "tunneling magnetoresistance", "tmr sensor", "anisotropic magnetoresistance",
    "amr sensor", "magnetic field sensor", "magnetic sensor", "spin hall sensor",
    "corbino disk", "corbino geometry", "magnetic field probe", "quantum sensor magnetic",
]

FUSION_KWS = [
    "tokamak", "fusion reactor", "fusion diagnostic", "plasma diagnostic",
    "iter", "stellarator", "magnetic confinement fusion", "fission reactor",
    "nuclear reactor instrumentation", "in-vessel sensor", "neutron flux monitor",
    "plasma current diagnostic", "magnetic diagnostic fusion", "divertor",
    "fusion energy", "burning plasma",
]

SPACE_KWS = [
    "space radiation", "radiation hardness", "radiation tolerance", "radiation hard",
    "proton irradiation", "neutron irradiation", "gamma irradiation",
    "heavy ion irradiation", "total ionizing dose", "displacement damage",
    "single event effect", "cosmic ray", "van allen belt", "spacecraft magnetometer",
    "cubesat", "planetary magnetometer", "heliophysics", "low earth orbit",
    "space environment", "space application", "radiation-hard electronics",
    "iss experiment", "geo orbit", "space qualified",
]

ENV_KWS = FUSION_KWS + SPACE_KWS + [
    "harsh environment", "extreme environment", "high temperature sensor",
    "cryogenic sensor", "radiation-hard sensor",
]

MATERIAL_2D_KWS = [
    "2d material", "van der waals", "graphene", "mos2", "wse2", "ws2", "mose2",
    "mote2", "tmd", "transition metal dichalcogenide", "hbn",
    "hexagonal boron nitride", "black phosphorus", "topological insulator",
    "bi2se3", "bi2te3", "nbse2", "monolayer", "few-layer",
]

MATERIAL_OTHER_KWS = [
    "insb", "indium antimonide", "inas", "indium arsenide", "gaas", "gallium arsenide",
    "gan", "gallium nitride", "algan", "bismuth hall", "silicon hall",
    "compound semiconductor hall", "iii-v hall",
]

NEGATIVE_KWS = [
    "magnetoencephalography", "meg brain", "biomagnetism", "geomagnetic survey",
    "indoor localization", "indoor positioning", "vehicle detection magnetometer",
    "compass calibration", "archaeological survey", "mineral exploration",
    "smartphone magnetometer", "battery cathode", "drug delivery", "protein folding",
    "perovskite solar", "organic semiconductor", "lithium ion battery",
]

HIGH_VALUE_AUTHORS = [
    # Authors of the reference paper (2D materials for fusion diagnostics)
    "Szary", "El-Ahmar", "Prokopowicz", "Ciuk",
    # Stanford harsh-environment sensor group (Senesky/Dowling network)
    "Senesky", "Dowling K", "Dowling KM", "Alpert H", "Chapin C",
    "Yalamarthy", "Satterthwaite", "Toor A",
    # Columbia — GaN/2D sensors for space
    "Eisner", "Hone", "Shepard K",
]


def score_paper(paper: dict) -> tuple[int, list[str]]:
    text = (paper["title"] + " " + paper["abstract"] + " " +
            " ".join(paper["authors"])).lower()
    title = paper["title"].lower()
    score = 0
    signals: list[str] = []

    def hit(kw_list: list[str], points: int, tag: str, title_bonus: int = 5):
        nonlocal score
        for kw in kw_list:
            if kw in text:
                pts = points + (title_bonus if kw in title else 0)
                score += pts
                signals.append(f"{tag}:{kw}(+{pts})")

    hit(SENSOR_KWS, 20, "SENSOR", title_bonus=5)
    hit(ENV_KWS, 20, "ENV", title_bonus=5)
    hit(MATERIAL_2D_KWS, 10, "MAT2D", title_bonus=3)
    hit(MATERIAL_OTHER_KWS, 5, "MATOTHER", title_bonus=2)

    author_str = " ".join(paper["authors"]).lower()
    for a in HIGH_VALUE_AUTHORS:
        if a.lower() in author_str:
            score += 22
            signals.append(f"HV_AUTHOR:{a}(+22)")

    for kw in NEGATIVE_KWS:
        if kw in text:
            score -= 25
            signals.append(f"NEG:{kw}(-25)")

    has_sensor = any(s.startswith("SENSOR:") for s in signals)
    has_env = any(s.startswith("ENV:") for s in signals)
    if not (has_sensor and has_env):
        # Hard requirement: must be about a sensor/magnetometer AND a harsh
        # environment. A paper about fusion RF heating or a generic space
        # radiation study with no sensor angle is not what this digest is for.
        score = 0
        signals.append("REQUIRES_SENSOR+ENV:excluded")

    if has_sensor and has_env and any(s.startswith("MAT2D:") for s in signals):
        score += 15
        signals.append("SENSOR+ENV+2D_BONUS(+15)")

    if has_sensor and has_env and re.search(r'\breview\b|\bsurvey\b|\bperspective\b', title):
        score = min(score + 10, 90)  # reviews/perspectives are valuable in this niche — light boost, not a cap
        signals.append("REVIEW_BONUS")

    return max(0, min(score, 100)), signals


# ─── State / Deduplication ────────────────────────────────────────────────────

def load_seen_ids() -> set[str]:
    p = CONFIG["state_file"]
    if p.exists():
        return set(json.loads(p.read_text()))
    return set()


def save_seen_ids(ids: set[str]) -> None:
    CONFIG["state_file"].write_text(json.dumps(sorted(ids), indent=2))


# ─── Email Builder ────────────────────────────────────────────────────────────

def build_email_html(papers_by_source: dict, date_str: str, total: int, days_used: int) -> str:
    window_note = f"{days_used}-day window" if days_used > 1 else "past 24 hours"

    if total == 0:
        body_content = """
        <tr><td style="padding:40px 0; text-align:center; color:#64748b; font-size:15px;">
        <div style="font-size:48px; margin-bottom:16px;">🧲</div>
        <div style="font-weight:600; color:#1e1b4b; font-size:18px; margin-bottom:8px;">
        No relevant papers found
        </div>
        <div>Searched the past 6 months across arXiv, PubMed, Crossref, and Semantic Scholar.</div>
        </td></tr>"""
    else:
        body_content = ""
        for source, papers in papers_by_source.items():
            body_content += f"""
            <tr><td style="padding:18px 0 6px; font-size:13px; font-weight:700;
            color:#0891b2; letter-spacing:0.08em; text-transform:uppercase;
            border-top:2px solid #ecfeff;">
            {source} — {len(papers)} paper{"s" if len(papers) != 1 else ""}
            </td></tr>"""
            for p in papers:
                authors_short = ", ".join(p["authors"][:4])
                if len(p["authors"]) > 4:
                    authors_short += f" +{len(p['authors'])-4} more"
                abstract_snip = (p["abstract"][:300] + "…") if len(p["abstract"]) > 300 else p["abstract"]
                score_color = "#22c55e" if p["score"] >= 60 else ("#f59e0b" if p["score"] >= 35 else "#94a3b8")
                body_content += f"""
                <tr><td style="padding:12px 0 16px; border-bottom:1px solid #f1f5f9;">
                <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <a href="{p['url']}" style="font-size:15px; font-weight:600; color:#0e293e;
                text-decoration:none; line-height:1.4; flex:1; margin-right:12px;">{p['title']}</a>
                <span style="background:{score_color}22; color:{score_color}; font-size:11px;
                font-weight:700; padding:3px 8px; border-radius:20px; white-space:nowrap;
                flex-shrink:0;">{p['score']}%</span>
                </div>
                <div style="font-size:12px; color:#64748b; margin:4px 0 6px;">
                {authors_short} &nbsp;·&nbsp; {p['published']}
                </div>
                {"<div style='font-size:13px; color:#475569; line-height:1.6;'>" + abstract_snip + "</div>" if abstract_snip else ""}
                </td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Magnetometer Scout Digest</title></head>
<body style="margin:0; padding:0; background:#f0fbfd; font-family:'Georgia',serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0fbfd; padding:32px 0;">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0"
style="background:#ffffff; border-radius:16px;
box-shadow:0 4px 32px rgba(8,145,178,0.10); overflow:hidden;">

<tr><td style="background:linear-gradient(135deg,#0e293e 0%,#0891b2 100%);
padding:36px 40px 28px;">
<div style="font-size:11px; color:#a5f3fc; letter-spacing:0.15em;
text-transform:uppercase; margin-bottom:8px;">Harsh-Environment Sensing</div>
<div style="font-size:26px; font-weight:700; color:#ffffff;
letter-spacing:-0.02em;">🧲 Magnetometer Scout</div>
<div style="font-size:13px; color:#cffafe; margin-top:6px;">
{date_str} &nbsp;·&nbsp;
{"<strong style='color:#fff'>" + str(total) + " new paper" + ("s" if total != 1 else "") + "</strong>" if total > 0 else "No new papers found"}
&nbsp;·&nbsp; {window_note}
</div>
</td></tr>

<tr><td style="padding:8px 40px 32px;">
<table width="100%" cellpadding="0" cellspacing="0">
{body_content}
</table>
</td></tr>

<tr><td style="background:#f0fbfd; padding:20px 40px; border-top:1px solid #cffafe;">
<div style="font-size:11px; color:#94a3b8; text-align:center; line-height:1.8;">
Hall effect sensors & novel magnetometers for fusion/fission and space environments<br>
Powered by Magnetometer Scout · arXiv · PubMed · Crossref · Semantic Scholar
</div>
</td></tr>
</table>
</td></tr></table>
</body></html>"""


def build_email_text(papers_by_source: dict, date_str: str, days_used: int) -> str:
    window = f"{days_used}-day window"
    lines = [f"Magnetometer Scout Digest — {date_str} ({window})", "=" * 60, ""]
    if not papers_by_source:
        lines += ["No relevant papers found in the past 6 months.", ""]
    else:
        for source, papers in papers_by_source.items():
            lines.append(f"[ {source} ]")
            for p in papers:
                lines += [
                    f"  {p['title']}",
                    f"  Score: {p['score']}% | {p['published']}",
                    f"  {', '.join(p['authors'][:3])}",
                    f"  {p['url']}",
                    "",
                ]
    return "\n".join(lines)


# ─── Send via Apps Script (Gmail/MailApp) ─────────────────────────────────────

def send_apps_script(subject: str, html: str, text: str, to_email: str) -> None:
    """Send via the same Apps Script web-app endpoint the main digest uses."""
    url = CONFIG["apps_script_url"]
    key = CONFIG["apps_script_key"]
    if not url or not key:
        raise RuntimeError("APPS_SCRIPT_URL / APPS_SCRIPT_KEY not set")

    payload = {
        "action": "send_digest",
        "key": key,
        "subject": subject,
        "html": html,
        "text": text,
        "recipients": [{"email": to_email, "name": ""}],
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            print(f" ✅ Gmail (Apps Script): sent to {result.get('sent', '?')} recipient(s).")
            if result.get("errors"):
                print(f" ⚠ Partial errors: {result['errors']}")
        else:
            raise RuntimeError(f"Apps Script error: {result}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"Apps Script HTTP error {e.code}: {body}") from e


# ─── Date helpers ─────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(date_str[:10])
    except Exception:
        return datetime.date.min


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(test_mode: bool = False, force_lookback: int | None = None) -> None:
    date_str = datetime.date.today().strftime("%B %d, %Y")

    if force_lookback is not None:
        start_idx = next(
            (i for i, s in enumerate(CONFIG["lookback_steps"]) if s >= force_lookback),
            len(CONFIG["lookback_steps"]) - 1,
        )
        steps = CONFIG["lookback_steps"][start_idx:]
        if not steps:
            steps = [force_lookback]
    else:
        steps = CONFIG["lookback_steps"]

    max_step = min(CONFIG["max_lookback_days"], CONFIG["lookback_steps"][-1])

    seen_on_disk = load_seen_ids()
    print(f"\n🧲 Magnetometer Scout — fetching full {max_step}-day window...")
    raw_all: list[dict] = []
    raw_all += fetch_arxiv(max_step)
    raw_all += fetch_pubmed(max_step)
    raw_all += fetch_crossref(max_step)
    raw_all += fetch_semantic_scholar(max_step)

    novel = [p for p in raw_all if p["id"] not in seen_on_disk]
    title_seen: set[str] = set()
    deduped: list[dict] = []
    for p in novel:
        norm = re.sub(r'\W+', ' ', p["title"].lower()).strip()
        if norm and norm not in title_seen and len(p["title"]) > 10:
            title_seen.add(norm)
            deduped.append(p)

    for p in deduped:
        p["score"], p["signals"] = score_paper(p)

    print(f" Fetched {len(raw_all)} total, {len(deduped)} novel after dedup.")

    today = datetime.date.today()
    relevant: list[dict] = []
    days_used = max_step

    for days in steps:
        if days > max_step:
            days = max_step
        cutoff = today - datetime.timedelta(days=days)
        window_papers = sorted(
            [p for p in deduped
             if p["score"] >= CONFIG["min_score"] and _parse_date(p["published"]) >= cutoff],
            key=lambda x: x["score"], reverse=True,
        )
        days_used = days
        print(f" {days:>3}d window → {len(window_papers)} relevant (score≥{CONFIG['min_score']})")
        if len(window_papers) >= CONFIG["min_papers"] or days >= max_step:
            relevant = window_papers
            if len(window_papers) >= CONFIG["min_papers"]:
                print(f" ✓ Target met ({CONFIG['min_papers']}+).")
            else:
                print(f" ⚠ Max lookback reached — {len(relevant)} paper(s).")
            break
        relevant = window_papers

    display_papers = relevant[: CONFIG["max_papers"]]
    by_source: dict[str, list[dict]] = {}
    for p in display_papers:
        src = p["source"]
        if src not in by_source:
            by_source[src] = []
        if len(by_source[src]) < CONFIG["max_per_source"]:
            by_source[src].append(p)

    total = sum(len(v) for v in by_source.values())
    subject = (
        f"🧲 Magnetometer Scout: No new papers [{days_used}d] — {date_str}"
        if total == 0
        else f"🧲 Magnetometer Scout: {total} paper{'s' if total != 1 else ''} [{days_used}d] — {date_str}"
    )

    if test_mode:
        print("\n" + "─" * 60)
        print(build_email_text(by_source, date_str, days_used))
        print("─" * 60)
        preview_path = Path(__file__).parent / "magnetometer_digest_preview.html"
        preview_path.write_text(build_email_html(by_source, date_str, total, days_used))
        print(f"\n 📄 HTML preview → {preview_path}")
    else:
        html = build_email_html(by_source, date_str, total, days_used)
        text = build_email_text(by_source, date_str, days_used)
        send_apps_script(subject, html, text, CONFIG["to_email"])

    all_fetched_ids = {p["id"] for p in deduped}
    save_seen_ids(seen_on_disk | all_fetched_ids)
    print(f"\n✅ Done. {total} papers in digest (lookback={days_used}d). "
          f"{len(all_fetched_ids)} IDs saved.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Magnetometer Scout")
    parser.add_argument("--test", action="store_true", help="Dry-run, no email")
    parser.add_argument("--lookback", type=int, default=None, help="Force starting lookback (days)")
    args = parser.parse_args()
    main(test_mode=args.test, force_lookback=args.lookback)
