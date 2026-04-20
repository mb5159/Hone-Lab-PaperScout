#!/usr/bin/env python3
"""
PaperScout v3 — Hone Lab shared literature digest.

Changes from v2:
  - Reads active subscribers + interests from Google Sheet via Apps Script endpoint
  - Unions all subscriber interests to drive search; supplements base keyword set
  - Sends via SendGrid API (no SMTP dependency)
  - Includes per-subscriber unsubscribe link in footer
  - State (seen paper IDs) persists in seen_ids.json committed back to the repo

Usage:
    python paper_scout.py            # production run
    python paper_scout.py --test     # dry-run: print digest, no email
    python paper_scout.py --lookback 7   # force a specific starting lookback
"""

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
    # SendGrid
    "sendgrid_api_key": os.getenv("SENDGRID_API_KEY", ""),
    "from_email":       os.getenv("FROM_EMAIL", "paperscout@yourdomain.com"),
    "from_name":        "Hone Lab PaperScout",

    # Apps Script endpoint (returns subscriber list)
    "apps_script_url": os.getenv("APPS_SCRIPT_URL", ""),
    "apps_script_key": os.getenv("APPS_SCRIPT_KEY", ""),

    # GitHub Pages base URL (used for unsubscribe / manage links in emails)
    "pages_url": os.getenv("PAGES_URL", "https://YOUR_USERNAME.github.io/YOUR_REPO"),

    # Adaptive lookback
    "min_papers":        5,
    "max_papers":        10,
    "max_lookback_days": 180,
    "initial_lookback":  1,
    "lookback_steps":    [1, 3, 7, 14, 30, 60, 90, 180, 365],

    "min_score":     25,
    "max_per_source": 10,

    # Seen-IDs state file (committed back to repo by CI)
    "state_file": Path(__file__).parent / "seen_ids.json",
}

# ─── Interest category → additional arXiv queries ────────────────────────────
# These supplement the base ARXIV_TOPIC_QUERIES when at least one subscriber
# has selected the interest. "radiation" and "assembly" are already in the base
# queries so they have empty lists here.

INTEREST_ARXIV_QUERIES = {
    "synthesis": [
        '("CVD graphene" OR "chemical vapor deposition graphene" OR "epitaxial graphene" '
        'OR "graphene growth" OR "TMD synthesis" OR "MoS2 synthesis" OR "WSe2 synthesis" '
        'OR "single crystal 2D" OR "scalable synthesis") AND '
        '(cat:cond-mat.mes-hall OR cat:cond-mat.mtrl-sci OR cat:physics.app-ph)',
    ],
    "moire": [
        '("twisted bilayer graphene" OR "magic angle" OR "moiré superlattice" '
        'OR "flat band graphene" OR "correlated insulator" OR "Wigner crystal 2D" '
        'OR "moiré heterostructure" OR "moiré exciton" OR "twisted TMD") AND '
        '(cat:cond-mat.mes-hall OR cat:cond-mat.str-el OR cat:cond-mat.supr-con)',
    ],
    "transport": [
        '("quantum Hall" OR "quantum spin Hall" OR "spin-orbit coupling" OR "Berry phase" '
        'OR "Dirac fermion" OR "topological insulator 2D" OR "Chern insulator" '
        'OR "anomalous Hall" OR "spin transport 2D" OR "high mobility 2D") AND '
        '(cat:cond-mat.mes-hall OR cat:cond-mat.str-el)',
    ],
    "tmds_optical": [
        '("exciton" OR "trion" OR "valley polarization" OR "photoluminescence" '
        'OR "second harmonic generation" OR "Raman 2D" OR "optical absorption 2D" '
        'OR "exciton binding energy" OR "dark exciton" OR "interlayer exciton") AND '
        '("MoS2" OR "WSe2" OR "WS2" OR "MoSe2" OR "MoTe2" OR "TMD" OR '
        '"transition metal dichalcogenide") AND '
        '(cat:cond-mat.mes-hall OR cat:cond-mat.mtrl-sci)',
    ],
    "optoelectronics": [
        '("polariton" OR "exciton-polariton" OR "bolometer" OR "terahertz graphene" '
        'OR "THz detector" OR "LED 2D" OR "optical cavity 2D" OR "waveguide TMD" '
        'OR "nanophotonics 2D" OR "plasmon graphene") AND '
        '(cat:cond-mat.mes-hall OR cat:physics.app-ph OR cat:physics.optics)',
    ],
    "nems": [
        '("NEMS" OR "nanoelectromechanical" OR "graphene resonator" OR "graphene drum" '
        'OR "graphene membrane" OR "Young modulus graphene" OR "mechanical resonance 2D" '
        'OR "graphene pressure sensor" OR "2D material mechanical") AND '
        '(cat:cond-mat.mes-hall OR cat:cond-mat.mtrl-sci OR cat:physics.app-ph)',
    ],
    "devices": [
        '("field effect transistor" OR "FET" OR "contact resistance 2D" '
        'OR "ohmic contact graphene" OR "gate dielectric 2D" OR "tunnel junction 2D" '
        'OR "2D semiconductor transistor" OR "subthreshold swing 2D") AND '
        '("graphene" OR "TMD" OR "MoS2" OR "WSe2" OR "2D material" OR "hBN") AND '
        '(cat:cond-mat.mes-hall OR cat:physics.app-ph)',
    ],
    "radiation": [],   # covered by base ARXIV_TOPIC_QUERIES
    "assembly": [],    # covered by base ARXIV_TOPIC_QUERIES
    "theory": [
        '("DFT" OR "density functional theory" OR "first principles" OR "tight binding" '
        'OR "ab initio" OR "GW calculation" OR "many-body perturbation" OR "TDDFT") AND '
        '("graphene" OR "TMD" OR "2D material" OR "van der Waals" OR "monolayer") AND '
        '(cat:cond-mat.mes-hall OR cat:cond-mat.mtrl-sci)',
    ],
}

# ─── Research Profile (base — always active) ─────────────────────────────────

RESEARCH_PROFILE = {
    "primary_keywords": [
        "2D material", "van der Waals", "vdW heterostructure", "monolayer",
        "few-layer", "bilayer", "TMD", "transition metal dichalcogenide",
        "MoS2", "WSe2", "WS2", "MoSe2", "MoTe2", "hBN", "hexagonal boron nitride",
        "graphene", "graphite exfoliation", "NbSe2", "CrI3", "InSe", "black phosphorus",
        "ReS2", "ReSe2", "TaS2", "VSe2",
        "gold exfoliation", "tape exfoliation", "deterministic exfoliation",
        "large-scale exfoliation", "N-layer", "layer-controlled", "patterned exfoliation",
        "flux growth", "bulk crystal", "crystal growth 2D", "mechanical exfoliation",
        "chemical vapor deposition 2D", "CVD graphene", "MBE 2D",
        "space radiation", "radiation hardness", "radiation hardness 2D",
        "proton irradiation", "neutron irradiation", "gamma irradiation",
        "heavy ion irradiation", "cosmic ray", "ISS experiment", "space application 2D",
        "radiation tolerance TMD", "radiation effect graphene", "radiation damage 2D",
        "total ionizing dose", "TID effect", "single event effect", "SEE",
        "displacement damage", "radiation-hard electronics",
        "Hall bar", "Hall effect 2D", "interdigitated FET", "UV photodetector 2D",
        "gated photoluminescence", "transfer length method", "TLM measurement",
        "CAFM", "conductive AFM", "Raman 2D", "encapsulation hBN",
        "dielectric encapsulation", "Corbino disk", "high-mobility graphene",
        "field effect transistor 2D", "FET 2D material",
        "GaN 2D", "GaN graphene", "GaN TMD", "nitride heterostructure",
        "tokamak sensor", "plasma diagnostics Hall",
        "moiré superlattice", "twisted bilayer", "correlated state 2D",
        "quantum Hall effect 2D", "fractional quantum Hall 2D",
        "clean interface vdW", "encapsulated graphene", "dry transfer",
        "polymer-free transfer", "stamp transfer 2D",
    ],

    "tracked_authors": {
        "columbia_pi": [
            "Eisner", "Hone", "Dean C", "Dean CR", "Zhu X",
            "Pasupathy", "Basov", "Shepard K", "Kim P", "Heinz T",
            "Muller D", "Menon V",
        ],
        "hone_lab_alumni": [
            "van der Zande", "Mak KF", "Shan J", "Rhodes D", "Ribeiro-Palau",
            "Wang L", "Lee GH", "Lee G", "Petrone N", "Finney N",
            "Yankowitz", "Chae S", "Gao Y", "Arefe G", "Kim YD",
            "Chenet D", "Zhang X",
        ],
        "dean_lab_alumni": [
            "Forsythe C", "Polshyn H", "Kerelsky A", "Rubio-Verdu C",
            "Halbertal D", "Turkel S", "Sunku S", "Shabani S",
        ],
        "stanford_pi": [
            "Pop E", "Chowdhury S", "Mannix A", "Mannix AJ",
            "Cui Y", "Liu F", "Senesky D", "Senesky DG", "Heinz TF",
        ],
        "dowling_network": [
            "Dowling K", "Dowling KM", "Alpert H", "Chapin C",
            "Yalamarthy A", "Satterthwaite P", "Toor A", "Miller R",
        ],
        "mit_pi": [
            "Jarillo-Herrero", "Kong J", "Palacios T", "Kim J",
            "Grossman J", "Grossman JC", "Gedik N",
        ],
        "upenn_pi": ["Jariwala D", "Yang S"],
        "pennstate_pi": ["Robinson J", "Robinson JA", "Das S", "Meunier V"],
        "manchester": [
            "Novoselov", "Geim", "Mishchenko A", "Gorbachev R",
            "Mayorov A", "Blake P", "Nair R", "Kretinin A", "Haigh S",
            "Grigorieva I", "Ponomarenko L", "Elias D", "Morozov S",
        ],
        "montreal": [
            "Martel R", "Martel", "Szkopek T", "Szkopek",
            "Bouchiat V", "Rosei F",
        ],
        "other_major": [
            "McEuen", "Park J", "Ralph D", "Capasso F", "Lukin M",
            "Brongersma M", "Zettl A", "Wang F", "Crommie M", "Louie S",
            "Koppens F", "Kis A", "Radenovic A", "Morpurgo A",
            "Vandersypen L", "Ensslin K", "Ihn T",
            "Lee YH", "Suenaga K", "Watanabe K", "Taniguchi T",
        ],
    },

    "negative_keywords": [
        "CMOS silicon", "organic semiconductor", "perovskite solar", "battery cathode",
        "protein folding", "drug delivery", "genomics", "lithium ion battery",
        "supercapacitor electrode", "dye sensitized",
    ],
}

ALL_TRACKED_AUTHORS = [
    a for group in RESEARCH_PROFILE["tracked_authors"].values() for a in group
]

HIGH_VALUE_AUTHORS = [
    "Eisner", "Hone", "Dean C", "Dean CR", "Zhu X", "Pasupathy", "Basov", "Shepard K",
    "Novoselov", "Geim", "Martel",
    "Jarillo-Herrero", "Kong J", "Palacios T",
    "Pop E", "Mannix", "Senesky D", "Senesky DG", "Dowling K",
    "Mak KF", "van der Zande", "Yankowitz", "Rhodes D", "Ribeiro-Palau",
    "Watanabe K", "Taniguchi T",
    "Koppens F", "Kis A", "Jariwala D", "Robinson J", "Lee YH",
]

# ─── arXiv Base Queries (always run) ─────────────────────────────────────────

ARXIV_CATEGORIES = [
    "cond-mat.mes-hall",
    "cond-mat.mtrl-sci",
    "physics.app-ph",
    "cond-mat.supr-con",
]

ARXIV_TOPIC_QUERIES = [
    '("radiation hardness" OR "radiation damage" OR "proton irradiation" OR '
    '"neutron irradiation" OR "total ionizing dose" OR "space radiation" OR "rad-hard") AND '
    '("graphene" OR "MoS2" OR "WSe2" OR "hBN" OR "TMD" OR "2D material" OR "van der Waals")',

    '("gold-assisted exfoliation" OR "gold-mediated exfoliation" OR "metal-assisted exfoliation" '
    'OR "large-scale exfoliation" OR "wafer-scale exfoliation" OR "exfoliation yield" '
    'OR "gold exfoliation" OR "Au exfoliation")',

    '("hBN encapsulation" OR "encapsulated graphene" OR "deterministic transfer" OR '
    '"dry transfer" OR "van der Waals assembly" OR "flux growth") AND '
    '("graphene" OR "MoS2" OR "WSe2" OR "TMD" OR "2D material")',

    '("Hall bar" OR "Hall sensor" OR "tokamak" OR "UV photodetector" OR "GaN graphene" '
    'OR "GaN heterostructure" OR "interdigitated" OR "Corbino") AND '
    '("graphene" OR "2D" OR "TMD" OR "MoS2" OR "WSe2")',
]

# ─── Subscriber Management ────────────────────────────────────────────────────

def fetch_subscribers() -> list[dict]:
    """Fetch active subscriber list from the Apps Script web endpoint."""
    url  = CONFIG["apps_script_url"]
    key  = CONFIG["apps_script_key"]
    if not url or not key:
        print("  [subscribers] APPS_SCRIPT_URL / APPS_SCRIPT_KEY not set — skipping.")
        return []

    req_url = f"{url}?action=subscribers&key={urllib.parse.quote(key)}"
    try:
        req = urllib.request.Request(req_url, headers={"User-Agent": "PaperScout/3.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        subs = [s for s in data.get("subscribers", []) if s.get("active")]
        print(f"  [subscribers] {len(subs)} active subscriber(s) loaded.")
        return subs
    except Exception as e:
        print(f"  [subscribers] Error fetching subscribers: {e}")
        return []


def build_dynamic_queries(subscribers: list[dict]) -> tuple[list[str], list[str]]:
    """
    Return (extra_arxiv_queries, extra_tracked_authors) derived from the union
    of all active subscriber interests and PI preferences.
    """
    all_interest_codes: set[str] = set()
    all_pi_names:       set[str] = set()

    for s in subscribers:
        for code in s.get("interests", []):
            all_interest_codes.add(code.strip().lower())
        for pi in s.get("tracked_pis", []):
            if pi.strip():
                all_pi_names.add(pi.strip())

    extra_queries: list[str] = []
    for code in all_interest_codes:
        extra_queries.extend(INTEREST_ARXIV_QUERIES.get(code, []))

    print(f"  [dynamic] Interest codes: {sorted(all_interest_codes)}")
    print(f"  [dynamic] Extra queries: {len(extra_queries)}, Extra PIs: {len(all_pi_names)}")
    return extra_queries, list(all_pi_names)


# ─── arXiv Fetch ─────────────────────────────────────────────────────────────

def _parse_arxiv_entries(data: bytes) -> list[dict]:
    ns  = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(data)
    papers = []
    for entry in root.findall("atom:entry", ns):
        id_el = entry.find("atom:id", ns)
        if id_el is None:
            continue
        paper_id  = id_el.text.strip().split("/abs/")[-1]
        title     = entry.find("atom:title", ns).text.strip().replace("\n", " ")
        summary   = entry.find("atom:summary", ns).text.strip().replace("\n", " ")
        authors   = [a.find("atom:name", ns).text for a in entry.findall("atom:author", ns)]
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


def fetch_arxiv(days_back: int, extra_queries: list[str] | None = None) -> list[dict]:
    date_from = (datetime.date.today() - datetime.timedelta(days=days_back + 1)).strftime("%Y%m%d")
    date_to   = datetime.date.today().strftime("%Y%m%d")
    cat_filter = " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES)

    all_queries = list(ARXIV_TOPIC_QUERIES)
    if extra_queries:
        all_queries.extend(extra_queries)

    all_papers: list[dict] = []
    seen_ids:   set[str]   = set()
    print(f"  [arXiv] {len(all_queries)} queries (lookback={days_back}d)...")

    for i, topic in enumerate(all_queries):
        # Extra queries already include cat: filters; base queries need them prepended
        if topic.startswith('(cat:'):
            query = f"{topic} AND submittedDate:[{date_from}0000 TO {date_to}2359]"
        else:
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
            req = urllib.request.Request(url, headers={"User-Agent": "PaperScout/3.0 (matthewbeck00@gmail.com)"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            papers = _parse_arxiv_entries(data)
            new = [p for p in papers if p["id"] not in seen_ids]
            seen_ids.update(p["id"] for p in new)
            all_papers.extend(new)
            print(f"  [arXiv] Query {i+1}/{len(all_queries)}: +{len(new)}")
        except Exception as e:
            print(f"  [arXiv] Query {i+1} error: {e}")
        if i < len(all_queries) - 1:
            time.sleep(3)

    print(f"  [arXiv] {len(all_papers)} unique papers total.")
    return all_papers


# ─── PubMed Fetch ─────────────────────────────────────────────────────────────

PUBMED_TERMS = (
    '(("2D materials"[tiab] OR "van der Waals"[tiab] OR "transition metal dichalcogenide"[tiab] '
    'OR "graphene"[tiab] OR "hexagonal boron nitride"[tiab] OR "MoS2"[tiab] OR "WSe2"[tiab] '
    'OR "WS2"[tiab] OR "MoSe2"[tiab] OR "moiré"[tiab] OR "twisted bilayer"[tiab] '
    'OR "NbSe2"[tiab] OR "black phosphorus"[tiab]) '
    'AND ("space radiation"[tiab] OR "radiation hardness"[tiab] OR "radiation damage"[tiab] '
    'OR "proton irradiation"[tiab] OR "total ionizing dose"[tiab] OR "exfoliation"[tiab] '
    'OR "Hall bar"[tiab] OR "GaN heterostructure"[tiab] OR "photodetector"[tiab] '
    'OR "tokamak"[tiab] OR "flux growth"[tiab] OR "vdW heterostructure"[tiab] '
    'OR "quantum Hall"[tiab] OR "correlated state"[tiab]))'
)


def fetch_pubmed(days_back: int) -> list[dict]:
    date_from = (datetime.date.today() - datetime.timedelta(days=days_back + 1)).strftime("%Y/%m/%d")
    date_to   = datetime.date.today().strftime("%Y/%m/%d")
    search_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&term={urllib.parse.quote(PUBMED_TERMS)}"
        f"&mindate={date_from}&maxdate={date_to}&datetype=pdat"
        "&retmax=80&retmode=json&tool=paperscout&email=scout@example.com"
    )
    print(f"  [PubMed] Searching (lookback={days_back}d)...")
    try:
        with urllib.request.urlopen(search_url, timeout=30) as resp:
            ids = json.loads(resp.read())["esearchresult"]["idlist"]
    except Exception as e:
        print(f"  [PubMed] Search ERROR: {e}")
        return []

    if not ids:
        print("  [PubMed] 0 papers found.")
        return []

    fetch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        f"?db=pubmed&id={','.join(ids)}&retmode=json&tool=paperscout&email=scout@example.com"
    )
    try:
        with urllib.request.urlopen(fetch_url, timeout=30) as resp:
            summaries = json.loads(resp.read())["result"]
    except Exception as e:
        print(f"  [PubMed] Fetch ERROR: {e}")
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
    print(f"  [PubMed] {len(papers)} papers fetched.")
    return papers


# ─── Semantic Scholar Fetch ───────────────────────────────────────────────────

SS_QUERIES = [
    "gold-assisted exfoliation 2D material monolayer wafer-scale",
    "radiation hardness 2D material graphene TMD space proton",
    "Hall sensor graphene encapsulated tokamak UV photodetector",
    "GaN graphene heterostructure 2D sensor harsh environment",
    "flux growth exfoliation van der Waals heterostructure device",
]


def fetch_semantic_scholar(days_back: int, skip_if_enough: int = 0) -> list[dict]:
    if skip_if_enough > 0:
        print(f"  [S2] Skipped — arXiv+PubMed returned {skip_if_enough} papers already.")
        return []

    papers_seen: set[str]  = set()
    all_papers:  list[dict] = []
    print(f"  [S2] {len(SS_QUERIES)} queries (fallback)...")

    for i, q in enumerate(SS_QUERIES):
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={urllib.parse.quote(q)}&limit=25"
            "&fields=paperId,title,abstract,authors,year,externalIds,publicationDate,venue"
        )
        data: dict = {}
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "PaperScout/3.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read())
                break
            except Exception as e:
                if "429" in str(e):
                    wait = 15 * (2 ** attempt)
                    print(f"  [S2] Rate limited — waiting {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"  [S2] Error: {e}")
                    break

        for p in (data.get("data") or []):
            pid = p.get("paperId", "")
            if pid in papers_seen:
                continue
            papers_seen.add(pid)
            doi     = (p.get("externalIds") or {}).get("DOI", "")
            url_out = f"https://doi.org/{doi}" if doi else f"https://www.semanticscholar.org/paper/{pid}"
            year    = p.get("year") or ""
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

    print(f"  [S2] {len(all_papers)} papers fetched.")
    return all_papers


# ─── Relevance Scorer ─────────────────────────────────────────────────────────

RADIATION_KWS = [
    "proton irradiation", "neutron irradiation", "gamma irradiation",
    "heavy ion irradiation", "alpha irradiation", "electron irradiation",
    "ion beam irradiation", "x-ray irradiation",
    "radiation hardness", "radiation hard", "radiation tolerance",
    "radiation damage", "radiation effect", "radiation-induced",
    "total ionizing dose", "tid effect", "displacement damage",
    "single event effect", "single-event", "see cross section",
    "rad-hard", "latchup", "single event upset",
    "space radiation", "space application", "space environment",
    "low earth orbit", "leo electronics", "geo orbit",
    "international space station", "iss experiment",
    "cosmic ray", "van allen belt",
]

FABRICATION_KWS = [
    "gold exfoliation", "gold-assisted exfoliation", "gold-mediated exfoliation",
    "gold mediated exfoliation", "gold-mediated", "gold mediated",
    "au-assisted exfoliation", "au exfoliation", "au-mediated exfoliation",
    "au substrate exfoliation", "gold substrate exfoliation",
    "metal-assisted exfoliation", "metal assisted exfoliation",
    "nickel-assisted exfoliation", "ni-assisted exfoliation",
    "palladium-assisted exfoliation", "pd-assisted exfoliation",
    "thermal release tape", "thermal tape exfoliation",
    "large-scale exfoliation", "wafer-scale exfoliation", "wafer-scale monolayer",
    "cm-scale monolayer", "inch-scale monolayer", "centimeter-scale monolayer",
    "exfoliation yield", "exfoliation efficiency",
    "adhesion energy exfoliation", "cleavage energy 2d",
    "layer-number control", "n-layer controlled", "layer-controlled exfoliation",
    "patterned exfoliation", "selective exfoliation",
    "2d crystal production", "monolayer production", "scalable exfoliation",
    "high-yield exfoliation", "batch exfoliation",
    "deterministic transfer", "dry transfer", "all-dry transfer",
    "viscoelastic stamp", "pdms stamp", "pc stamp", "ppc stamp",
    "polymer-free transfer", "clean transfer", "van der waals assembly",
    "heterostructure assembly", "layer stacking", "pick-up transfer",
    "stamping technique 2d", "flip-chip transfer",
    "hbn encapsulation", "hbn encapsulated", "boron nitride encapsulation",
    "encapsulated graphene", "encapsulated mos2", "encapsulated wse2",
    "dielectric encapsulation", "dual-gate encapsulation",
    "flux growth", "bulk crystal growth", "chemical vapor transport",
    "bridgman growth", "cvt growth", "single crystal 2d",
    "mechanical exfoliation", "scotch tape", "tape exfoliation",
]

DEVICE_KWS = [
    "hall bar", "hall sensor", "hall effect graphene", "hall effect 2d",
    "corbino disk", "corbino geometry", "magnetic field sensor",
    "tokamak", "plasma diagnostic", "fusion reactor", "iter",
    "uv photodetector", "uv detector", "deep uv", "ultraviolet detector",
    "interdigitated electrode", "interdigitated contact", "interdigitated fet",
    "transfer length method", "tlm measurement", "contact resistance 2d",
    "cafm", "conductive afm", "kelvin probe",
    "high mobility graphene", "high-mobility mos2",
    "gan graphene", "gan 2d", "gan tmd", "gan heterostructure",
    "algan gan", "algan", "nitride heterostructure",
    "extreme environment", "high temperature 2d", "cryogenic 2d sensor",
]

MATERIAL_KWS = [
    "van der waals", "2d material", "tmd", "mos2", "wse2", "ws2", "mose2",
    "mote2", "hexagonal boron nitride", "hbn", "graphene", "exfoliation",
    "hall effect", "uv detector", "raman", "photoluminescence",
    "monolayer", "bilayer", "few-layer", "transition metal dichalcogenide",
    "black phosphorus", "nbse2", "inse", "res2",
    "vdw heterostructure", "quantum hall", "afm", "tem",
    "single-layer", "cvd graphene", "epitaxial graphene",
    "transport measurement", "field effect transistor 2d",
    "exciton", "polariton", "twisted bilayer", "moiré",
    "nems", "resonator", "graphene membrane", "mechanical 2d",
]

LOWER_PRIORITY_KWS = [
    "magic angle", "correlated insulator",
    "mott insulator graphene", "wigner crystal 2d",
    "unconventional superconductor", "flat band",
]

NEGATIVE_KWS = [
    "perovskite solar", "perovskite led", "organic semiconductor",
    "cmos silicon", "battery cathode", "battery anode",
    "protein folding", "drug delivery", "genomics",
    "lithium ion battery", "supercapacitor electrode",
    "dye sensitized solar", "solid state battery",
]


def score_paper(paper: dict, extra_tracked_authors: list[str] | None = None) -> tuple[int, list[str]]:
    text  = (paper["title"] + " " + paper["abstract"] + " " +
             " ".join(paper["authors"])).lower()
    title = paper["title"].lower()
    score = 0
    signals: list[str] = []

    def hit(kw_list: list[str], points: int, tag: str, title_bonus: int = 4):
        nonlocal score
        for kw in kw_list:
            if kw in text:
                pts = points + (title_bonus if kw in title else 0)
                score += pts
                signals.append(f"{tag}:{kw}(+{pts})")

    hit(RADIATION_KWS,      18, "RAD",   title_bonus=5)
    hit(FABRICATION_KWS,    16, "FAB",   title_bonus=4)
    hit(DEVICE_KWS,         16, "DEV",   title_bonus=4)
    hit(MATERIAL_KWS,        5, "MAT",   title_bonus=3)
    hit(LOWER_PRIORITY_KWS,  2, "MOIRE", title_bonus=1)

    author_str = " ".join(paper["authors"]).lower()
    for a in HIGH_VALUE_AUTHORS:
        if a.lower() in author_str:
            score += 22
            signals.append(f"HV_AUTHOR:{a}(+22)")
    for a in ALL_TRACKED_AUTHORS:
        if a not in HIGH_VALUE_AUTHORS and a.lower() in author_str:
            score += 10
            signals.append(f"AUTHOR:{a}(+10)")
    # Extra authors from subscriber PI preferences
    if extra_tracked_authors:
        for a in extra_tracked_authors:
            if a.lower() in author_str and a not in HIGH_VALUE_AUTHORS and a not in ALL_TRACKED_AUTHORS:
                score += 10
                signals.append(f"SUB_AUTHOR:{a}(+10)")

    for kw in NEGATIVE_KWS:
        if kw in text:
            score -= 18
            signals.append(f"NEG:{kw}(-18)")

    has_device_or_rad = any(s.startswith(("RAD:", "DEV:")) for s in signals)
    has_moire         = any(s.startswith("MOIRE:") for s in signals)
    if has_moire and not has_device_or_rad:
        score = min(score, 35)
        signals.append("MOIRE_ONLY_CAP:35")

    if re.search(r'\breview\b|\bsurvey\b|\bperspective\b', title):
        score = min(score, 55)
        signals.append("REVIEW_CAP:55")

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

def _manage_url(email: str) -> str:
    base = CONFIG["pages_url"].rstrip("/")
    return f"{base}/?email={urllib.parse.quote(email)}"


def build_email_html(papers_by_source: dict, date_str: str, total: int,
                     days_used: int, subscriber_email: str = "") -> str:
    window_note = f"{days_used}-day window" if days_used > 1 else "past 24 hours"
    manage_link = _manage_url(subscriber_email) if subscriber_email else CONFIG["pages_url"]

    if total == 0:
        body_content = """
        <tr><td style="padding:40px 0; text-align:center; color:#64748b; font-size:15px;">
            <div style="font-size:48px; margin-bottom:16px;">🔭</div>
            <div style="font-weight:600; color:#1e1b4b; font-size:18px; margin-bottom:8px;">
                No relevant papers found
            </div>
            <div>Searched the past 6 months across arXiv, PubMed, and Semantic Scholar.<br>
            Try broadening your interests on the signup page.</div>
        </td></tr>"""
    else:
        body_content = ""
        for source, papers in papers_by_source.items():
            body_content += f"""
        <tr><td style="padding:18px 0 6px; font-size:13px; font-weight:700;
            color:#6c63ff; letter-spacing:0.08em; text-transform:uppercase;
            border-top:2px solid #f0eeff;">
            {source} — {len(papers)} paper{"s" if len(papers) != 1 else ""}
        </td></tr>"""
            for p in papers:
                authors_short = ", ".join(p["authors"][:4])
                if len(p["authors"]) > 4:
                    authors_short += f" +{len(p['authors'])-4} more"
                abstract_snip = (p["abstract"][:300] + "…") if len(p["abstract"]) > 300 else p["abstract"]
                score_color   = "#22c55e" if p["score"] >= 60 else ("#f59e0b" if p["score"] >= 35 else "#94a3b8")
                body_content += f"""
        <tr><td style="padding:12px 0 16px; border-bottom:1px solid #f1f5f9;">
            <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <a href="{p['url']}" style="font-size:15px; font-weight:600; color:#1e1b4b;
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
<title>PaperScout Digest</title></head>
<body style="margin:0; padding:0; background:#f8f7ff; font-family:'Georgia',serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f7ff; padding:32px 0;">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0"
    style="background:#ffffff; border-radius:16px;
    box-shadow:0 4px 32px rgba(108,99,255,0.10); overflow:hidden;">

    <tr><td style="background:linear-gradient(135deg,#1e1b4b 0%,#4f46e5 100%);
        padding:36px 40px 28px;">
        <div style="font-size:11px; color:#a5b4fc; letter-spacing:0.15em;
            text-transform:uppercase; margin-bottom:8px;">Hone Lab · Columbia University</div>
        <div style="font-size:26px; font-weight:700; color:#ffffff;
            letter-spacing:-0.02em;">PaperScout Digest</div>
        <div style="font-size:13px; color:#c7d2fe; margin-top:6px;">
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

    <tr><td style="background:#f8f7ff; padding:20px 40px; border-top:1px solid #e0e7ff;">
        <div style="font-size:11px; color:#94a3b8; text-align:center; line-height:1.8;">
            Powered by PaperScout v3 · arXiv · PubMed · Semantic Scholar<br>
            <a href="{manage_link}" style="color:#6c63ff;">Update interests / unsubscribe</a>
        </div>
    </td></tr>
</table>
</td></tr></table>
</body></html>"""


def build_email_text(papers_by_source: dict, date_str: str, days_used: int,
                     subscriber_email: str = "") -> str:
    window = f"{days_used}-day window"
    lines  = [f"PaperScout Digest — {date_str} ({window})", "=" * 60, ""]
    if not papers_by_source:
        lines += ["No relevant papers found in the past 6 months.", ""]
    else:
        for source, papers in papers_by_source.items():
            lines.append(f"[ {source} ]")
            for p in papers:
                lines += [
                    f"  {p['title']}",
                    f"  Score: {p['score']}%  |  {p['published']}",
                    f"  {', '.join(p['authors'][:3])}",
                    f"  {p['url']}",
                    "",
                ]
    manage_link = _manage_url(subscriber_email) if subscriber_email else CONFIG["pages_url"]
    lines += ["─" * 60, f"Update interests / unsubscribe: {manage_link}"]
    return "\n".join(lines)


# ─── Send via SendGrid ────────────────────────────────────────────────────────

def send_sendgrid(subject: str, html: str, text: str, recipients: list[dict]) -> None:
    """
    Send via SendGrid v3 REST API using only stdlib (no extra pip packages).
    recipients: list of {"email": "...", "name": "..."}
    """
    api_key = CONFIG["sendgrid_api_key"]
    if not api_key:
        raise RuntimeError("SENDGRID_API_KEY not set")

    payload = {
        "personalizations": [{"to": recipients}],
        "from": {"email": CONFIG["from_email"], "name": CONFIG["from_name"]},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text},
            {"type": "text/html",  "value": html},
        ],
    }
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
        print(f"  ✅ SendGrid: {status} — sent to {len(recipients)} recipient(s).")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"SendGrid error {e.code}: {body}") from e


# ─── Date helpers ─────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(date_str[:10])
    except Exception:
        return datetime.date.min


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(test_mode: bool = False, force_lookback: int | None = None) -> None:
    date_str = datetime.date.today().strftime("%B %d, %Y")

    # 1. Load subscribers
    subscribers = fetch_subscribers()
    if not subscribers and not test_mode:
        print("  ⚠ No active subscribers. Exiting.")
        return

    # 2. Build dynamic queries from subscriber interests
    extra_queries, extra_authors = build_dynamic_queries(subscribers)

    # 3. Determine lookback steps
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

    # 4. Fetch papers (one big window, then slice adaptively)
    seen_on_disk = load_seen_ids()
    print(f"\n🔭 PaperScout v3 — fetching full {max_step}-day window...")
    raw_all: list[dict] = []
    raw_all += fetch_arxiv(max_step, extra_queries=extra_queries)
    raw_all += fetch_pubmed(max_step)
    raw_all += fetch_semantic_scholar(
        max_step, skip_if_enough=len(raw_all) if len(raw_all) >= 30 else 0
    )

    # 5. Deduplicate
    novel = [p for p in raw_all if p["id"] not in seen_on_disk]
    title_seen: set[str] = set()
    deduped: list[dict] = []
    for p in novel:
        norm = re.sub(r'\W+', ' ', p["title"].lower()).strip()
        if norm and norm not in title_seen and len(p["title"]) > 10:
            title_seen.add(norm)
            deduped.append(p)

    # 6. Score
    for p in deduped:
        p["score"], p["signals"] = score_paper(p, extra_tracked_authors=extra_authors)

    print(f"  Fetched {len(raw_all)} total, {len(deduped)} novel after dedup.")

    # 7. Adaptive window slice
    today   = datetime.date.today()
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
        print(f"  {days:>3}d window → {len(window_papers)} relevant (score≥{CONFIG['min_score']})")
        if len(window_papers) >= CONFIG["min_papers"] or days >= max_step:
            relevant = window_papers
            if len(window_papers) >= CONFIG["min_papers"]:
                print(f"  ✓ Target met ({CONFIG['min_papers']}+).")
            else:
                print(f"  ⚠ Max lookback reached — {len(relevant)} paper(s).")
            break
        relevant = window_papers

    # 8. Cap and group by source
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
        f"📡 PaperScout: No new papers [{days_used}d] — {date_str}"
        if total == 0
        else f"📡 PaperScout: {total} paper{'s' if total != 1 else ''} [{days_used}d] — {date_str}"
    )

    # 9. Send or print
    if test_mode:
        print("\n" + "─" * 60)
        # Use first subscriber email for preview link, or empty string
        preview_email = subscribers[0]["email"] if subscribers else ""
        print(build_email_text(by_source, date_str, days_used, preview_email))
        print("─" * 60)
        preview_path = Path(__file__).parent / "digest_preview.html"
        preview_path.write_text(
            build_email_html(by_source, date_str, total, days_used, preview_email)
        )
        print(f"\n  📄 HTML preview → {preview_path}")
    else:
        # Send individual email to each subscriber (preserves privacy)
        for sub in subscribers:
            html = build_email_html(by_source, date_str, total, days_used, sub["email"])
            text = build_email_text(by_source, date_str, days_used, sub["email"])
            send_sendgrid(subject, html, text, [{"email": sub["email"], "name": sub.get("name", "")}])
            time.sleep(0.5)  # gentle rate limiting

    # 10. Persist seen IDs
    all_fetched_ids = {p["id"] for p in deduped}
    save_seen_ids(seen_on_disk | all_fetched_ids)
    print(f"\n✅ Done. {total} papers in digest (lookback={days_used}d). "
          f"{len(all_fetched_ids)} IDs saved.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hone Lab PaperScout v3")
    parser.add_argument("--test",     action="store_true", help="Dry-run, no email")
    parser.add_argument("--lookback", type=int, default=None, help="Force starting lookback (days)")
    args = parser.parse_args()
    main(test_mode=args.test, force_lookback=args.lookback)
