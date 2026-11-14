#!/usr/bin/env python3
"""Brokerage Brief: fetch industry RSS feeds, sort stories into sections, build a static site.

Usage:
  python build.py                 # fetch all feeds, merge into data/articles.json, build site/
  python build.py --no-fetch      # rebuild site/ from data/articles.json only
  python build.py --seed FILE     # merge items from a JSON file (list of {source,title,link,published,summary})

Standard library only, so it runs on any Python 3.9+ (including GitHub Actions) with no installs.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FEEDS_FILE = ROOT / "feeds.json"
DATA_FILE = ROOT / "data" / "articles.json"
STATUS_FILE = ROOT / "data" / "feed_status.json"
TEMPLATE = ROOT / "templates" / "index.html"
OUT_DIR = ROOT / "site"

KEEP_DAYS = 60          # how long stories stay on the site
MAX_ARTICLES = 4000     # hard cap on stored stories
SUMMARY_CHARS = 280
UA = "Mozilla/5.0 (compatible; BrokerageBriefBot/1.0; RSS reader)"

# --------------------------------------------------------------------------------------
# Sections. Order matters: on a tie in score, the earlier section wins.
# Each rule is (regex, weight). Title matches count 3x, summary matches 1x.
# --------------------------------------------------------------------------------------
SECTIONS = [
    {"id": "people", "name": "People Moves", "blurb": "Hires, appointments, promotions and departures across brokers, banks and wealth firms."},
    {"id": "tech", "name": "Technology & Infrastructure", "blurb": "Trading platforms, liquidity and connectivity, market data, cloud, AI and the vendors behind them."},
    {"id": "regulation", "name": "Regulation & Compliance", "blurb": "Licences, enforcement, fines and rule changes from regulators worldwide."},
    {"id": "deals", "name": "Deals & Corporate", "blurb": "M&A, funding, results, trading volumes and expansion into new markets."},
    {"id": "wealth", "name": "Wealth Management", "blurb": "Advisers, RIAs, private banks, family offices and wealth platforms."},
    {"id": "digital", "name": "Digital Assets", "blurb": "Crypto brokerage, tokenisation, stablecoins and exchanges crossing into traditional finance."},
    {"id": "markets", "name": "Markets & Products", "blurb": "New instruments, product launches and other industry news."},
]

RULES = {
    "people": [
        (r"\bappoint(s|ed|ment)?\b", 4), (r"\bpeople moves\b|\bcareer moves\b|\bexecutive moves\b", 6), (r"\bhires?\b|\bhired\b|\bhiring\b", 4),
        (r"\bjoins\b(?! forces)", 3), (r"\bnames\b|\bnamed\b", 2), (r"\bpromot(es|ed|ion)\b", 3),
        (r"\bsteps? down\b|\bstepping down\b|\bresign(s|ed|ation)?\b|\bretir(es|ed|ing)\b|\breplac(es|ed|ing)\b|\bchange (chairman|chair|CEO)\b|\bdies\b", 4),
        (r"\bdepart(s|ure)\b|\bexits\b|\bleaves\b|\bpoach(es|ed)?\b|\bousted\b|\bsucceed(s|ed)?\b|\bsuccessor\b", 3),
        (r"\b(new|interim) (ceo|cfo|coo|cto|cio|chair(man|woman)?|president|head)\b", 4),
        (r"\b(chief [a-z]+ officer|managing director|general manager|head of|vice president|board of directors|non-executive)\b", 2),
        (r"\b(veteran|ex-[A-Za-z]+)\b", 1), (r"\btaps\b|\bbolsters? (team|leadership|board)\b|\badds? .{0,30}\b(team|executive|director)\b", 2),
    ],
    "tech": [
        (r"\btechnolog(y|ies)\b|\btech\b|\bfintech\b", 2), (r"\bplatforms?\b", 2), (r"(?-i:\bAPIs?\b|\bSDK\b)", 3),
        (r"\bcloud\b|\bAWS\b|\bAzure\b|\bdata cent(er|re)\b|\bcolocation\b", 3),
        (r"(?-i:\bAI\b|\bLLMs?\b)|\bartificial intelligence\b|\bmachine learning\b|\bLLM\b|\bgenerative\b|\bagentic\b", 2),
        (r"\bliquidity (bridge|aggregat|provider|hub)|\bbridge\b|\bconnectivity\b|\blow[- ]latency\b|(?-i:\bFIX\b)", 3),
        (r"\binfrastructure\b|\bmarket data\b|\bdata feed\b|\banalytics\b|\bsoftware\b|\bSaaS\b|\bvendor\b", 3),
        (r"\bMT4\b|\bMT5\b|\bMetaTrader\b|\bcTrader\b|\bTradingView\b|\bDXtrade\b|\bMatchTrader\b|(?-i:\bOMS\b|\bEMS\b)|\bmatching engine\b", 4),
        (r"\boutages?\b|\bcyber(security|attack)?\b|\bhack(ed)?\b|\bdowntime\b", 3),
        (r"\bintegrat(es|ed|ion)\b|\bautomat(es|ed|ion)\b|\bdigitis|\bdigitiz|\bwidget\b|\bapp\b|\bdesktop\b|\bworkflow\b", 2),
        (r"\b(selects?|adopts?|deploys?|rolls? out|implements?|powers?|teams? up with|partners? with)\b", 1),
        (r"\b(Bloomberg|Refinitiv|LSEG|FactSet|Broadridge|SS&C|FIS|Murex|Calypso|Ion|smartTrade|Integral|PrimeXM|oneZero|Celoxica|Finalto|Devexperts|Gold-i|Centroid|Leverate|B2Broker|Tools for Brokers|Spotware|MetaQuotes)\b", 3),
    ],
    "regulation": [
        (r"(?-i:\b(SEC|CFTC|FINRA|FCA|ESMA|CySEC|ASIC|FSCA|DFSA|VARA|SCA|ADGM|FSRA|MAS|SFC|FSA|BaFin|AMF|CONSOB|CNMV|FMA|IIROC|CIRO|OSC|NFA|SEBI|CSRC|FINMA|CMA|JFSA|FSC)\b)|\bcentral bank\b", 3),
        (r"\blicen[cs](e|ed|es|ing)\b|\bauthori[sz](ed|ation)\b|\bregistration\b|\bapproval\b", 3),
        (r"\bfine[sd]?\b|\bpenalt(y|ies)\b|\benforcement\b|\bsanction(s|ed)?\b|\bcharged\b|\bsentenced\b|\bfraud\b|\bscam\b|\bbanned?\b", 4),
        (r"\bregulat(or|ors|ory|ion|ions|ed)\b|\bcompliance\b|\brules?\b|\brulemaking\b|\bconsultation\b|\bproposes\b|\bamendments?\b|\bexemptive\b|\bguidance\b", 2),
        (r"\bMiFID\b|\bMiCA\b|\bEMIR\b|\bDodd-Frank\b|\bConsumer Duty\b|(?-i:\bAML\b|\bKYC\b)|\bT\+1\b|\bclearing requirement", 3),
        (r"\blawsuit\b|\bsues?\b|\bsued\b|\bcourt\b|\blitigation\b|\bsettle(s|d|ment)\b|\bwarning\b|\bblacklist", 3),
        (r"\bSenate\b|\bCongress\b|\bnominations?\b|\bcommissioner\b|\blegislation\b|\bbill\b", 1),
    ],
    "deals": [
        (r"\bacquir(es|ed|ing|er)\b|\bacquisition\b|\bmerg(er|es|ed|e)\b|\bbuys\b|\btakeover\b|\bdeal\b|\bstake\b|\bdivest", 4),
        (r"(?-i:\bIPO\b)|\blisting\b|\bfunding\b|\braises?\b|\bSeries [A-E]\b|\bvaluation\b|\binvests? in\b|\binvestment in\b|\bbacked\b", 3),
        (r"\brevenues?\b|\bprofits?\b|\bearnings\b|\bresults\b|(?-i:\bQ[1-4]\b|\bH[12]\b)|\bquarter(ly)?\b|\bnet income\b|\bloss\b", 3),
        (r"\bvolumes?\b|\bmetrics\b|\bclient (assets|accounts)\b|\bDARTs\b|\bAUM\b|\bturnover\b", 2),
        (r"\bexpands?\b|\bexpansion\b|\benters\b|\blaunches in\b|\bnew office\b|\bopens\b|\bscraps\b|\bshuts?\b|\bwinds? down\b|\blayoffs?\b|\bjob cuts\b|\brestructur", 2),
    ],
    "wealth": [
        (r"\bwealth\b|\bprivate bank(ing|er|s)?\b|\bfamily offices?\b|(?-i:\bU?HNW\b)", 4),
        (r"\badvis[eo]rs?\b|(?-i:\bRIAs?\b)|\bfinancial planner|\bfinancial planning\b|\bfiduciary\b|\bbreakaway\b|\bindependent broker-dealer\b", 3),
        (r"\basset manag(er|ers|ement)\b|\bfund(s)?\b|(?-i:\bETFs?\b)|\bpension\b|\bretirement\b|\b401\(k\)\b|\bannuit", 1),
        (r"\b(LPL|Raymond James|Edward Jones|Ameriprise|Morgan Stanley Wealth|Merrill|UBS|Julius Baer|Pictet|Lombard Odier|Coutts|St\. James|Rathbones|Quilter|Wells Fargo Advisors|Cetera|Osaic|Commonwealth)\b", 2),
    ],
    "digital": [
        (r"\bcrypto(currency|currencies)?\b|\bbitcoin\b|(?-i:\bBTC\b|\bETH\b)|\bethereum\b|\bsolana\b|\bXRP\b", 4),
        (r"\bstablecoins?\b|\btokeni[sz](ed|ation|e)\b|\btokens?\b|\bblockchain\b|\bdigital assets?\b|\bDeFi\b|\bweb3\b|\bon-chain\b|(?-i:\bDLT\b)", 4),
        (r"\b(Coinbase|Binance|Kraken|Payward|Coincheck|Bybit|OKX|Gemini|Crypto\.com|Bitstamp|Circle|Tether|Robinhood Crypto)\b", 3),
        (r"\bprediction markets?\b|\bKalshi\b|\bPolymarket\b", 2),
    ],
    "markets": [
        (r"\blaunch(es|ed)?\b|\bintroduc(es|ed)\b|\bunveils?\b|\bnew (product|service|offering|instrument)\b", 2),
        (r"\boptions\b|\bfutures\b|\bCFDs?\b|\bspread betting\b|\bcommodit(y|ies)\b|\bgold\b|\bindices\b|\bzero[- ]commission\b|\bfractional\b", 2),
        (r"\baward(s|ed)?\b|\bconference\b|\bsummit\b|\bevent\b|\bsurvey\b|\breport\b|\bstudy\b", 1),
    ],
}

ASSETS = {
    "Equities": r"\bstocks?\b|\bequit(y|ies)\b|\bshares?\b|\bETFs?\b|\bNYSE\b|\bNasdaq\b|\bLSE\b|\bexchanges?\b|\boptions\b|\bbroker-dealers?\b|\bsecurities\b|\bclearing\b|\bcustody\b|\bIPO\b|\bSEC\b|\bFINRA\b|\bRobinhood\b|\bSchwab\b|\bInteractive Brokers\b|\bIBKR\b|\beToro\b|\bSaxo\b|\bTrading 212\b|\bWebull\b|\bmoomoo\b|\bFutu\b|\bTiger Brokers\b",
    "FX & CFD": r"\bFX\b|\bforex\b|\bcurrenc(y|ies)\b|\bCFDs?\b|\bspot\b|\bmargin trading\b|\bleverage\b|\bMT[45]\b|\bMetaTrader\b|\bcTrader\b|\bprop(rietary)? (trading|firm)s?\b|\bprop firms?\b|\bliquidity provider|\bretail broker|\bspread betting\b|\bIG Group\b|\bPlus500\b|\bCMC Markets\b|\bOANDA\b|\bPepperstone\b|\bIC Markets\b|\bExness\b|\bXM\b|\bFXCM\b|\bAvaTrade\b|\bTickmill\b|\bCySEC\b|\bOTC derivatives\b|\bLMAX\b|\bCboe FX\b|\bEBS\b|\bFXall\b|\bRMB\b|\brenminbi\b",
    "Wealth": r"\bwealth\b|\badvis[eo]rs?\b|\bRIAs?\b|\bprivate bank|\bfamily office|\bfinancial planner|\bretirement\b|\bpension\b|\basset manag",
    "Crypto": r"\bcrypto|\bbitcoin\b|\bstablecoin|\btokeni[sz]|\bblockchain\b|\bdigital assets?\b|\bethereum\b|\bsolana\b",
}

REGIONS = {
    "Americas": r"\bUS\b|\bU\.S\.|\bUSA\b|\bUnited States\b|\bAmerica(n)?\b|\bNew York\b|\bWall Street\b|\bNYSE\b|\bNasdaq\b|\bCME\b|\bCboe\b|\bDTCC\b|\bTreasur(y|ies)\b|\bChicago\b|\bCanada\b|\bCanadian\b|\bToronto\b|\bBrazil\b|\bMexico\b|\bLatin America\b|\bLatAm\b|\bArgentina\b|\bChile\b|\bColombia\b|\bSEC\b|\bCFTC\b|\bFINRA\b|\bNFA\b|\bCIRO\b|\bSenate\b|\bLouisiana\b|\bArkansas\b|\bFed\b",
    "UK & Europe": r"\bUK\b|\bU\.K\.|\bBritain\b|\bBritish\b|\bEngland\b|\bLondon\b|\bEurope(an)?\b|\bEU\b|\bEurozone\b|\bGermany\b|\bGerman\b|\bFrance\b|\bFrench\b|\bParis\b|\bSpain\b|\bItaly\b|\bNetherlands\b|\bAmsterdam\b|\bSwitzerland\b|\bSwiss\b|\bZurich\b|\bGeneva\b|\bIreland\b|\bDublin\b|\bLuxembourg\b|\bCyprus\b|\bCypriot\b|\bLimassol\b|\bFrankfurt\b|\bDeutsche B[öo]rse\b|\bDAX\b|\bEuronext\b|\bEurex\b|\bSIX\b|\bLSEG\b|\bMalta\b|\bPoland\b|\bNordic\b|\bSweden\b|\bNorway\b|\bDenmark\b|\bFinland\b|\bFCA\b|\bESMA\b|\bCySEC\b|\bBaFin\b|\bAMF\b|\bCONSOB\b|\bFINMA\b|\bMiFID\b|\bMiCA\b|\bBank of England\b|\bBofE\b|\bHouse of Lords\b|\bWero\b",
    "Middle East & Africa": r"\bUAE\b|\bDubai\b|\bAbu Dhabi\b|\bSaudi\b|\bRiyadh\b|\bQatar\b|\bDoha\b|\bBahrain\b|\bKuwait\b|\bOman\b|\bMiddle East\b|\bMENA\b|\bGCC\b|\bGulf\b|\bJordan\b|\bEgypt\b|\bIsrael\b|\bTurkey\b|\bAfrica(n)?\b|\bSouth Africa\b|\bNigeria\b|\bKenya\b|\bMauritius\b|\bSeychelles\b|\bDFSA\b|\bVARA\b|\bSCA\b|\bADGM\b|\bFSRA\b|\bFSCA\b|\bCMA\b",
    "Asia Pacific": r"\bAsia(n)?\b|\bAPAC\b|\bChina\b|\bChinese\b|\bBeijing\b|\bShanghai\b|\bHong Kong\b|\bHKEX\b|\bSGX\b|\bASX\b|\bJPX\b|\be-HKD\b|\bSingapore\b|\bJapan(ese)?\b|\bTokyo\b|\bKorea(n)?\b|\bIndia(n)?\b|\bMumbai\b|\bAustralia(n)?\b|\bSydney\b|\bNew Zealand\b|\bTaiwan\b|\bThailand\b|\bVietnam\b|\bIndonesia\b|\bMalaysia\b|\bPhilippines\b|\bASIC\b|\bMAS\b|\bSFC\b|\bSEBI\b|\bCSRC\b|\bJFSA\b|\bNomura\b|\bRMB\b|\brenminbi\b",
}

# Stories from broad feeds (filter:true) must match at least one of these to be kept.
INDUSTRY_FILTER = re.compile(
    r"broker|brokerage|exchange|trading|trader|custody|wealth|asset manag|fund|ETF|regulat|licen[cs]e|SEC\b|CFTC|FCA|tokeni|stablecoin|institution|bank|prime|liquidity|platform|hires?|appoint|CEO",
    re.I,
)

COMPILED = {k: [(re.compile(p, re.I), w) for p, w in v] for k, v in RULES.items()}


def smart_compile(pattern: str) -> re.Pattern:
    """Case-insensitive, except alternatives that are acronyms (US, SEC, FX...) stay case-sensitive."""
    parts, depth, cur = [], 0, ""
    for ch in pattern:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    out = []
    for p in parts:
        word = re.sub(r"\\b|\\.|[^A-Za-z]", "", p)
        out.append(f"(?-i:{p})" if word and len(word) <= 5 and word.upper() == word else p)
    return re.compile("|".join(out), re.I)


ASSET_RX = {k: smart_compile(v) for k, v in ASSETS.items()}
REGION_RX = {k: smart_compile(v) for k, v in REGIONS.items()}


def classify(title: str, summary: str, feed: dict) -> dict:
    scores = {}
    for sec in SECTIONS:
        sid = sec["id"]
        s = 0
        for rx, w in COMPILED[sid]:
            if rx.search(title):
                s += w * 3
            if rx.search(summary):
                s += w
        if feed.get("section") == sid:
            s += 4
        scores[sid] = s
    # People moves need a title signal; avoid "CEO says..." stories landing there.
    if not any(rx.search(title) for rx, _ in COMPILED["people"]):
        scores["people"] = min(scores["people"], 2)
    order = [s["id"] for s in SECTIONS]
    best = max(order, key=lambda k: (scores[k], -order.index(k)))
    if scores[best] < 4:
        best = feed.get("section") or "markets"
    # A story can also be listed in a second section when it scores strongly there too
    # (e.g. a trading-tech vendor being acquired shows in Deals and in Technology).
    also = [k for k in order if k != best and k != "markets" and scores[k] >= 9 and scores[k] >= 0.6 * scores[best]][:1]

    text = f"{title} {summary}"
    assets = [a for a, rx in ASSET_RX.items() if rx.search(text)]
    if not assets and feed.get("asset"):
        assets = [feed["asset"]]
    if best == "wealth" and "Wealth" not in assets:
        assets.append("Wealth")
    if best == "digital" and "Crypto" not in assets:
        assets.append("Crypto")

    regions = [r for r, rx in REGION_RX.items() if rx.search(text)]
    if not regions:
        regions = [feed.get("region") or "Global"]
    if "Global" in regions and len(regions) > 1:
        regions.remove("Global")
    return {"section": best, "also": also, "assets": assets, "regions": regions}


# --------------------------------------------------------------------------------------
# Fetching and parsing
# --------------------------------------------------------------------------------------
TAG_RX = re.compile(r"<[^>]+>")
WS_RX = re.compile(r"\s+")


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(html.unescape(s))
    s = TAG_RX.sub(" ", s)
    s = WS_RX.sub(" ", s).strip()
    s = re.sub(r"\s*(The post .* appeared first on .*|Continue reading.*|Read more.*)$", "", s, flags=re.I)
    return s


def truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:-") + "…"


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                    "%a, %d %b %Y - %H:%M", "%A, %B %d, %Y - %H:%M"):
            try:
                d = datetime.strptime(s.replace("Z", "+00:00"), fmt)
                break
            except ValueError:
                continue
    if d is None:
        m = re.search(r"(\d{1,2}) (\w{3})\w* (\d{4})", s)
        if m:
            try:
                d = datetime.strptime(" ".join(m.groups()), "%d %b %Y")
            except ValueError:
                d = None
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc) if d else None


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(el, *names) -> str | None:
    for c in el:
        if local(c.tag) in names:
            if c.text and c.text.strip():
                return c.text
            if local(c.tag) == "link" and c.get("href"):
                return c.get("href")
    return None


IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)
IMG_TAG = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def find_image(e) -> str:
    """Best picture for a feed entry: media:content / media:thumbnail / enclosure / featured-image / first <img>."""
    best, best_w = "", -1
    for c in e.iter():
        name = local(c.tag)
        url = c.get("url") or c.get("href") or ""
        if name in ("content", "thumbnail") and url and (c.get("medium") == "image" or (c.get("type") or "").startswith("image") or IMG_EXT.search(url) or name == "thumbnail"):
            try:
                w = int(c.get("width") or 0)
            except ValueError:
                w = 0
            if w > best_w:
                best, best_w = url, w
        elif name == "enclosure" and url and ((c.get("type") or "").startswith("image") or IMG_EXT.search(url)):
            if best_w < 0:
                best, best_w = url, 0
        elif name in ("featured-image", "image") and not best:
            val = url or (c.text or "").strip()
            if val.startswith("http"):
                best = val
    if not best:
        for c in e:
            if local(c.tag) in ("encoded", "description", "content", "summary") and c.text:
                m = IMG_TAG.search(html.unescape(c.text) if "&lt;img" in c.text else c.text)
                if m:
                    best = m.group(1)
                    break
    best = html.unescape(best.strip())
    if best.startswith("//"):
        best = "https:" + best
    if not best.startswith("https://") or re.search(r"(feedburner|pixel|tracking|1x1|gravatar|spacer)", best, re.I):
        return ""
    return best


def parse_feed(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items = []
    entries = [e for e in root.iter() if local(e.tag) in ("item", "entry")]
    for e in entries:
        link = None
        for c in e:
            if local(c.tag) == "link":
                if c.get("href") and c.get("rel", "alternate") == "alternate":
                    link = c.get("href")
                    break
                if c.text and c.text.strip():
                    link = c.text.strip()
                    break
        link = link or child_text(e, "guid", "id")
        items.append({
            "title": clean_text(child_text(e, "title")),
            "link": (link or "").strip(),
            "published": child_text(e, "pubdate", "published", "updated", "date", "issued"),
            "summary": clean_text(child_text(e, "description", "summary", "encoded", "content")),
            "image": find_image(e),
        })
    return items


def fetch(feed: dict) -> tuple[dict, list[dict], str]:
    try:
        req = urllib.request.Request(feed["url"], headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read()
        items = parse_feed(body)
        pics = sum(1 for i in items if i.get("image"))
        return feed, items, f"ok ({len(items)} items, {pics} with pictures)"
    except Exception as ex:  # noqa: BLE001 - a broken feed must never stop the build
        return feed, [], f"error: {type(ex).__name__}: {str(ex)[:120]}"


def make_article(raw: dict, feed: dict, now: datetime) -> dict | None:
    title, link = raw.get("title", "").strip(), raw.get("link", "").strip()
    if not title or not link.startswith("http"):
        return None
    summary = clean_text(raw.get("summary", ""))
    if summary.lower().startswith(title.lower()):
        summary = summary[len(title):].strip(" -–:")
    if feed.get("filter") and not INDUSTRY_FILTER.search(f"{title} {summary}"):
        return None
    pub = parse_date(raw.get("published")) or now
    if pub > now + timedelta(hours=2):
        pub = now
    tags = classify(title, summary, feed)
    return {
        "id": hashlib.sha1(link.split("?")[0].rstrip("/").encode()).hexdigest()[:12],
        "title": title,
        "link": link,
        "source": feed["name"],
        "published": pub.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": truncate(summary, SUMMARY_CHARS),
        "image": raw.get("image", ""),
        **tags,
    }


def title_key(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower())[:70]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def merge(existing: list[dict], new: list[dict], now: datetime) -> list[dict]:
    by_id = {a["id"]: a for a in existing}
    seen_titles = {title_key(a["title"]): a["id"] for a in existing}
    for a in new:
        tk = title_key(a["title"])
        if a["id"] in by_id:
            a["published"] = by_id[a["id"]]["published"]  # keep first-seen date stable
            a["image"] = a.get("image") or by_id[a["id"]].get("image", "")
            by_id[a["id"]] = a
        elif tk in seen_titles:
            continue  # same story syndicated elsewhere
        else:
            by_id[a["id"]] = a
            seen_titles[tk] = a["id"]
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = [a for a in by_id.values() if a["published"] >= cutoff]
    out.sort(key=lambda a: a["published"], reverse=True)
    return out[:MAX_ARTICLES]


def build_site(articles: list[dict], feeds: list[dict], status: dict, now: datetime) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sections": SECTIONS,
        "assets": list(ASSETS.keys()),
        "regions": list(REGIONS.keys()) + ["Global"],
        "sources": [{"name": f["name"], "url": f["url"], "status": status.get(f["name"], "not fetched")} for f in feeds if f.get("enabled", True)],
        "articles": articles,
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", data)
    out = OUT_DIR / "index.html"
    out.write_text(page, encoding="utf-8")
    (OUT_DIR / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--seed", type=Path)
    args = ap.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    feeds = [f for f in load_json(FEEDS_FILE, {"feeds": []})["feeds"] if f.get("enabled", True)]
    feed_by_name = {f["name"]: f for f in feeds}
    existing = load_json(DATA_FILE, [])
    status = load_json(STATUS_FILE, {})
    new: list[dict] = []

    if args.seed:
        for raw in load_json(args.seed, []):
            feed = feed_by_name.get(raw.get("source"), {"name": raw.get("source", "Unknown")})
            art = make_article(raw, feed, now)
            if art:
                new.append(art)
            status[feed["name"]] = "ok (seed)"

    if not args.no_fetch and not args.seed:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for feed, items, msg in pool.map(fetch, feeds):
                status[feed["name"]] = msg
                print(f"{feed['name']:<24} {msg}")
                for raw in items:
                    art = make_article(raw, feed, now)
                    if art:
                        new.append(art)

    articles = merge(existing, new, now)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(articles, ensure_ascii=False, indent=1), encoding="utf-8")
    STATUS_FILE.write_text(json.dumps(status, indent=1), encoding="utf-8")
    out = build_site(articles, feeds, status, now)
    counts = {}
    for a in articles:
        counts[a["section"]] = counts.get(a["section"], 0) + 1
    print(f"Built {out} with {len(articles)} stories: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
