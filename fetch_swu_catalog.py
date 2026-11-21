#!/usr/bin/env python3
"""Regenerates app/src/main/assets/swu_catalog.json — the bundled card catalog.

The app treats swu-db as its card database: this snapshot is what ships in the APK (first run /
offline); the in-app sync (data/CatalogSync.kt) then keeps a fresher copy in filesDir using the
same schema. Run this by hand when a new set is announced/released:

    python3 tools/fetch_swu_catalog.py

Stdlib only. Sources (no auth): https://api.swu-db.com  — /sets, /cards/{set} (every printing of a
set, with its VariantType), /cards/{set}/{n} and, only when the API can't serve a set (LAWOP was
missing from it in 2026-09), the swu-db.com website's search table.

What ends up in the catalog
- MAIN sets     : full expansions (no " - " in the name, >= 150 cards, not Intro Battle). Their plain-
                  numbered Normal printings are the cards; every other printing (hyperspace, foil,
                  showcase, prestige…) is a variant. Unreleased sets are included with their release
                  date; the app hides them until that day.
- TWIN_SUNS     : Twin Suns precon exclusives ("Twin Suns 2026") — collected like an expansion.
- PRODUCT       : other boxed products with cards of their own (Intro Battle: Hoth) — likewise.
- PROMO         : every other set (yearly promos, conventions, prerelease, judge, Store Showdown,
                  movie promos…) — variants only, no cards of their own.
- WEEKLY_PLAY   : the per-expansion "OP Promo" sets — 20 non-foil cards (numbers 1..20) plus foil twins.
                  swu-db lists a twin in one of two shapes: at number+20 (Jump to Lightspeed on: all 20
                  cards) or as an `F`-suffixed row at the SAME number (`SOROP/012F` — Spark, Shadows and
                  Twilight, where only some cards have a foil). One catalog entry per card with
                  `foil: true/false` — true = swu-db lists a foil twin; false = it doesn't, i.e. non-foil only.
                  (Both shapes were checked against swudb.com's per-printing data: an exact match.)
                  The app still offers a foil toggle on every promo.
Official fallback (the card feed)
swu-db can lag a release by weeks (Icons 2027 came out with 59 of its 194 cards there). So for every
recent set — released in the last OFFICIAL_DAYS, upcoming, or listed by swu-db with no cards yet — the
official card database (admin.starwarsunlimited.com, the API behind starwarsunlimited.com/cards) fills
in whatever swu-db is missing: cards and variants by collector number, **swu-db always wins** a
number it has. Their art is mirrored once into --art-dir (WebP, 700px) and served from --art-base, so
phones never touch the official site; once swu-db has a card, its own art is used and the mirrored
file is pruned. A set swu-db doesn't list at all is added only when catalog_rules.json `releaseDates`
gives its release date (the official API has none).

The nightly GitHub Actions job in the card feed repo (tools/catalog_feed/) runs this with --art-dir
and publishes the result; the app syncs from that feed first and swu-db second (CardDatabase.kt).
Run by hand, it writes the bundled asset and points fallback art at the feed's mirror.

Card identity is always `SET-NNN` (collector number), never the name. A variant's is `SET-` + its
number zero-padded to 3 with any suffix kept (`SOR-087F`, `SS1-002-C`); `base` is the card it is a
version of, worked out by `link_variants` (a mirror of VariantLinker.kt) or null when it can't be.
"""
from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

API = "https://api.swu-db.com"
SITE = "https://www.swu-db.com"
OUT = Path(__file__).resolve().parent.parent / "app/src/main/assets/swu_catalog.json"

# Curated corrections live in ONE file shared with the app (see CatalogRules.kt), so the bundled
# snapshot and the in-app sync always apply the same rules.
def _rules_path() -> Path:
    """--rules wins (the feed repo keeps the file beside this script); else the app's asset."""
    argv = sys.argv[1:]
    if "--rules" in argv and argv.index("--rules") + 1 < len(argv):
        return Path(argv[argv.index("--rules") + 1])
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "app/src/main/assets/catalog_rules.json", here / "catalog_rules.json"):
        if candidate.exists():
            return candidate
    raise SystemExit("catalog_rules.json not found — pass --rules")


RULES = json.loads(_rules_path().read_text())
EXCLUDE_MAIN = set(RULES["excludeMainSets"])   # sets left out entirely
MIN_MAIN_CARDS = RULES["minMainCards"]
TWIN_SUNS = re.compile(RULES.get("twinSunsPattern", r"^Twin Suns\b"), re.I)
PRODUCT = re.compile(RULES.get("productPattern", r"^Intro Battle\b"), re.I)
RARITY = {"Common": "C", "Uncommon": "U", "Rare": "R", "Legendary": "L", "Special": "S"}

OFFICIAL_API = "https://admin.starwarsunlimited.com/api/card-list"
FEED_ART_BASE = "https://ellismiller.github.io/unlimited-card-feed/art/"
# The official database's variant names -> swu-db's (checked on ASH/SEC: same numbers, these words).
# Anything unlisted is kept as-is; a variant with no type at all is labelled "Variant" until swu-db has it.
OFFICIAL_LABELS = {"Standard Prestige": "Prestige", "Foil Prestige": "Prestige Foil", "Serialized Prestige": "Prestige Serialized"}
OFFICIAL_DAYS = 180          # how far back a set still gets checked against the official database
ART_WIDTH = 700              # mirrored art: wide enough for the app's 900px-capped detail view, ~60 KB as WebP

def http_get(url: str, tries: int = 4, timeout: int = 40) -> str:
    return http_bytes(url, tries, timeout).decode("utf-8")


def http_bytes(url: str, tries: int = 4, timeout: int = 40) -> bytes:
    last = None
    for attempt in range(tries):
        try:
            # The official API and its CDN refuse non-browser agents.
            agent = "Mozilla/5.0 (compatible; UnlimitedDamage-catalog/2)" if "starwarsunlimited.com" in url else "UnlimitedBinder-catalog/1"
            req = urllib.request.Request(url, headers={"User-Agent": agent})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:  # network blips / 502s (swu-db does 502 sometimes)
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def api_json(path: str):
    return json.loads(http_get(API + path))


def clean_name(card_id: str, name: str) -> str:
    name = html.unescape(name).strip()
    name = re.sub(r"(?<=[A-Za-z])0(?![0-9])", "O", name)  # C-3P0 -> C-3PO, B2EM0 -> B2EMO
    return RULES["nameFixes"].get(card_id, name)


def display_name(name: str) -> str:
    return name.replace("Jump To", "Jump to")  # swu-db capitalises it inconsistently


def card_id(set_code: str, number: int) -> str:
    return f"{set_code}-{number:03d}"


def flat(values) -> list[str]:
    """swu-db list fields come back as plain strings from /cards/{set}/{n} but DynamoDB-wrapped
    (`{"S": "Aggression"}`) from /cards/search — accept both. The app's parser does the same."""
    out = []
    for v in values or []:
        if isinstance(v, dict):
            v = v.get("S")
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out


def to_card(raw: dict, set_code: str, number: int, *, art: str | None = None, fixes: bool = True) -> dict:
    """`fixes=False` for a variant's stats: they must not pick up the rules meant for the card at that number."""
    cid = card_id(set_code, number)
    aspects = flat(raw.get("Aspects"))
    card = {
        "id": cid,
        "set": set_code,
        "n": number,
        "name": clean_name(cid, raw["Name"]) if fixes else clean_name("", raw["Name"]),
        "sub": (raw.get("Subtitle") or "").strip() or None,
        "type": raw.get("Type"),
        "rarity": RARITY.get(raw.get("Rarity") or "", None),
        "aspects": aspects,
        "traits": [t.title() for t in flat(raw.get("Traits"))],
        "cost": _int(raw.get("Cost")),
        "power": _int(raw.get("Power")),
        "hp": _int(raw.get("HP")),
        "text": (raw.get("FrontText") or "").strip() or None,
        "arena": (flat(raw.get("Arenas")) or [None])[0],
        "art": art or _art_path(raw.get("FrontArt")),
    }
    card.update(back_fields(raw))
    if fixes:
        card.update(RULES["dataFixes"].get(cid, {}))
    return {k: v for k, v in card.items() if v not in (None, [], "")}


def back_fields(raw: dict) -> dict:
    """Leaders are double-sided: the deployed side's rules text and art path (None on everything else)."""
    if raw.get("DoubleSided") is not True:
        return {}
    return {"backText": (raw.get("BackText") or "").strip() or None, "backArt": _art_path(raw.get("BackArt"))}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _art_path(url: str | None):
    m = re.search(r"/images/cards/(.+)$", url or "")
    return m.group(1) if m else None


def parse_date(s: str | None):
    if not s:
        return None
    return datetime.strptime(s, "%m/%d/%y").date().isoformat()


# --- HTML fallback (site search table) ------------------------------------------------------------

def site_rows(set_code: str) -> list[dict]:
    """Parse the website's search-result table for a set — the only source for LAWOP right now."""
    page = http_get(f"{SITE}/search?q=set%3A{set_code.lower()}&variants=true")
    rows = []
    for m in re.finditer(r'<tr class="card-row"(.*?)</tr>', page, flags=re.S):
        body = m.group(1)
        img = re.search(r'front-image="([^"]+)"', body)
        cells = re.findall(r'<td[^>]*>(.*?)</td>', body, flags=re.S)
        text = [re.sub(r"<[^>]+>", " ", c) for c in cells]
        text = [re.sub(r"\s+", " ", html.unescape(t)).strip() for t in text]
        if len(text) < 12:
            continue
        aspects = [a.title() for a in re.findall(r'/static/images/([a-z]+)\.png', cells[3])]
        full = text[2]
        name, _, sub = full.partition(" - ")
        rows.append({
            "Set": text[0], "Number": text[1], "Name": name, "Subtitle": sub, "Type": text[4],
            "Aspects": aspects, "Cost": text[6], "Power": text[7], "HP": text[8],
            "Traits": [t.strip() for t in text[9].split("•") if t.strip()],
            "Rarity": text[10], "FrontArt": img.group(1) if img else None,
        })
    return rows


def table_numbers(page: str) -> tuple[list[int], set[int]]:
    """Collector numbers listed in a site search table -> (plain numbers, numbers that also have an `F` row).

    The number cell reads `012` for a card and `012F` for its same-number foil twin. Both rows share one
    image URL, so the numbers must come from the cell, not the image path — otherwise the twin vanishes.
    """
    plain, foil = set(), set()
    for body in re.findall(r'<tr class="card-row"(.*?)</tr>', page, flags=re.S):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', body, flags=re.S)
        if len(cells) < 2:
            continue
        token = re.sub(r"<[^>]+>|\s+", "", html.unescape(cells[1]))
        if token.isdigit():
            plain.add(int(token))
        elif re.fullmatch(r"\d+F", token, flags=re.I):
            foil.add(int(token[:-1]))
    return sorted(plain), foil


# --- Fetch ----------------------------------------------------------------------------------------

def variant_id(set_code: str, number: str) -> str | None:
    m = re.fullmatch(r"(\d+)(.*)", number.strip())
    return f"{set_code}-{int(m.group(1)):03d}{m.group(2)}" if m else None


def number_order(number: str):
    m = re.fullmatch(r"(\d+)(.*)", number)
    return (int(m.group(1)), m.group(2)) if m else (1 << 30, number)


def fetch_set(code: str, set_name: str, cards_too: bool) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """(cards, [(variant, its stats)]) for one set — mirrors CatalogSync.parseSet."""
    data = json.loads(http_get(f"{API}/cards/{code.lower()}"))
    data = data.get("data", []) if isinstance(data, dict) else data
    cards: dict[str, dict] = {}
    variants: dict[str, tuple[dict, dict]] = {}
    for raw in data:
        number = str(raw.get("Number") or "").strip()
        if not number or number[:1].upper() == "T" or "token" in (raw.get("Type") or "").lower():
            continue
        label = (raw.get("VariantType") or "").strip() or "Normal"
        if cards_too and label == "Normal" and number.isdigit():
            card = to_card(raw, code, int(number))
            cards.setdefault(card["id"], card)
            continue
        vid = variant_id(code, number)
        if not vid or not raw.get("Name"):
            continue
        stats = to_card(raw, code, int(re.match(r"\d+", number).group()), fixes=False)
        art = stats.get("art")
        variant = {"id": vid, "number": number, "label": set_name if label == "Normal" else label,
                   "name": stats["name"], "sub": stats.get("sub"),
                   "art": art if art != f"{code}/{number}.png" else None}   # only when not the usual path
        variants.setdefault(vid, ({k: v for k, v in variant.items() if v is not None}, stats))
    ordered = sorted(variants.values(), key=lambda p: number_order(p[0]["number"]))
    return sorted(cards.values(), key=lambda c: c["n"]), ordered


# --- Variant linking (mirror of VariantLinker.kt / FamilyIndex.normalize) ---------------------------

def normalize(s: str | None) -> str:
    if not s or not s.strip():
        return ""
    if all(ord(ch) < 128 for ch in s):
        folded = s.lower()
    else:
        folded = "".join(ch for ch in unicodedata.normalize("NFD", s) if not unicodedata.category(ch).startswith("M")).lower()
    folded = re.sub(r"\([^)]*\)", " ", folded)
    folded = re.sub(r"['\u2019\u2018`]", "", folded)
    folded = re.sub(r"[^a-z0-9+/\-]+", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def title_key(c: dict) -> str:
    return normalize(c.get("name")) + "|" + normalize(c.get("sub"))


def stats_key(c: dict):
    return (c.get("type"), c.get("cost"), c.get("power"), c.get("hp"),
            frozenset(a.lower() for a in c.get("aspects", [])), normalize(c.get("text")))


def link_variants(raws: list[tuple[dict, dict]], cards: list[dict], sets: list[dict]) -> list[dict]:
    kind = {s["code"]: s["kind"] for s in sets}
    set_of = lambda v: v["id"].split("-", 1)[0]
    released = {s["code"]: s.get("released") or "" for s in sets}
    playable = {"MAIN", "TWIN_SUNS", "PRODUCT"}
    pool = sorted((c for c in cards if kind.get(c["set"]) in playable), key=lambda c: (released[c["set"]], c["set"], c["n"]))
    by_title: dict[str, list[dict]] = {}
    for c in pool:
        by_title.setdefault(title_key(c), []).append(c)
    by_stats: dict = {}
    for c in pool:
        if c.get("text"):
            by_stats.setdefault(stats_key(c), []).append(c)

    def base(variant: dict, c: dict):
        mine = stats_key(c)
        titled = by_title.get(title_key(c), [])
        if kind.get(set_of(variant)) in playable:
            in_set = [t for t in titled if t["set"] == set_of(variant)]
            if len(in_set) == 1:
                return in_set[0]
            same = [t for t in in_set if stats_key(t) == mine]
            if len(same) == 1:
                return same[0]
            if not in_set and c.get("text"):
                here = [t for t in by_stats.get(mine, []) if t["set"] == set_of(variant)]
                if len(here) == 1:
                    return here[0]
        if titled:
            for t in titled:
                if stats_key(t) == mine:
                    return t
            return titled[0] if len({stats_key(t) for t in titled}) == 1 else None
        if not c.get("text"):
            return None
        same = by_stats.get(mine, [])
        return same[0] if same and len({title_key(t) for t in same}) == 1 else None

    links = RULES.get("variantLinks", {})
    present = {c["id"] for c in cards}
    out = []
    for variant, stats in raws:
        b = base(variant, stats)
        v = dict(variant)
        pinned = links.get(v["id"])
        if pinned in present:
            v["base"] = pinned
        elif b is not None:
            v["base"] = b["id"]
        if "base" in v:                      # the base card's title is shown; keep ours only for review
            v.pop("name", None)
            v.pop("sub", None)
        out.append(v)
    return out


def fetch_weekly(codes: list[str]) -> tuple[str, list[dict]]:
    """Try each candidate code (swu-db lists LAWOP and LAWP for the same set) until one has data."""
    for code in codes:
        # numbers: from the site listing (the search API returns nothing for promo sets)
        page = http_get(f"{SITE}/search?q=set%3A{code.lower()}&variants=true")
        nums, same_number_foils = table_numbers(page)
        if not nums:
            continue
        raws: dict[int, dict] = {}
        for n in nums:
            try:
                raws[n] = json.loads(http_get(f"{API}/cards/{code.lower()}/{n:03d}"))
            except urllib.error.HTTPError:
                try:
                    raws[n] = json.loads(http_get(f"{API}/cards/{code.lower()}/{n:02d}"))
                except urllib.error.HTTPError:
                    pass
        source = "api"
        if len(raws) < len(nums):  # API can't serve it -> website table
            raws = {int(r["Number"]): r for r in site_rows(code) if r["Number"].isdigit()}   # not `012F` twins
            source = "site"
        if not raws:
            continue
        app_code = RULES.get("setCodeAliases", {}).get(code, code)   # keeps ids stable if swu-db renames a set
        print(f"    {code}: {len(raws)} cards via {source}" + (f" (filed as {app_code})" if app_code != code else ""))
        base = sorted(n for n in raws if n <= 20)
        cards = []
        for n in base:
            c = to_card(raws[n], app_code, n)
            c["foil"] = (n + 20) in raws or n in same_number_foils
            cards.append(c)
        return app_code, cards
    raise RuntimeError(f"no data for any of {codes}")


# --- Official fallback (admin.starwarsunlimited.com) ----------------------------------------------

def official_rows(code: str) -> list[dict]:
    """Every printing the official database lists for a set — cards, variants and tokens — as Strapi
    `attributes` dicts. 250 a page (its cap); a page is ~3 MB, so the timeout is generous."""
    rows, page = [], 1
    while True:
        query = urllib.parse.urlencode({
            "locale": "en", "filters[expansion][code][$eq]": code, "sort[0]": "cardNumber:asc",
            "pagination[pageSize]": 250, "pagination[page]": page,
        })
        data = json.loads(http_get(f"{OFFICIAL_API}?{query}", timeout=150))
        rows += [x.get("attributes") or {} for x in data.get("data") or []]
        if page >= ((data.get("meta") or {}).get("pagination") or {}).get("pageCount", 1):
            return rows
        page += 1


def official_new_sets(known: set[str]) -> list[dict]:
    """Expansions the official database has cards for that swu-db doesn't list at all, from its newest
    cards: [{code, name}]."""
    query = urllib.parse.urlencode({"locale": "en", "sort[0]": "createdAt:desc", "pagination[pageSize]": 250})
    data = json.loads(http_get(f"{OFFICIAL_API}?{query}", timeout=150))
    seen: dict[str, str] = {}
    for x in data.get("data") or []:
        exp = _rel((x.get("attributes") or {}).get("expansion")) or {}
        code, name = exp.get("code"), exp.get("name")
        if code and name and code not in known and code not in EXCLUDE_MAIN:
            seen.setdefault(code, name)
    return [{"code": c, "name": n} for c, n in seen.items()]


def _rel(value):
    """A Strapi relation: {"data": {...}} -> its attributes; {"data": [...]} -> a list of them; else None."""
    data = value.get("data") if isinstance(value, dict) else None
    if isinstance(data, list):
        return [d.get("attributes") or {} for d in data]
    if isinstance(data, dict):
        return data.get("attributes") or {}
    return None


def _names(value) -> list[str]:
    rel = _rel(value)
    return [a["name"].strip() for a in rel if (a.get("name") or "").strip()] if isinstance(rel, list) else []


def _name(value) -> str | None:
    rel = _rel(value)
    return (rel.get("name") or "").strip() or None if isinstance(rel, dict) else None


def _image(value) -> str | None:
    """The 700px rendition when there is one, else the original."""
    rel = _rel(value)
    if not isinstance(rel, dict):
        return None
    medium = (rel.get("formats") or {}).get("medium") or {}
    return medium.get("url") or rel.get("url")


def official_text(s: str | None) -> str | None:
    """Plain rules text in swu-db's style: the unique glyph spelled out, stray markup and padding gone."""
    if not s:
        return None
    s = s.replace("<uq>", "unique").replace("\u2011", "-").replace("\u2013", "-").replace("\u00a0", " ")
    s = re.sub(r"<[^>]+>", "", html.unescape(s))
    s = "\n".join(line.strip() for line in s.split("\n"))
    return s.strip() or None


def official_card(a: dict, code: str, art: str | None, back_art: str | None, *, fixes: bool = True) -> dict:
    n = int(a["cardNumber"])
    cid = card_id(code, n)
    kind = _name(a.get("type"))
    upgrade = kind == "Upgrade"            # an upgrade's printed modifiers are upgradePower / upgradeHp
    card = {
        "id": cid, "set": code, "n": n,
        "name": clean_name(cid if fixes else "", a.get("title") or ""),
        "sub": (a.get("subtitle") or "").strip() or None,
        "type": kind,
        "rarity": RARITY.get(_name(a.get("rarity")) or ""),
        "aspects": _names(a.get("aspects")) + _names(a.get("aspectDuplicates")),
        "traits": [t.title() for t in _names(a.get("traits"))],
        "cost": _int(a.get("cost")),
        "power": _int(a.get("upgradePower") if upgrade else a.get("power")),
        "hp": _int(a.get("upgradeHp") if upgrade else a.get("hp")),
        "text": official_text(a.get("text")),
        "arena": (_names(a.get("arenas")) or [None])[0],
        "art": art,
    }
    if kind == "Leader":
        card["backText"] = official_text(a.get("deployBox"))
        card["backArt"] = back_art
    if fixes:
        card.update(RULES["dataFixes"].get(cid, {}))
    return {k: v for k, v in card.items() if v not in (None, [], "")}


class ArtMirror:
    """Official art for the cards swu-db lacks, copied once into the feed (`--art-dir`) and served from
    `--art-base`, so no phone ever loads it from the official site. Files are `SET/NUMBER.webp` and a
    leader's deployed side `SET/NUMBER-b.webp` (the app derives a variant's back that way). Without
    --art-dir nothing is downloaded: the URLs point at the published feed's copies."""

    def __init__(self, base: str, directory: Path | None):
        self.base, self.dir = base, directory
        self.sources: dict[str, str] = {}

    def url(self, rel: str, source: str | None) -> str | None:
        if not source:
            return None
        self.sources[rel] = source
        return self.base + rel

    def sync(self, catalog: dict) -> None:
        """Download what the finished catalog points at and isn't on disk; delete what it no longer does."""
        if self.dir is None:
            return
        keep: set[str] = set()
        for c in catalog["cards"]:
            keep.update(x for x in (c.get("art"), c.get("backArt")) if x)
        for v in catalog["variants"]:
            if v.get("art"):
                keep.add(v["art"])
                keep.add(re.sub(r"\.(\w+)$", r"-b.\1", v["art"]))
        keep = {u[len(self.base):] for u in keep if u.startswith(self.base)}
        todo = [r for r in sorted(keep) if r in self.sources and not (self.dir / r).exists()]
        if todo:
            from PIL import Image          # pip install pillow — only needed when mirroring
            def fetch(rel: str) -> bool:
                try:
                    img = Image.open(io.BytesIO(http_bytes(self.sources[rel], timeout=60)))
                    img.load()
                    if img.width > ART_WIDTH:
                        img = img.resize((ART_WIDTH, round(img.height * ART_WIDTH / img.width)), Image.LANCZOS)
                    if img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGBA")
                    out = self.dir / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    tmp = out.with_suffix(".tmp")
                    img.save(tmp, "WEBP", quality=82, method=6)
                    os.replace(tmp, out)
                    return True
                except Exception as e:  # a missing picture retries next run; the app shows the card's name
                    print(f"    !! art {rel}: {e}")
                    return False
            with ThreadPoolExecutor(8) as pool:
                ok = sum(pool.map(fetch, todo))
            print(f"  art: {ok}/{len(todo)} mirrored")
        pruned = 0
        if self.dir.exists():
            for f in self.dir.rglob("*"):
                if f.is_file() and f.relative_to(self.dir).as_posix() not in keep:
                    f.unlink()
                    pruned += 1
        if pruned:
            print(f"  art: {pruned} no longer needed, removed")


def official_fill(code: str, cards: list[dict], variants: list[tuple[dict, dict]], mirror: ArtMirror,
                  set_name: str, reprints: list[tuple[str, str, int]]) -> tuple[list[dict], list[tuple[dict, dict]], dict[str, str]]:
    """swu-db's cards and variants for a set plus every printing the official database has that swu-db
    lacks (by collector number — swu-db wins any number it has). Returns (cards, variants, the official
    `variantOf` base of each variant it added). Every card the official database calls a reprint is
    appended to [reprints] as (its id, the original's name, the original's collector number)."""
    rows = official_rows(code)
    have_cards = {c["n"] for c in cards}
    have_variants = {v["id"] for v, _ in variants}
    cards, variants = list(cards), list(variants)
    bases: dict[str, str] = {}
    added_c = added_v = 0
    for a in rows:
        kind = _name(a.get("type")) or ""
        number = a.get("cardNumber")
        if not isinstance(number, int) or kind.startswith("Token") or not a.get("title"):
            continue
        base = _rel(a.get("variantOf"))
        types = _names(a.get("variantTypes"))
        label = OFFICIAL_LABELS.get(types[0], types[0]) if types else ("Standard" if not base else "Variant")
        if not base and label == "Standard":
            original = _rel(a.get("reprintOf"))
            if isinstance(original, dict) and original.get("title") and isinstance(original.get("cardNumber"), int):
                reprints.append((card_id(code, number), original["title"], original["cardNumber"]))
            if number in have_cards:
                continue
            art = mirror.url(f"{code}/{number:03d}.webp", _image(a.get("artFront")))
            back = mirror.url(f"{code}/{number:03d}-b.webp", _image(a.get("artBack"))) if kind == "Leader" else None
            cards.append(official_card(a, code, art, back))
            have_cards.add(number)
            added_c += 1
            continue
        vid = variant_id(code, str(number))
        if not vid or vid in have_variants:
            continue
        art = mirror.url(f"{code}/{number}.webp", _image(a.get("artFront")))
        mirror.url(f"{code}/{number}-b.webp", _image(a.get("artBack")))   # a leader variant's deployed side
        stats = official_card(a, code, art, None, fixes=False)
        variant = {"id": vid, "number": str(number), "label": set_name if label == "Standard" else label,
                   "name": stats["name"], "sub": stats.get("sub"), "art": art}
        variants.append(({k: v for k, v in variant.items() if v is not None}, stats))
        have_variants.add(vid)
        if isinstance(base, dict) and isinstance(base.get("cardNumber"), int):
            bases[vid] = card_id(code, base["cardNumber"])
        added_v += 1
    if added_c or added_v:
        print(f"    + official: {added_c} cards, {added_v} variants swu-db doesn't have yet")
    return (sorted(cards, key=lambda c: c["n"]),
            sorted(variants, key=lambda p: number_order(p[0]["number"])), bases)


# --- Same card, reworded (official reprintOf) ------------------------------------------------------

def reprint_groups(reprints: list[tuple[str, str, int]], cards: list[dict], sets: list[dict]) -> list[list[str]]:
    """A reprint the official database names (`reprintOf`) is the same card as its original even when it is
    reworded (Icons: "the defender" -> "the defending unit") — without this the app would ask the user
    whether they're one card. Each group: the reprint, its original (same name and collector number in a
    playable set) and every printing of that title that matches the original's stats."""
    kind = {s["code"]: s["kind"] for s in sets}
    playable = [c for c in cards if kind.get(c["set"]) in ("MAIN", "TWIN_SUNS", "PRODUCT")]
    by_id = {c["id"]: c for c in playable}
    out = []
    for new_id, name, number in reprints:
        if new_id not in by_id:
            continue
        found = [c for c in playable if c["n"] == number and c["id"] != new_id
                 and normalize(c["name"]) == normalize(clean_name("", name))]
        if len(found) != 1:
            continue
        orig = found[0]
        same = {c["id"] for c in playable if title_key(c) == title_key(orig) and stats_key(c) == stats_key(orig)}
        out.append(sorted(same | {orig["id"], new_id}))
    return out


def merge_groups(groups: list[list[str]]) -> list[list[str]]:
    """Overlapping groups become one (the app maps each id to a single group). Mirror of CatalogRules.mergeGroups."""
    merged: list[set[str]] = []
    order: list[str] = []
    for g in groups:
        order += [i for i in g if i not in order]
        g = set(g)
        for m in [m for m in merged if m & g]:
            g |= m
            merged.remove(m)
        merged.append(g)
    rank = {i: n for n, i in enumerate(order)}
    return sorted((sorted(m, key=rank.get) for m in merged), key=lambda m: rank[m[0]])


# --- Never shrink (mirror of CatalogSync.merge) ---------------------------------------------------

def order_sets(sets: list[dict]) -> list[dict]:
    """Expansions by release date, each followed by its Weekly Play checklist; promo sources last."""
    weekly: dict[str, list[dict]] = {}
    for s in sets:
        if s["kind"] == "WEEKLY_PLAY":
            weekly.setdefault(s.get("parent"), []).append(s)
    mains = sorted((s for s in sets if s["kind"] in ("MAIN", "TWIN_SUNS", "PRODUCT")),
                   key=lambda s: (s.get("released") or "9999", s["code"]))
    out, placed = [], set()
    for m in mains:
        out.append(m)
        for w in weekly.get(m["code"], []):
            out.append(w)
            placed.add(w["code"])
    out += [s for s in sets if s["kind"] == "WEEKLY_PLAY" and s["code"] not in placed]
    out += sorted((s for s in sets if s["kind"] == "PROMO"), key=lambda s: s["code"])
    return out


def carry_forward(previous: dict | None, sets: list[dict], cards: list[dict], variants: list[dict]):
    """A set this run lost, or got back smaller (a timeout, a 502, an upstream hiccup), keeps the previous
    catalog's version of it — card and variant ids are ledger keys, so they must never vanish. The app
    applies the same rule to whatever it downloads."""
    if not previous:
        return sets, cards, variants
    by_set = lambda items: {k: [i for i in items if i["id"].split("-", 1)[0] == k] for k in {i["id"].split("-", 1)[0] for i in items}}
    new_sets = {s["code"]: s for s in sets}
    new_cards, new_vars = by_set(cards), by_set(variants)
    old_cards, old_vars = by_set(previous.get("cards", [])), by_set(previous.get("variants", []))
    for old in previous.get("sets", []):
        code = old["code"]
        if code in new_sets and len(new_cards.get(code, [])) >= len(old_cards.get(code, [])) \
                and len(new_vars.get(code, [])) >= len(old_vars.get(code, [])):
            continue
        print(f"  ~ {code}: kept the previous run's data ({'missing' if code not in new_sets else 'smaller'} this time)")
        new_sets[code] = old
        new_cards[code] = old_cards.get(code, [])
        new_vars[code] = old_vars.get(code, [])
    ordered = order_sets(list(new_sets.values()))
    return (ordered,
            [c for s in ordered for c in sorted(new_cards.get(s["code"], []), key=lambda c: c["n"])],
            [v for s in ordered for v in new_vars.get(s["code"], [])])


def parse_args():
    ap = argparse.ArgumentParser(description="Build the card catalog (swu-db, with the official database filling its gaps).")
    ap.add_argument("--out", type=Path, default=OUT, help="catalog to write (default: the app's bundled asset)")
    ap.add_argument("--rules", type=Path, help="catalog_rules.json (default: the app's asset)")
    ap.add_argument("--previous", type=Path, help="last published catalog, for never-shrink (default: --out if it exists)")
    ap.add_argument("--no-previous", action="store_true", help="build from scratch, no never-shrink")
    ap.add_argument("--art-dir", type=Path, help="mirror official art here (the feed's docs/art); omit to only link to the feed")
    ap.add_argument("--art-base", default=FEED_ART_BASE, help="public URL of --art-dir")
    ap.add_argument("--official-days", type=int, default=OFFICIAL_DAYS, help="check sets released this many days back")
    ap.add_argument("--no-official", action="store_true", help="swu-db only")
    return ap.parse_args()


def iso_to_swu(iso: str) -> str:
    """"2027-03-13" -> swu-db's "3/13/27"."""
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}/{d.year % 100:02d}"


def main() -> int:
    args = parse_args()
    if args.art_dir:
        try:
            import PIL  # noqa: F401 — fail now, not after ten minutes of fetching
        except ImportError:
            raise SystemExit("--art-dir needs Pillow: pip install pillow")
    previous_path = None if args.no_previous else (args.previous or args.out)
    previous = None
    if previous_path and previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
        except ValueError:
            print(f"  !! {previous_path} unreadable — building without never-shrink")
    mirror = ArtMirror(args.art_base if args.art_base.endswith("/") else args.art_base + "/", args.art_dir)
    official_on = not args.no_official
    official_cutoff = (date.today() - timedelta(days=args.official_days)).isoformat()
    official_bases: dict[str, str] = {}
    reprints: list[tuple[str, str, int]] = []
    release_dates: dict[str, str] = RULES.get("releaseDates", {})

    sets_raw = api_json("/sets")
    today = date.today().isoformat()
    for s in sets_raw:                       # a curated date fills one swu-db hasn't published yet
        if not s.get("releaseDate") and s.get("setId") in release_dates:
            s["releaseDate"] = iso_to_swu(release_dates[s["setId"]])
    if official_on:
        known = {s["setId"] for s in sets_raw} | set(RULES.get("setCodeAliases", {}).values())
        try:
            for new in official_new_sets(known):
                if new["code"] not in release_dates:
                    print(f"  ?? {new['code']} {new['name']}: on the official site, not swu-db — add its release date to "
                          f"catalog_rules.json releaseDates to include it")
                    continue
                print(f"  + {new['code']} {new['name']}: official database only")
                sets_raw.append({"setId": new["code"], "fullName": new["name"], "numberCards": MIN_MAIN_CARDS,
                                 "releaseDate": iso_to_swu(release_dates[new["code"]]), "officialOnly": True})
        except Exception as e:
            print(f"  !! couldn't list the official database's sets ({e})")

    def is_twin_suns(s):
        return bool(TWIN_SUNS.search(s["fullName"])) and s["setId"] not in EXCLUDE_MAIN

    def is_product(s):
        return not is_twin_suns(s) and bool(PRODUCT.search(s["fullName"])) and s["setId"] not in EXCLUDE_MAIN

    def is_weekly(s):
        return s["fullName"].endswith("- OP Promo") and "Judge" not in s["fullName"]

    main_sets = []
    for s in sets_raw:
        name, code = s["fullName"], s["setId"]
        if (" - " not in name and (s.get("numberCards") or 0) >= MIN_MAIN_CARDS
                and code not in EXCLUDE_MAIN and s.get("releaseDate") and not is_twin_suns(s) and not is_product(s)):
            main_sets.append(s)
    twin_suns = [s for s in sets_raw if is_twin_suns(s) or is_product(s)]
    # Twin Suns and other products take their place among the expansions by release date.
    main_sets = sorted(main_sets + twin_suns,
                       key=lambda s: (datetime.strptime(s["releaseDate"], "%m/%d/%y") if s.get("releaseDate") else datetime.max, s["setId"]))
    promo_sets = sorted((s for s in sets_raw if s not in main_sets and not is_weekly(s) and s["setId"] not in EXCLUDE_MAIN),
                        key=lambda s: s["setId"])

    weekly_by_parent: dict[str, list[str]] = {}
    for s in sets_raw:
        if is_weekly(s):
            parent_name = s["fullName"].removesuffix(" - OP Promo")
            weekly_by_parent.setdefault(parent_name, []).append(s["setId"])

    sets, cards, raw_variants = [], [], []
    for s in main_sets:
        code = s["setId"]
        kind = "TWIN_SUNS" if is_twin_suns(s) else "PRODUCT" if is_product(s) else "MAIN"
        print(f"  {code} {s['fullName']}")
        got, got_variants, swu_ok = [], [], False
        if not s.get("officialOnly"):
            try:
                got, got_variants = fetch_set(code, display_name(s["fullName"]), cards_too=True)
                swu_ok = True
            except Exception as e:
                print(f"    !! {code}: swu-db failed ({e})")
        released = parse_date(s.get("releaseDate"))
        # Recent / upcoming sets, and any swu-db lists with no cards: the official database fills the gaps.
        # (An older set swu-db merely failed on is left to never-shrink, not rebuilt from the official site.)
        recent = released is None or released >= official_cutoff
        if official_on and (recent or s.get("officialOnly") or (swu_ok and not got)):
            try:
                got, got_variants, bases = official_fill(code, got, got_variants, mirror, display_name(s["fullName"]), reprints)
                official_bases.update(bases)
            except Exception as e:
                print(f"    !! {code}: official database failed ({e})")
        if not got:
            print(f"    !! {code}: no cards yet, skipped" if swu_ok else f"    !! {code} skipped")
            continue
        sets.append({"code": code, "name": display_name(s["fullName"]), "kind": kind, "released": released,
                     "count": len(got), "variants": len(got_variants)})
        cards += got
        raw_variants += got_variants
        if kind != "MAIN":
            continue
        codes = sorted(weekly_by_parent.get(s["fullName"].replace("Jump To", "Jump to"), []) or
                       weekly_by_parent.get(s["fullName"], []),
                       key=lambda c: (not c.endswith("OP"), c))
        if codes:
            try:
                wcode, wcards = fetch_weekly(codes)
                sets.append({"code": wcode, "name": f"{display_name(s['fullName'])} — Weekly Play", "kind": "WEEKLY_PLAY",
                             "parent": code, "released": released, "count": len(wcards)})
                cards += wcards
            except Exception as e:
                print(f"    !! weekly play for {code} skipped ({e})")

    for s in promo_sets:
        code = s["setId"]
        try:
            _, got_variants = fetch_set(code, display_name(s["fullName"]), cards_too=False)
        except Exception as e:
            print(f"    !! promo set {code} skipped ({e})")
            continue
        if not got_variants:
            continue
        print(f"  {code} {s['fullName']}: {len(got_variants)} variants")
        promo = {"code": code, "name": display_name(s["fullName"]), "kind": "PROMO", "released": parse_date(s.get("releaseDate")),
                 "count": 0, "variants": len(got_variants), "listed": s.get("numberCards") or 0}
        sets.append({k: v for k, v in promo.items() if v is not None})
        raw_variants += got_variants

    variants = link_variants(raw_variants, cards, sets)
    present_ids = {c["id"] for c in cards}
    pinned = RULES.get("variantLinks", {})
    for v in variants:                       # the official database says outright what a variant is of
        b = official_bases.get(v["id"])
        if b in present_ids and v["id"] not in pinned:
            v["base"] = b
            v.pop("name", None)
            v.pop("sub", None)
    unlinked = [v for v in variants if "base" not in v]
    print(f"\n  {len(variants)} variants, {len(unlinked)} with no base card" +
          "".join(f"\n    ?? {v['id']} {v['label']}: {v['name']}" + (f" — {v['sub']}" if v.get("sub") else "") for v in unlinked))

    sets, cards, variants = carry_forward(previous, order_sets(sets), cards, variants)
    present = {c["id"] for c in cards}
    groups = list(RULES["sameCard"]) + reprint_groups(reprints, cards, sets) + list((previous or {}).get("sameCard", []))
    same_card = [g for g in ([i for i in m if i in present] for m in merge_groups(groups)) if len(g) >= 2]
    catalog = {"schema": 5, "generated": today, "sets": sets, "cards": cards, "sameCard": same_card, "variants": variants}
    if previous and {k: v for k, v in previous.items() if k != "generated"} == {k: v for k, v in catalog.items() if k != "generated"}:
        catalog["generated"] = previous.get("generated", today)   # nothing changed: same bytes, no new commit
    mirror.sync(catalog)
    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, out)
    print(f"\nwrote {out} — {len(sets)} sets, {len(cards)} cards, {out.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
