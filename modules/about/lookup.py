"""What the open music databases say about an artist, an album and a song.

MusicBrainz identifies them and links to their Wikidata and Discogs entries; Wikipedia gives
the text (through Wikidata, so it is the right article and in the reader's language when one
exists); Discogs adds styles, label and its members' rating. No keys: all three answer
anyone who names their client. A song has no such link, so its article is searched for and
taken only when its opening names both the song and the artist."""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import quote

UA = "mscOS-about/1.0 (+https://github.com/mescon/mscOS)"
MB = "https://musicbrainz.org/ws/2"
_mb_lock, _mb_last = asyncio.Lock(), 0.0
RECEPTION = [re.compile(p, re.I) for p in (r"critical (reception|response)|kritikernas|recensioner", r"reception|mottagande", r"reviews|kritik")]
# what streaming services append to a title: "(Remastered 2011)", " - Live", "[Deluxe Edition]"
EDITION = re.compile(r"\s*[(\[][^)\]]*(remaster|deluxe|edition|version|bonus|anniversary|expanded|mono|stereo|live|feat\.|with )[^)\]]*[)\]]"
                     r"|\s+-\s+.*(remaster|version|edit|mix|live|mono|stereo).*$", re.I)


class Unavailable(Exception):
    """A database did not answer: nothing to conclude, ask again later."""


def plain(title: str) -> str:
    """The title without edition notes; unchanged when nothing would be left."""
    t = EDITION.sub("", title or "").strip()
    return t or (title or "").strip()


def _fold(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").casefold()).strip()


def clip(text: str, limit: int) -> str:
    """Whole sentences up to about limit characters."""
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind(".\n"))
    return (cut[:end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "…").strip()


def sections(text: str) -> tuple[str, dict[str, str]]:
    """A Wikipedia plain-text extract: its introduction and its top-level sections by heading."""
    parts = re.split(r"\n==\s*([^=\n]+?)\s*==\n", "\n" + (text or ""))
    return parts[0].strip(), {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def reception(secs: dict[str, str]) -> str:
    """What the critics said: the section (or sub-section) headed so, the most exact heading first."""
    parts = []   # (heading, text) at every level
    for head, body in secs.items():
        sub = re.split(r"\n=+\s*([^=\n]+?)\s*=+\n", "\n" + body)
        parts.append((head, sub[0]))
        parts += [(sub[i], sub[i + 1]) for i in range(1, len(sub) - 1, 2)]
    for want in RECEPTION:
        for head, body in parts:
            paras = [p.strip() for p in body.split("\n") if len(p.strip()) > 80]
            if want.search(head) and paras:
                return clip("\n\n".join(paras[:2]), 1100)
    return ""


def discogs_text(s: str) -> str:
    """Discogs markup ([a=Name], [l=Label], [b]..[/b], [url=..]text[/url]) as plain text."""
    # a reference by number alone has no name to show: the sentence goes
    s = " ".join(x for x in re.split(r"(?<=[.!?])\s+", s or "") if not re.search(r"\[[almr]=?\d+\]", x))
    s = re.sub(r"\[(?:a|l|m|r)=?([^\]]*)\]", lambda m: "" if m.group(1).isdigit() else re.sub(r" \(\d+\)$", "", m.group(1)), s or "")
    s = re.sub(r"\[url=[^\]]*\]([^\[]*)\[/url\]", r"\1", s)
    return re.sub(r"\[/?[a-z]+\]", "", s).strip()


async def _json(http, url: str, params: dict | None = None, headers: dict | None = None):
    try:
        async with http.get(url, params=params, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})}) as r:
            return await r.json(content_type=None) if r.status == 200 else None
    except (OSError, ValueError, asyncio.TimeoutError):
        return None


async def _mb(http, path: str, **params):
    """MusicBrainz asks for at most one request a second."""
    global _mb_last
    async with _mb_lock:
        wait = _mb_last + 1.1 - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        for attempt in range(3):
            try:
                if (d := await _json(http, f"{MB}/{path}", {"fmt": "json", **params})) is not None:
                    return d
            finally:
                _mb_last = time.monotonic()
            await asyncio.sleep(1.5 * (attempt + 1))   # 503: asked too often
        raise Unavailable("MusicBrainz")


def _q(s: str) -> str:
    return '"' + re.sub(r'(["\\])', r"\\\1", s) + '"'


def _rels(d: dict) -> dict[str, str]:
    """{"wikidata": "Q…", "discogs": "<path after discogs.com>"} from MusicBrainz' links."""
    out = {}
    for r in (d or {}).get("relations", []):
        url = (r.get("url") or {}).get("resource", "")
        if r.get("type") == "wikidata" and (m := re.search(r"/(Q\d+)$", url)):
            out["wikidata"] = m.group(1)
        elif r.get("type") == "discogs" and (m := re.search(r"discogs\.com/((?:artist|master|release|label)/\d+)", url)):
            out.setdefault("discogs", m.group(1))
    return out


def _genres(d: dict) -> list[str]:
    g = sorted((d or {}).get("genres", []), key=lambda x: -x.get("count", 0))
    return [x["name"] for x in g[:4] if x.get("name")]


async def wiki_titles(http, qids: list[str], langs: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """Wikidata id -> (language, article title), the first of langs that has one."""
    if not qids:
        return {}
    d = await _json(http, "https://www.wikidata.org/w/api.php", {
        "action": "wbgetentities", "ids": "|".join(qids), "props": "sitelinks",
        "sitefilter": "|".join(f"{l}wiki" for l in langs), "format": "json"})
    out = {}
    for q, e in ((d or {}).get("entities") or {}).items():
        for l in langs:
            if t := ((e.get("sitelinks") or {}).get(f"{l}wiki") or {}).get("title"):
                out[q] = (l, t)
                break
    return out


async def wikidata_image(http, qid: str) -> str:
    """The picture Wikidata has of it (on Wikimedia Commons, so free to show), or ""."""
    d = await _json(http, "https://www.wikidata.org/w/api.php", {"action": "wbgetclaims", "entity": qid, "property": "P18", "format": "json"})
    for c in ((d or {}).get("claims") or {}).get("P18", []):
        name = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(name, str) and name:
            return "https://commons.wikimedia.org/wiki/Special:FilePath/" + quote(name.replace(" ", "_")) + "?width=640"
    return ""


async def wiki_article(http, lang: str, title: str) -> dict | None:
    d = await _json(http, f"https://{lang}.wikipedia.org/w/api.php", {
        "action": "query", "prop": "extracts|pageprops|pageimages", "ppprop": "disambiguation", "piprop": "thumbnail", "pithumbsize": 640, "explaintext": 1, "exsectionformat": "wiki",
        "redirects": 1, "titles": title, "format": "json", "formatversion": 2})
    page = (((d or {}).get("query") or {}).get("pages") or [{}])[0]
    if not page.get("extract") or "disambiguation" in (page.get("pageprops") or {}):
        return None
    intro, secs = sections(page["extract"])
    # "This article is about the album. For the …, see …": a note to the reader, not the text
    intro = re.sub(r"^(Denna artikel handlar om|För andra betydelser|Den här artikeln handlar om)[^\n]*\n+", "", intro)
    return {"lang": lang, "title": page.get("title", title), "intro": intro, "sections": secs,
            "image": (page.get("thumbnail") or {}).get("source", ""),   # the article's own picture, when it is a free one
            "url": f"https://{lang}.wikipedia.org/wiki/" + page.get("title", title).replace(" ", "_")}


async def wiki_song(http, artist: str, title: str, langs: tuple[str, ...]) -> dict | None:
    """Searched for, so checked: the opening must name the song and the artist."""
    for lang in dict.fromkeys((*langs, "en")):
        d = await _json(http, f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "query", "list": "search", "srsearch": f"{_q(title)} {artist}", "srlimit": 4, "format": "json"})
        for hit in ((d or {}).get("query") or {}).get("search", []):
            if _fold(title) not in _fold(hit.get("title", "")):
                continue
            a = await wiki_article(http, lang, hit["title"])
            head = _fold(a["intro"][:600]) if a else ""
            if a and _fold(artist) in head and _fold(title) in head and re.search(r"\b(song|single|composition|track|låt|singel|sång)\b", head):
                return a
    return None


def _entry(a: dict | None, limit: int = 900) -> dict:
    if not a:
        return {"text": "", "links": []}
    return {"text": clip(a["intro"], limit), "lang": a["lang"], "links": [{"name": "Wikipedia", "url": a["url"]}],
            **({"image": a["image"]} if a.get("image") else {})}


async def discogs(http, path: str, token: str = ""):
    return await _json(http, f"https://api.discogs.com/{path.replace('artist/', 'artists/').replace('master/', 'masters/').replace('release/', 'releases/')}",
                       headers={"Authorization": f"Discogs token={token}"} if token else None)


async def artist(http, name: str, langs: tuple[str, ...], token: str = "") -> dict | None:
    """None: not found (or the databases did not answer)."""
    d = await _mb(http, "artist", query=f"artist:{_q(name)}", limit=5)
    hits = [a for a in (d or {}).get("artists", []) if a.get("score", 0) >= 90
            and _fold(name) in {_fold(a.get("name")), *(_fold(x.get("name")) for x in a.get("aliases", []))}]
    if not hits:
        return None
    full = await _mb(http, f"artist/{hits[0]['id']}", inc="url-rels+genres")
    rels = _rels(full)
    wt = await wiki_titles(http, [rels["wikidata"]], langs) if "wikidata" in rels else {}
    art = await wiki_article(http, *wt[rels["wikidata"]]) if wt else None
    out = {"name": full.get("name", name), "mbid": full["id"], **_entry(art)}
    if not out.get("image") and "wikidata" in rels and (img := await wikidata_image(http, rels["wikidata"])):
        out["image"] = img   # many articles on bands show a logo that is not free: Wikidata's portrait instead
    span, area = full.get("life-span") or {}, (full.get("area") or {}).get("name")
    out["facts"] = [f for f in (
        ["from", area] if area else None,
        ["active" if full.get("type") == "Group" else "born", span["begin"][:4]] if span.get("begin") else None,
        ["genre", ", ".join(_genres(full))] if _genres(full) else None) if f]
    if "discogs" in rels:
        out["links"].append({"name": "Discogs", "url": f"https://www.discogs.com/{rels['discogs']}"})
        if not out["text"] and (dg := await discogs(http, rels["discogs"], token)) and dg.get("profile"):
            out.update(text=clip(discogs_text(dg["profile"]), 900), lang="en")
    out["links"].append({"name": "MusicBrainz", "url": f"https://musicbrainz.org/artist/{full['id']}"})
    return out


async def album(http, mbid: str, artist_name: str, title: str, langs: tuple[str, ...], token: str = "") -> dict | None:
    d = await _mb(http, "release-group", query=f"releasegroup:{_q(plain(title))} AND arid:{mbid}", limit=5)
    hits = [g for g in (d or {}).get("release-groups", []) if g.get("score", 0) >= 85]
    if not hits:
        return None
    hits.sort(key=lambda g: (_fold(g.get("title")) != _fold(plain(title)), -g.get("score", 0)))
    full = await _mb(http, f"release-group/{hits[0]['id']}", inc="url-rels+genres")
    rels = _rels(full)
    wt = await wiki_titles(http, [rels["wikidata"]], langs) if "wikidata" in rels else {}
    art = await wiki_article(http, *wt[rels["wikidata"]]) if wt else None
    out = {"name": full.get("title", title), **_entry(art)}
    rec = reception(art["sections"]) if art else ""
    if not rec and art and art["lang"] != "en" and (en := await wiki_titles(http, [rels["wikidata"]], ("en",))):
        # the reviews are mostly written up in the English article
        if (ea := await wiki_article(http, *en[rels["wikidata"]])) and (rec := reception(ea["sections"])):
            out["review"] = {"text": rec, "lang": "en", "url": ea["url"]}
    elif rec:
        out["review"] = {"text": rec, "lang": art["lang"], "url": art["url"]}
    facts = [["released", full["first-release-date"][:4]]] if full.get("first-release-date") else []
    genres = _genres(full)
    if "discogs" in rels:
        out["links"].append({"name": "Discogs", "url": f"https://www.discogs.com/{rels['discogs']}"})
        dg = await discogs(http, rels["discogs"], token) or {}
        genres = (dg.get("styles") or dg.get("genres") or genres)[:4]
        rel = await discogs(http, f"release/{dg['main_release']}", token) if dg.get("main_release") else None
        if rel:
            if rel.get("labels"):
                facts.append(["label", rel["labels"][0].get("name", "")])
            rating = (rel.get("community") or {}).get("rating") or {}
            if rating.get("count", 0) >= 10:
                facts.append(["discogs", f"{rating['average']:.1f}".rstrip("0").rstrip("."), rating["count"]])
            if rel.get("notes") and not out["text"]:
                out.update(text=clip(discogs_text(rel["notes"]), 700), lang="en")
    if genres:
        facts.append(["genre", ", ".join(genres)])
    out["facts"] = facts
    out["links"].append({"name": "MusicBrainz", "url": f"https://musicbrainz.org/release-group/{full['id']}"})
    return out


async def track(http, artist_name: str, title: str, langs: tuple[str, ...]) -> dict | None:
    a = await wiki_song(http, artist_name, plain(title), langs)
    if not a:
        return None
    out = {"name": plain(title), **_entry(a, 800), "facts": []}
    if rec := reception(a["sections"]):
        out["review"] = {"text": rec, "lang": a["lang"], "url": a["url"]}
    return out


if __name__ == "__main__":  # python3 lookup.py "Artist" "Album" "Song" [lang]
    import json
    import sys

    from aiohttp import ClientSession, ClientTimeout

    async def main():
        langs = tuple(dict.fromkeys((*sys.argv[4:5], "en")))
        async with ClientSession(timeout=ClientTimeout(total=15)) as http:
            a = await artist(http, sys.argv[1], langs)
            print(json.dumps({"artist": a, "album": await album(http, a["mbid"], sys.argv[1], sys.argv[2], langs) if a else None,
                              "track": await track(http, sys.argv[1], sys.argv[3], langs)}, ensure_ascii=False, indent=1))
    asyncio.run(main())
