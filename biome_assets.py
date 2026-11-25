#!/usr/bin/env python3
"""Standalone biome asset catalog and curator for LOTRO environment.

Use: python scripts/biome_assets.py --help

Deterministic planning by default. The only agent integration is the user's
local Cursor CLI `agent` command with a Composer model, for optional cached
asset metadata review. No cloud API keys or third-party Python packages are
required for standard commands. Thumbnail previews optionally use Pillow.

This module does not modify Unreal assets or call undocumented Fab download APIs.
"""
from __future__ import annotations


# ----------------------------------------------------------------------
# Catalog, classifier and quality scorer
# ----------------------------------------------------------------------
"""Deterministic catalog, asset classification, quality scoring, and biome subset selection."""

import datetime as dt
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

CATALOG_URL = "https://huggingface.co/datasets/codepawl/asset-index/resolve/main/metadata.jsonl"
ROLES = ("canopy", "understory", "groundcover", "rocks", "debris", "surface", "accents")
BIOMES = {
    "forest": {"canopy": 5, "understory": 5, "groundcover": 6, "rocks": 3, "debris": 3, "surface": 5, "accents": 2},
    "meadow": {"canopy": 1, "understory": 2, "groundcover": 8, "rocks": 2, "debris": 1, "surface": 6, "accents": 3},
    "wetland": {"canopy": 2, "understory": 4, "groundcover": 7, "rocks": 1, "debris": 2, "surface": 6, "accents": 2},
    "ruins": {"canopy": 1, "understory": 3, "groundcover": 4, "rocks": 4, "debris": 5, "surface": 5, "accents": 2},
}
BIOME_ALIASES = {"forrest":"forest", "woodland":"forest", "woods":"forest", "temperate forest":"forest", "marsh":"wetland", "swamp":"wetland", "bog":"wetland", "grassland":"meadow", "field":"meadow", "ruin":"ruins", "courtyard":"ruins"}
ROLE_TERMS = {
    "canopy": ("tree", "oak", "birch", "pine", "maple", "ash", "beech", "spruce", "willow", "poplar", "elm", "cedar", "canopy", "trunk"),
    "understory": ("shrub", "bush", "sapling", "fern", "thicket", "hedge", "bramble", "young tree", "undergrowth", "reed"),
    "groundcover": ("grass", "plant", "flower", "clover", "fern", "weed", "moss", "lichen", "herb", "sedge", "rush", "ground cover", "groundcover", "foliage"),
    "rocks": ("rock", "boulder", "stone", "pebble", "outcrop", "cliff", "slate", "granite", "limestone"),
    "debris": ("fallen", "log", "branch", "twig", "stick", "deadwood", "stump", "root", "rubble", "ruin", "broken stone", "leaf litter", "debris"),
    "surface": ("surface", "ground", "soil", "dirt", "mud", "sand", "leaf litter", "gravel", "grass", "moss", "rock", "stone", "meadow", "terrain", "material", "pbr", "forest floor"),
    "accents": ("flower", "mushroom", "stump", "small stone", "wildflower", "fern", "moss", "berries", "log", "decals", "decal"),
}
BIOME_TERMS = {
    "forest": ("forest", "woodland", "oak", "birch", "maple", "fern", "moss", "leaf", "temperate", "trees", "deciduous", "pine", "branch"),
    "meadow": ("meadow", "grass", "grassland", "clover", "field", "wildflower", "temperate", "loam", "pasture"),
    "wetland": ("wetland", "swamp", "marsh", "bog", "mud", "reed", "sedge", "rush", "willow", "wet", "damp", "moss"),
    "ruins": ("ruin", "rubble", "stone", "moss", "broken", "debris", "weathered", "weed", "ivy", "gravel"),
}
NEGATIVE_TERMS = ("low poly", "lowpoly", "cartoon", "stylized", "stylised", "voxel", "pixel art", "hand painted")
REPUTABLE_SOURCES = {"ambientcg", "polyhaven", "poly haven", "quixel", "megascans"}


def tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def as_list(value: Any) -> list[Any]:
    if value is None: return []
    if isinstance(value, list): return value
    if isinstance(value, (tuple, set)): return list(value)
    if isinstance(value, str):
        v = value.strip()
        if v.startswith("["):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list): return parsed
            except ValueError: pass
        return [v] if v else []
    return [value]


def parse_json(value: Any) -> Any:
    if not isinstance(value, str): return value
    try: return json.loads(value)
    except (ValueError, TypeError): return value


def source_is_free(source: dict[str, Any]) -> tuple[bool, bool]:
    """Return (candidate_free, verified_free). Vendor-specific price claims are not proof."""
    v = source.get("isFree", source.get("is_free", source.get("free")))
    if v is False: return False, False
    licence = str(source.get("licence", source.get("license", ""))).lower()
    host = str(source.get("source", source.get("seller", ""))).lower()
    if "cc0" in licence or (host in ("ambientcg", "polyhaven", "kenney") and "cc" in licence): return True, True
    if v is True: return True, False  # listing can have changed since catalog snapshot
    if "fab" in licence or "quixel" in host or "megascans" in host or host == "quixel megascans":
        return True, False  # price must be rechecked at live listing before acquiring
    if "quaternius" in host: return True, False
    return False, False


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("asset_id"):
        name = str(raw.get("asset_id"))
    else:
        name = str(raw.get("uid") or raw.get("id") or "")
    if not name:
        name = str(raw.get("source") or raw.get("seller") or "unknown") + ":" + str(raw.get("title") or "unknown").lower().replace(" ", "-")
    rating = raw.get("rating")
    if isinstance(rating, dict):
        avg = rating.get("average", rating.get("avg"))
        cnt = rating.get("count", rating.get("ratingCount"))
    else:
        avg = raw.get("rating_average", rating if isinstance(rating, (int,float)) else None)
        cnt = raw.get("rating_count", raw.get("review_count"))
    cat = raw.get("category_path", raw.get("category", ""))
    if isinstance(cat, dict): cat = cat.get("path", cat.get("name", ""))
    price_free, verified = source_is_free(raw)
    ts = str(raw.get("publishedAt", raw.get("published_at", "")) or "")
    provider = str(raw.get("source", raw.get("seller", "")) or "unknown")
    source_url = str(raw.get("source_url", raw.get("url", "")) or "")
    kind = str(raw.get("kind", raw.get("listingType", "")) or "").lower()
    tags = [str(x) for x in as_list(raw.get("tags"))]
    style = str(raw.get("style", "") or "").lower()
    kind_text = " ".join((str(raw.get("use_for", "")), str(raw.get("surface_type", ""))))
    search_text = " ".join([str(raw.get("title", "")), str(cat), " ".join(tags), kind, kind_text, str(raw.get("search_text", ""))]).lower()
    if not isinstance(raw.get("source_url", None), str) and not source_url: source_url = ""
    return {
        "asset_id": name, "title": str(raw.get("title", name)), "source": provider,
        "source_url": source_url, "licence": str(raw.get("licence", raw.get("license", "unknown")) or "unknown"),
        "free_candidate": int(price_free), "free_verified": int(verified),
        "kind": kind, "style": style, "category": str(cat or ""), "search_text": search_text,
        "tags": tags, "biomes": [str(x).lower() for x in as_list(raw.get("biomes"))],
        "pbr_maps": [str(x).lower() for x in as_list(raw.get("pbr_maps"))],
        "max_texture_px": int(raw.get("max_texture_px") or 0),
        "triangles": int(raw.get("triangles") or 0),
        "lods": 1 if raw.get("lods") is True else 0,
        "tileable": 1 if raw.get("tileable") is True else 0,
        "scale_m": float(raw.get("scale_m") or 0),
        "rating_avg": float(avg) if avg is not None else None,
        "rating_count": int(cnt) if cnt is not None else None,
        "published_at": ts, "no_ai": 1 if raw.get("no_ai") is True else 0,
        "ai_generated": 1 if raw.get("ai_generated") is True else 0,
        "get_info": parse_json(raw.get("get", {})),
        "preview_url": str(raw.get("preview_url", raw.get("thumbnail", "")) or ""),
        "raw": raw,
    }


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS assets (
      asset_id TEXT PRIMARY KEY, title TEXT, source TEXT, source_url TEXT, licence TEXT,
      free_candidate INTEGER, free_verified INTEGER, kind TEXT, style TEXT, category TEXT,
      search_text TEXT, rating_avg REAL, rating_count INTEGER, published_at TEXT,
      raw TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS assessments (
      asset_id TEXT PRIMARY KEY, reviewed_role TEXT, confidence REAL,
      approved INTEGER, notes TEXT, model TEXT, updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_assets_free ON assets(free_candidate);
    CREATE INDEX IF NOT EXISTS ix_assets_kind ON assets(kind);
    """)
    return conn


def load_catalog(conn: sqlite3.Connection, path: Path, limit: int=0) -> int:
    count = 0
    with path.open("r", encoding="utf-8-sig") as handle:
        if path.suffix.lower() == ".json":
            data = json.load(handle)
            iterable = data if isinstance(data, list) else data.get("items", data.get("assets", []))
        else:
            iterable = (json.loads(line) for line in handle if line.strip())
        for raw in iterable:
            if not isinstance(raw, dict): continue
            a = normalize(raw)
            conn.execute("""INSERT INTO assets
              (asset_id,title,source,source_url,licence,free_candidate,free_verified,kind,style,category,search_text,rating_avg,rating_count,published_at,raw)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(asset_id) DO UPDATE SET title=excluded.title,source=excluded.source,
               source_url=excluded.source_url,licence=excluded.licence,
               free_candidate=excluded.free_candidate,free_verified=excluded.free_verified,
               kind=excluded.kind,style=excluded.style,category=excluded.category,
               search_text=excluded.search_text,rating_avg=excluded.rating_avg,
               rating_count=excluded.rating_count,published_at=excluded.published_at,raw=excluded.raw""",
                (a["asset_id"], a["title"], a["source"], a["source_url"], a["licence"],
                 a["free_candidate"],a["free_verified"],a["kind"],a["style"],a["category"],
                 a["search_text"],a["rating_avg"],a["rating_count"],a["published_at"],json.dumps(a,ensure_ascii=False)))
            count += 1
            if count % 2000 == 0: conn.commit()
            if limit and count >= limit: break
    conn.commit()
    return count


def all_free(conn: sqlite3.Connection) -> list[dict[str,Any]]:
    rows = conn.execute("""SELECT a.raw, b.reviewed_role, b.confidence, b.approved, b.notes
                          FROM assets a LEFT JOIN assessments b ON a.asset_id=b.asset_id
                          WHERE a.free_candidate=1""").fetchall()
    output=[]
    for r in rows:
        a=json.loads(r["raw"])
        a["assessment"] = {"role": r["reviewed_role"], "confidence": r["confidence"],
                           "approved": r["approved"], "notes": r["notes"]} if r["reviewed_role"] else None
        output.append(a)
    return output


def role_of(a: dict[str,Any]) -> str:
    if a.get("assessment") and a["assessment"].get("approved") is not None:
        if a["assessment"]["approved"] == 1 and a["assessment"].get("role") in ROLES:
            return a["assessment"]["role"]
    kind = a["kind"]
    text = a["search_text"]
    if kind in ("material", "texture", "surface", "pbr", "atlases", "atlas", "decal"):
        return "surface" if kind != "decal" else "accents"
    if "material" in a["category"].lower() and "vegetation" not in a["category"].lower(): return "surface"
    title = a["title"].lower()
    if any(w in title for w in ("forest floor", "ground", "soil", "mud", "dirt", "sand", "texture", "pbr material")) and (a["tileable"] or a["pbr_maps"]):
        return "surface"
    if ("log" in tokens(title) and any(t in tokens(title) for t in ("fallen", "mossy", "dead", "decaying"))) or any(w in title for w in ("fallen log", "dead tree", "rubble", "broken stone", "branch", "stump", "roots")): return "debris"
    if any(w in title for w in ("rock", "stone", "boulder", "cliff", "outcrop", "pebble")): return "rocks"
    if any(w in title for w in ("tree", "oak", "birch", "pine", "spruce", "beech", "maple", "willow")): return "canopy"
    if any(w in title for w in ("shrub", "bush", "sapling", "fern", "bramble", "hedge", "reed")): return "understory"
    if any(w in title for w in ("grass", "flower", "moss", "clover", "groundcover", "sedge", "weed", "plant")): return "groundcover"
    scores={role: sum(1 for term in terms if term in text) for role,terms in ROLE_TERMS.items()}
    role=max(scores, key=scores.get)
    return role if scores[role]>0 else "accents"


def quality(a:dict[str,Any], role:str) -> tuple[float, list[str]]:
    """Transparent technical-quality proxy, NOT downloads or visual inspection."""
    score = 20.0
    why=[]
    src = a["source"].lower()
    style = a["style"]
    photoreal = style in ("photoreal", "realistic", "photogrammetry", "photorealistic")
    if photoreal: score+=15; why.append("photoreal metadata")
    elif any(n in style for n in ("stylized", "lowpoly", "cartoon")) or any(n in a["title"].lower() for n in NEGATIVE_TERMS):
        score-=28; why.append("stylized/low-poly")
    else: why.append("render style unverified")
    if any(v in src for v in REPUTABLE_SOURCES): score+=11; why.append("established scanning/PBR source")
    maps=set(a["pbr_maps"])
    if role == "surface":
        if any(m in maps for m in ("base_colour","albedo", "base_color","color")): score+=7
        if any(m in maps for m in ("normal","normal_map","normals")): score+=9
        if any(m in maps for m in ("roughness","rough")): score+=9
        if a["tileable"]: score+=10
        else: score-=8
        if not maps: score-=20; why.append("PBR maps unverified")
    else:
        if a["lods"]: score+=10; why.append("LOD metadata")
        if a["triangles"]:
            if a["triangles"] > 1_000_000: score-=14; why.append("very high source mesh complexity")
            else: score+=4
        if a["scale_m"]: score+=4; why.append("physical scale known")
        if a["kind"] == "atlas": score-=15
    tex = a["max_texture_px"]
    if tex >= 4096: score+=10
    elif tex >= 2048: score+=7
    elif tex >= 1024: score+=2
    elif tex and tex < 1024: score-=10
    rating, count = a["rating_avg"], a["rating_count"]
    if rating is not None and count is not None and count>0:
        # Bayesian prior toward 3.5 until >=20 meaningful ratings.
        bayes=(count * rating + 20 * 3.5) / (count+20)
        score+=12 * ((bayes-2.5)/2.5)
        if count >= 10: score+=5
        elif count<3: score-=3
        why.append(f"Fab user rating {rating:.1f}/5 ({count} ratings), Bayesian adjusted")
    else:
        why.append("ratings and download count unavailable")
    if a["ai_generated"]: score-=12; why.append("AI-generated flag")
    if a["published_at"]:
        try:
            y=int(a["published_at"][:4]);
            if y >= 2024: score+=4
            elif y < 2019: score-=5
        except ValueError: pass
    return round(max(0,min(100,score)),1), why


def biome_name(description:str, requested:str|None=None) -> str:
    if requested:
        key=requested.strip().lower()
        key=BIOME_ALIASES.get(key,key)
        if key in BIOMES: return key
        raise ValueError(f"Unknown biome {requested}; choose {', '.join(BIOMES)}")
    low=description.lower()
    for term in ("wetland","swamp","marsh","bog","ruin","courtyard","forest","forrest","woodland","woods","meadow","grassland","field"):
        if term in low:
            b=BIOME_ALIASES.get(term, term)
            if b in BIOMES: return b
    return "forest"


def fitness(a:dict[str,Any], biome:str, role:str, description:str="") -> float:
    text=a["search_text"]
    words=BIOME_TERMS[biome]
    matches=sum(1 for x in words if x in text)
    explicitly=[t for t in a["biomes"] if biome in t or t in biome]
    role_fit=sum(1 for x in ROLE_TERMS[role] if x in text)
    score=8 * min(matches,4)+4 * min(role_fit,4)+ (10 if explicitly else 0)
    if role_of(a)!=role: score-=35
    description_terms=tokens(description) - set("the of and a with some around in wet old natural has for next to that near".split())
    score += min(12,2*len(description_terms & tokens(text)))
    if biome=="wetland" and role in ("groundcover","understory") and not any(v in text for v in ("wet", "marsh", "reed", "sedge", "rush", "swamp", "bog", "willow", "damp")):
        score-=16
    if role=="surface" and not (a["pbr_maps"] or a["kind"] in ("material","surface","texture")):
        score-=30
    return score


def rank(a:dict[str,Any], biome:str, role:str, description:str) -> dict[str,Any]:
    q, why=quality(a,role)
    relevance=fitness(a, biome, role, description)
    total=.65*q + .35*max(0,min(100,relevance))
    if a.get("assessment"):
        ann=a["assessment"]
        if ann.get("approved")==0: total=-999
        elif ann.get("approved")==1 and ann.get("role")==role:
            total += min(10, 10*float(ann.get("confidence") or 0))
    return {"asset":a,"role":role,"quality":q,"relevance":round(relevance,1),"score":round(total,1),"quality_notes":why}


def search(conn:sqlite3.Connection, description:str, role:str|None=None, biome:str|None=None,
           limit:int=20, min_quality:float=50, require_ratings:bool=False, sources:list[str]|None=None) -> list[dict[str,Any]]:
    b=biome_name(description,biome)
    roles=[role] if role else list(ROLES)
    if role and role not in ROLES: raise ValueError("Unknown role: "+role)
    ranked=[]
    for a in all_free(conn):
        if sources and not any(s.lower() in a["source"].lower() for s in sources): continue
        if require_ratings and not (a["rating_count"] and a["rating_count"]>=3): continue
        r=role_of(a)
        if r not in roles: continue
        item=rank(a,b,r,description)
        if item["quality"]<min_quality or item["score"]<0: continue
        if item["relevance"] < 5: continue
        ranked.append(item)
    return sorted(ranked,key=lambda x:(-x["score"],-x["quality"],x["asset"]["asset_id"]))[:limit]


def shortlist(conn:sqlite3.Connection, description:str, biome:str|None=None, min_quality:float=50,
              sources:list[str]|None=None, quotas:dict[str,int]|None=None,
              require_ratings:bool=False, multiplier:int=1) -> dict[str,Any]:
    b=biome_name(description,biome)
    quotas=quotas or BIOMES[b]
    results=[]
    seen=set()
    for role,count in quotas.items():
        ranked=search(conn,description,role=role,biome=b,limit=count*multiplier*4,
                      min_quality=min_quality,sources=sources,require_ratings=require_ratings)
        selected=0
        # Avoid identical material variants and over-representation by the same seller/title stem.
        publisher_counts:dict[str,int]={}
        for item in ranked:
            a=item["asset"]
            if a["asset_id"] in seen:continue
            pub=a["source"].lower()
            cap=max(2,int(math.ceil(count*multiplier*.65)))
            if publisher_counts.get(pub,0)>=cap:continue
            seen.add(a["asset_id"]); publisher_counts[pub]=publisher_counts.get(pub,0)+1
            results.append(item); selected+=1
            if selected>=count*multiplier:break
    return {"schema_version":1,"biome":b,"description":description,
            "strategy":"deterministic","quality_threshold":min_quality,
            "counts":{"requested":quotas,"selected":{r:sum(i["role"]==r for i in results) for r in quotas}},
            "assets":[{"asset_id":i["asset"]["asset_id"],"role":i["role"],"title":i["asset"]["title"],
                       "source":i["asset"]["source"],"url":i["asset"]["source_url"],
                       "licence":i["asset"]["licence"],"free_verified":bool(i["asset"]["free_verified"]),
                       "quality_score":i["quality"],"relevance_score":i["relevance"],"rank_score":i["score"],
                       "quality_evidence":i["quality_notes"],"kind":i["asset"]["kind"],
                       "preview_url":i["asset"]["preview_url"],"acquisition_status":"needs_user_action" if not i["asset"]["free_verified"] else "ready_to_download",
                       "local_path":None,"unreal_asset_path":None} for i in results]}


def annotate(conn:sqlite3.Connection, assessments:list[dict[str,Any]], model:str="composer-2.5") -> int:
    n=0
    for r in assessments:
        name=str(r.get("asset_id", ""))
        role=r.get("role")
        if role not in ROLES or not name: continue
        if not conn.execute("SELECT 1 FROM assets WHERE asset_id=?",(name,)).fetchone(): continue
        conf=max(0.,min(1.,float(r.get("confidence",0))))
        approved=1 if r.get("approved") is True else 0
        conn.execute("""INSERT INTO assessments VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(asset_id) DO UPDATE SET reviewed_role=excluded.reviewed_role,
        confidence=excluded.confidence, approved=excluded.approved, notes=excluded.notes,
        model=excluded.model,updated_at=excluded.updated_at""",
          (name,role,conf,approved,str(r.get("rationale", ""))[:500],model,dt.datetime.now(dt.timezone.utc).isoformat()))
        n+=1
    conn.commit();return n


# ----------------------------------------------------------------------
# CC0 download gating and acquisition planner
# ----------------------------------------------------------------------
"""Acquire verifiably CC0 files via public direct URLs; leave Fab claims to official UI."""

import hashlib
import json
import re
import shutil
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ALLOW_HOSTS=("ambientcg.com", "polyhaven.com", "polyhaven.org", "kenney.nl", "quaternius.com")
MAX_BYTES=400 * 1024 * 1024


def _host_allowed(url:str, extra_hosts:list[str]|None=None) -> bool:
    u=urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.hostname: return False
    hosts=[*ALLOW_HOSTS,*(extra_hosts or [])]
    return any(u.hostname == h or u.hostname.endswith("."+h) for h in hosts)


def find_direct(a:dict[str,Any], tier:str="2K-JPG") -> str|None:
    get=a.get("get_info")
    if not isinstance(get,dict):return None
    zips=get.get("zips",{})
    if isinstance(zips,dict):
        for key in (tier,"2K-PNG","2K-JPG","1K-JPG","4K-JPG"):
            if isinstance(zips.get(key),str) and zips[key].startswith("https://"): return zips[key]
    for key in ("download_url", "direct_url"):
        url=get.get(key)
        if isinstance(url,str) and url.startswith("https://"):return url
    return None


def inspect_acquisition(a:dict[str,Any]) -> dict[str,Any]:
    lic=a["licence"].lower()
    if not a["free_candidate"]:
        return {"method":"unavailable","reason":"Not confirmed as a free product"}
    if not a["free_verified"] or "cc0" not in lic:
        return {"method":"manual_fab" if ("fab" in lic or "quixel" in a["source"].lower() or "megascans" in a["source"].lower()) else "manual_provider",
                "url":a["source_url"],"reason":"Verify current free availability, license and acquisition through provider UI"}
    direct=find_direct(a)
    if direct and _host_allowed(direct):
        return {"method":"direct_cc0","url":direct,"reason":"Official direct CC0 asset URL in catalog"}
    return {"method":"manual_provider","url":a["source_url"],"reason":"No validated direct CC0 download URL"}


def download_to(url:str, target:Path, max_bytes:int=MAX_BYTES) -> tuple[int,str]:
    if not _host_allowed(url):raise ValueError("Direct-download host not in known CC0 provider allowlist")
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_suffix(target.suffix+".part")
    req=urllib.request.Request(url,headers={"User-Agent":"BiomeAssetUtility/0.1 (+local personal asset library)"})
    sha=hashlib.sha256();total=0
    try:
        with urllib.request.urlopen(req,timeout=40) as response:
            if not _host_allowed(response.geturl()):raise ValueError("Redirect to unapproved host")
            with temp.open("wb") as f:
                while True:
                    chunk=response.read(1024*1024)
                    if not chunk: break
                    total+=len(chunk)
                    if total>max_bytes:raise ValueError("Asset exceeds maximum configured download size")
                    sha.update(chunk);f.write(chunk)
        if total==0:raise ValueError("Empty download")
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True);raise
    return total,sha.hexdigest()


def safe_filename(asset_id:str, url:str) -> str:
    name=re.sub(r"[^a-zA-Z0-9_-]+","_",asset_id)[:85].strip("_") or "asset"
    path=urllib.parse.urlparse(url).path
    ext=Path(path).suffix.lower()
    if ext not in (".zip",".glb",".gltf",".fbx",".obj",".png",".jpg",".jpeg",".exr",".blend",".usd"):
        ext=".zip" # common provider query-string downloadable archives
    return name+ext


def acquire(conn:sqlite3.Connection, manifest:dict[str,Any], folder:Path, limit:int=8,
            role:str|None=None,dry_run:bool=True) -> dict[str,Any]:
    notes=[];completed=0
    for item in manifest.get("assets",[]):
        if role and item.get("role")!=role: continue
        row=conn.execute("SELECT raw FROM assets WHERE asset_id=?",(item["asset_id"],)).fetchone()
        if not row:
            notes.append({"asset_id":item["asset_id"],"status":"not_in_catalog"});continue
        a=json.loads(row["raw"])
        acq=inspect_acquisition(a)
        if acq["method"]!="direct_cc0":
            item["acquisition_status"]="needs_user_action"
            notes.append({"asset_id":a["asset_id"],"status":"manual","details":acq})
            continue
        url=acq["url"]
        path=folder / safe_filename(a["asset_id"],url)
        if path.exists() and path.stat().st_size>0:
            item["acquisition_status"]="on_disk";item["local_path"]=str(path)
            notes.append({"asset_id":a["asset_id"],"status":"already_on_disk","path":str(path)})
            continue
        if completed>=limit:
            notes.append({"asset_id":a["asset_id"],"status":"skipped_limit"});continue
        if dry_run:
            notes.append({"asset_id":a["asset_id"],"status":"would_download","url":url});completed+=1
            continue
        try:
            size,checksum=download_to(url,path)
            item["acquisition_status"]="on_disk";item["local_path"]=str(path)
            item["sha256"]=checksum
            notes.append({"asset_id":a["asset_id"],"status":"downloaded","bytes":size,"sha256":checksum,"path":str(path)})
            completed+=1
        except Exception as e:
            item["acquisition_status"]="failed"
            notes.append({"asset_id":a["asset_id"],"status":"failed","error":str(e)})
    return {"dry_run":dry_run,"results":notes,"completed_or_would_download":completed}


# ----------------------------------------------------------------------
# Optional local Cursor Composer review
# ----------------------------------------------------------------------
"""One optional Cursor Composer CLI review pass; all normal searches remain deterministic."""

import json
import re
import subprocess
from typing import Any


def _parse_result(stdout: str) -> dict[str,Any]:
    wrapper = json.loads(stdout)
    if not isinstance(wrapper,dict) or wrapper.get("is_error"):
        raise RuntimeError("Cursor agent returned an error")
    response=wrapper.get("result","")
    if isinstance(response,dict): return response
    if not isinstance(response,str): raise ValueError("Missing Cursor result text")
    response=response.strip()
    response=re.sub(r"^```(?:json)?\s*", "",response)
    response=re.sub(r"\s*```$", "",response)
    try: return json.loads(response)
    except json.JSONDecodeError:
        decoder=json.JSONDecoder()
        for start,c in enumerate(response):
            if c=="{":
                try: obj,_=decoder.raw_decode(response[start:]); return obj
                except ValueError: pass
        raise ValueError("Cursor result does not contain valid classification JSON")


def review_candidates(conn, manifest:dict[str,Any], model:str="composer-2.5", timeout:int=900, preview_index:dict[str,Any]|None=None) -> dict[str,Any]:
    if not model.startswith("composer"):
        raise ValueError("Only Cursor Composer models are allowed for this utility")
    items=manifest.get("assets",[])
    if not items: raise ValueError("No assets in shortlist")
    if len(items)>140: raise ValueError("Agent review limited to 140 shortlisted candidates per pass")
    compact=[{k:i.get(k) for k in ("asset_id","title","role","source","url","kind","licence","quality_score","rank_score","quality_evidence")}
             for i in items]
    allowed={i["asset_id"] for i in compact}
    prompt=(
       "You are a conservative metadata asset curator for a realistic Unreal Engine 5.8 LOTRO reconstruction. "
       "You are NOT creating code or downloading assets. Review the candidates ONCE as a batch, "
       "using only supplied metadata and actual listing evidence if you explicitly inspect it. "
       "Return one JSON object ONLY, no Markdown, with key assessments, a list of records: "
       '{"asset_id":string,"role":one of canopy|understory|groundcover|rocks|debris|surface|accents,'
       '"approved":boolean,"confidence":number between 0 and 1,"rationale":short string}. '
       "Do not invent IDs or downloads, assume ratings, or claim preview inspection you did not perform. "
       "Reject out-of-place, lowpoly, stylized, outdated-looking (if verifiable), oversized assembly, or "
       "clearly unsuitable assets. Do not force every candidate to be approved. "
       "Mark uncertain metadata as uncertain. Assign each only its best ecological role. "
       "No tool calls required; do not alter files or run shell commands. "
       "TARGET BIOME: " + manifest.get("description",manifest.get("biome","forest")) + "\n"
       "CANDIDATES:\n" + json.dumps(compact,ensure_ascii=False,separators=(",",":"))
    )
    if preview_index:
        sheets=preview_index.get("sheets",[])
        prompt += ("\nVISUAL REVIEW: Local contact sheets are provided: " + json.dumps(sheets) +
                   ". Use your image-reading tools to inspect them IF available before evaluating visual quality. "
                   "Index mapping: " + json.dumps(preview_index.get("entries",[]),separators=(",",":")) +
                   ". Never claim visual review of an image you could not actually open. "
                   "If thumbnails are missing, stay conservative and classify by metadata only.")
    command=["agent","-p","--mode=ask","--model",model,"--output-format","json",prompt]
    try:
        result=subprocess.run(command,capture_output=True,text=True,timeout=timeout,check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("Cursor 'agent' command not found; install Cursor CLI or skip --agent-review") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Cursor Composer review timed out; deterministic plan is unaffected") from exc
    if result.returncode:
        raise RuntimeError(f"Cursor Agent failed with code {result.returncode}: {result.stderr[:600]}")
    parsed=_parse_result(result.stdout)
    records=parsed.get("assessments")
    if not isinstance(records,list):raise ValueError("Agent failed to return assessments list")
    records=[r for r in records if isinstance(r,dict) and str(r.get("asset_id")) in allowed]
    n=annotate(conn,records,model=model)
    return {"reviewed":n,"candidate_count":len(items),"model":model,
            "message":"Assessments cached in SQLite; future `plan` runs are deterministic and do not call agent."}


# ----------------------------------------------------------------------
# Optional contact sheets
# ----------------------------------------------------------------------
"""Optional public thumbnail contact sheets for a single informed Composer review pass."""

import io
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TRUSTED_PREVIEW_HOSTS=("media.fab.com","fab.com","polyhaven.com","ambientcg.com",
                      "huggingface.co","cdn-avatars.huggingface.co","kenney.nl","quaternius.com")


def approved_preview(url:str)->bool:
    parsed=urllib.parse.urlparse(url)
    h=(parsed.hostname or "").lower()
    return parsed.scheme=="https" and any(h==base or h.endswith("."+base) for base in TRUSTED_PREVIEW_HOSTS)


def get_preview(url:str, max_bytes:int=5_000_000)->bytes:
    if not approved_preview(url):raise ValueError("Preview host is not on approved public list")
    req=urllib.request.Request(url,headers={"User-Agent":"BiomeAssetUtility/0.1"})
    with urllib.request.urlopen(req,timeout=15) as r:
        if not approved_preview(r.geturl()):raise ValueError("Preview redirected to untrusted host")
        content_type=r.headers.get("Content-Type","").lower()
        if content_type and not content_type.startswith("image/"):
            raise ValueError("Preview URL did not serve an image")
        b=r.read(max_bytes+1)
        if len(b)>max_bytes:raise ValueError("Preview image too large")
        return b


def make_sheets(manifest:dict[str,Any], outdir:Path, max_assets:int=120,
                per_sheet:int=20) -> dict[str,Any]:
    """Build local contact sheets. Pillow is an OPTIONAL dependency for this command."""
    try:
        from PIL import Image,ImageDraw,ImageFont,ImageOps
    except ImportError as exc:
        raise RuntimeError("Install Pillow to use previews: py -m pip install Pillow") from exc
    outdir.mkdir(parents=True,exist_ok=True)
    assets=manifest.get("assets",[])[:max_assets]
    cellw,cellh=252,230
    columns=5;rows=per_sheet//columns
    entries=[];paths=[]
    for start in range(0,len(assets),per_sheet):
        page=Image.new("RGB",(columns*cellw,rows*cellh),(240,239,237))
        draw=ImageDraw.Draw(page)
        for j,asset in enumerate(assets[start:start+per_sheet]):
            k=start+j;col=j%columns;row=j//columns
            x=col*cellw;y=row*cellh
            url=asset.get("preview_url") or ""
            state="no_preview"
            if url:
                try:
                    payload=get_preview(url)
                    thumb=Image.open(io.BytesIO(payload)).convert("RGB")
                    thumb.thumbnail((cellw-14,cellh-62))
                    target=(x+int((cellw-thumb.width)/2),y+5)
                    page.paste(thumb,target)
                    state="preview_loaded"
                except Exception as exc:
                    state="preview_unavailable"
            draw.text((x+7,y+cellh-54),f"{k+1}. {asset.get('role','?')}  [{asset.get('quality_score','?')}]",fill=(20,20,20))
            draw.text((x+7,y+cellh-36),str(asset.get("title",""))[:36],fill=(20,20,20))
            draw.text((x+7,y+cellh-18),str(asset.get("asset_id",""))[:37],fill=(55,55,55))
            entries.append({"index":k+1,"asset_id":asset.get("asset_id"),"sheet":(start//per_sheet)+1,
                            "cell":j+1,"thumbnail_status":state})
        dest=outdir/f"contact_sheet_{(start//per_sheet)+1:02d}.jpg"
        page.save(dest,quality=88)
        paths.append(str(dest.resolve()))
    payload={"biome":manifest.get("biome"),"description":manifest.get("description"),
             "sheets":paths,"entries":entries,"note":"Visual thumbnails only, not actual UE asset render validation"}
    idx=outdir/"index.json"
    idx.write_text(json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    return {"index":str(idx.resolve()),"sheets":paths,"thumbnail_ok":sum(e["thumbnail_status"]=="preview_loaded" for e in entries),
            "candidates":len(entries)}


# ----------------------------------------------------------------------
# Command line interface
# ----------------------------------------------------------------------
"""CLI: python -m biome_assets.cli ..."""

import argparse
import json
import shutil
import sys
import urllib.request
from pathlib import Path


DEFAULT_DB = Path.home()/".biome-assets"/"catalog.sqlite3"
DEFAULT_CACHE = Path.home()/".biome-assets"/"cache"


def emit(obj, output:Path|None=None):
    body=json.dumps(obj,ensure_ascii=False,indent=2)
    if output:
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(body+"\n",encoding="utf-8")
        print(f"Saved {output}")
    else:
        print(body)


def sync_catalog(dest:Path, max_bytes:int=100_000_000, url:str=CATALOG_URL) -> dict:
    """Download metadata, not asset files. Explicit allowlist, bounded and resumable local index."""
    if url != CATALOG_URL: raise ValueError("Only the pinned public metadata feed can be synced automatically")
    dest.parent.mkdir(parents=True,exist_ok=True)
    temp=dest.with_suffix(".download")
    count=0
    req=urllib.request.Request(url,headers={"User-Agent":"BiomeAssetUtility/0.1"})
    try:
        with urllib.request.urlopen(req,timeout=60) as response, temp.open("wb") as f:
            if not response.geturl().startswith("https://huggingface.co/") and not response.geturl().startswith("https://cdn-lfs.hf.co/") and not response.geturl().startswith("https://cas-bridge.xethub.hf.co/"):
                raise ValueError("Unexpected catalog redirect; refusing")
            while True:
                chunk=response.read(1024*1024)
                if not chunk: break
                count+=len(chunk)
                if count>max_bytes:raise ValueError("Catalog too large")
                f.write(chunk)
        with temp.open("r",encoding="utf-8") as f:
            line=f.readline()
            if not isinstance(json.loads(line),dict):raise ValueError("Invalid catalog JSONL")
        temp.replace(dest)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return {"path":str(dest),"bytes":count,"source":CATALOG_URL}


def build_parser():
    p=argparse.ArgumentParser(prog="biome-assets",description="Free-asset catalog, quality ranker, biome manifests and safe acquisition")
    p.add_argument("--db",type=Path,default=DEFAULT_DB,help="SQLite database (default ~/.biome-assets/catalog.sqlite3)")
    sp=p.add_subparsers(dest="command",required=True)
    cat=sp.add_parser("catalog",help="Sync metadata, import catalogs, or show statistics")
    sub=cat.add_subparsers(dest="subcommand",required=True)
    s=sub.add_parser("sync",help="Fetch public metadata snapshot, then index it (not the actual assets)")
    s.add_argument("--to",type=Path,default=DEFAULT_CACHE/"metadata.jsonl")
    s.add_argument("--no-import",action="store_true")
    imp=sub.add_parser("import",help="Import JSONL metadata or Fab library JSON export")
    imp.add_argument("path",type=Path)
    imp.add_argument("--limit",type=int,default=0)
    sub.add_parser("stats",help="Count available and free-candidate records")
    q=sp.add_parser("search",help="Deterministic category search")
    q.add_argument("description",help="Biome description, e.g. 'temperate forest with moss and oak'")
    q.add_argument("--biome",choices=list(BIOMES))
    q.add_argument("--role",choices=ROLES)
    q.add_argument("--top",type=int,default=15)
    q.add_argument("--min-quality",type=float,default=55)
    q.add_argument("--source",action="append")
    q.add_argument("--require-ratings",action="store_true")
    q.add_argument("--out",type=Path)
    pl=sp.add_parser("plan",help="Build a balanced 7-role generator manifest")
    pl.add_argument("description")
    pl.add_argument("--biome",choices=list(BIOMES))
    pl.add_argument("--min-quality",type=float,default=55)
    pl.add_argument("--source",action="append")
    pl.add_argument("--require-ratings",action="store_true")
    pl.add_argument("--candidate-multiplier",type=int,default=1,help="Increase candidate pool when planning one Composer review pass")
    pl.add_argument("--out",type=Path,default=Path("biome_manifest.json"))
    rev=sp.add_parser("review",help="ONE optional cached Cursor Composer agent pass over a shortlisted manifest")
    rev.add_argument("manifest",type=Path)
    rev.add_argument("--model",default="composer-2.5")
    rev.add_argument("--timeout",type=int,default=900)
    rev.add_argument("--preview-index",type=Path,help="Optional index.json created by previews, for one visual Composer pass")
    pv=sp.add_parser("previews",help="Create contact sheets of public thumbnails for visual review (requires Pillow)")
    pv.add_argument("manifest",type=Path)
    pv.add_argument("--out",type=Path,default=Path("biome_previews"))
    pv.add_argument("--max",type=int,default=120)
    dl=sp.add_parser("download",help="Auto-download only verified public CC0 direct URLs; Fab remains official UI")
    dl.add_argument("manifest",type=Path)
    dl.add_argument("--to",type=Path,default=DEFAULT_CACHE/"assets")
    dl.add_argument("--limit",type=int,default=8)
    dl.add_argument("--role",choices=ROLES)
    dl.add_argument("--yes",action="store_true",help="Actually download direct CC0 files (default: dry run)")
    return p


def main(argv:list[str]|None=None) -> int:
    parser=build_parser()
    args=parser.parse_args(argv)
    try:
        conn=connect(args.db)
        if args.command=="catalog":
            if args.subcommand=="sync":
                result=sync_catalog(args.to)
                if not args.no_import:result["indexed_rows"]=load_catalog(conn,args.to)
                emit(result)
            elif args.subcommand=="import":
                if not args.path.is_file():raise FileNotFoundError(args.path)
                emit({"indexed_rows":load_catalog(conn,args.path,args.limit),"path":str(args.path)})
            else:
                rows=conn.execute("SELECT count(*) total,sum(free_candidate) free_candidates,sum(free_verified) verified_free FROM assets").fetchone()
                emit({"total":rows["total"],"free_candidates":rows["free_candidates"] or 0,
                      "verified_free":rows["verified_free"] or 0,
                      "composer_reviewed":conn.execute("SELECT count(*) FROM assessments").fetchone()[0]})
        elif args.command=="search":
            results=search(conn,args.description,role=args.role,biome=args.biome,limit=args.top,
                           min_quality=args.min_quality,require_ratings=args.require_ratings,sources=args.source)
            emit([{"asset_id":r["asset"]["asset_id"],"title":r["asset"]["title"],"role":r["role"],
                  "score":r["score"],"quality":r["quality"],"source":r["asset"]["source"],
                  "listing":r["asset"]["source_url"],"free_verified":bool(r["asset"]["free_verified"]),
                  "quality_evidence":r["quality_notes"]} for r in results],args.out)
        elif args.command=="plan":
            if args.candidate_multiplier<1 or args.candidate_multiplier>5:
                raise ValueError("candidate multiplier must be 1–5")
            plan=shortlist(conn,args.description,args.biome,min_quality=args.min_quality,
                           sources=args.source,require_ratings=args.require_ratings,
                           multiplier=args.candidate_multiplier)
            emit(plan,args.out)
            total=len(plan["assets"])
            print(f"Selected {total} candidates across 7 categories; Fab records need free/listing/license verification.")
        elif args.command=="review":
            m=json.loads(args.manifest.read_text(encoding="utf-8"))
            index=json.loads(args.preview_index.read_text(encoding="utf-8")) if args.preview_index else None
            emit(review_candidates(conn,m,args.model,args.timeout,preview_index=index))
            print("Now rerun `plan` to apply cached Composer assessments to a deterministic shortlist.")
        elif args.command=="previews":
            m=json.loads(args.manifest.read_text(encoding="utf-8"))
            emit(make_sheets(m,args.out,args.max))
        elif args.command=="download":
            m=json.loads(args.manifest.read_text(encoding="utf-8"))
            result=acquire(conn,m,args.to,args.limit,args.role,dry_run=not args.yes)
            if args.yes:
                args.manifest.write_text(json.dumps(m,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            emit(result)
            if not args.yes: print("DRY RUN. Add --yes to download eligible CC0 direct URLs; Fab assets require manual acquisition.")
        return 0
    except (ValueError,OSError,RuntimeError) as exc:
        print(f"Error: {exc}",file=sys.stderr)
        return 2

if __name__=="__main__":
    raise SystemExit(main())
