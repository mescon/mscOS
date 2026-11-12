"""What the open music databases say about an artist, an album and a song.

MusicBrainz identifies them and links to their Wikidata and Discogs entries; Wikipedia gives
the text (through Wikidata, so it is the right article and in the reader's language when one
exists); Discogs adds styles, label and its members' rating. No keys: all three answer
anyone who names their client. A song has no such link, so its article is searched for and
taken only when its opening names both the song and the artist."""
from __future__ import annotations

import asyncio
import json
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


async def _json(http, url: str, params: dict | None = None, headers: dict | None = None, missing=None):
    """The answer as JSON; None when there was none. A 404 gives `missing` (a plain "not there")."""
    try:
        async with http.get(url, params=params, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})}) as r:
            if r.status == 404:
                return missing
            return await r.json(content_type=None) if r.status == 200 else None
    except (OSError, ValueError, asyncio.TimeoutError):
        return None


async def _mb(http, path: str, **params):
    """MusicBrainz asks for at most one request a second."""
    global _mb_last
    async with _mb_lock:
        wait = _mb_last + 1.0 - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        for attempt in range(3):
            try:
                if (d := await _json(http, f"{MB}/{path}", {"fmt": "json", **params}, missing={})) is not None:
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


async def discogs(http, path: str, token: str = ""):
    return await _json(http, f"https://api.discogs.com/{path.replace('artist/', 'artists/').replace('master/', 'masters/').replace('release/', 'releases/')}",
                       headers={"Authorization": f"Discogs token={token}"} if token else None)


def _credited(hits: list[dict], name: str) -> str:
    """The id of the artist credited under this name on the first good hit, or ""."""
    for h in hits:
        if h.get("score", 0) < 85:
            continue
        for c in h.get("artist-credit", []):
            a = c.get("artist") or {}
            if a.get("id") and _fold(name) in (_fold(c.get("name")), _fold(a.get("name"))):
                return a["id"]
    return ""


async def find_artist(http, name: str, album_title: str = "", title: str = "", spotify_id: str = "") -> str:
    """Which artist this is, as a MusicBrainz id ("" when not found). Several artists share a
    name, so the name is asked about last: first the player's own id for the artist (MusicBrainz
    links artists to their Spotify pages), then who made an album or a song of this title."""
    if spotify_id:
        d = await _mb(http, "url", resource=f"https://open.spotify.com/artist/{spotify_id}", inc="artist-rels")
        for r in d.get("relations", []):
            if (r.get("artist") or {}).get("id"):
                return r["artist"]["id"]
    if album_title:
        d = await _mb(http, "release-group", query=f"releasegroup:{_q(plain(album_title))} AND artist:{_q(name)}", limit=5)
        if mbid := _credited(d.get("release-groups", []), name):
            return mbid
    if title:
        d = await _mb(http, "recording", query=f"recording:{_q(plain(title))} AND artist:{_q(name)}", limit=5)
        if mbid := _credited(d.get("recordings", []), name):
            return mbid
    d = await _mb(http, "artist", query=f"artist:{_q(name)}", limit=5)
    hits = [a for a in d.get("artists", []) if a.get("score", 0) >= 90
            and _fold(name) in {_fold(a.get("name")), *(_fold(x.get("name")) for x in a.get("aliases", []))}]
    return hits[0]["id"] if hits else ""

# ---- more than one source: each writes its own text, and the reader chooses
LINK_HOSTS = (("allmusic.com", "AllMusic"), ("rateyourmusic.com", "Rate Your Music"), ("genius.com", "Genius"),
              ("last.fm", "Last.fm"), ("bandcamp.com", "Bandcamp"), ("secondhandsongs.com", "SecondHandSongs"),
              ("whosampled.com", "WhoSampled"), ("setlist.fm", "setlist.fm"), ("songkick.com", "Songkick"))


def out_links(d: dict) -> list[dict]:
    """Where else to read, from MusicBrainz' own links: the other databases, and the official site."""
    out, seen = [], set()
    for r in (d or {}).get("relations", []):
        url = (r.get("url") or {}).get("resource", "")
        if not url.startswith("https://") or re.search(r"[\s\"'<>]", url):
            continue
        host = re.sub(r"^www\.", "", url.split("/")[2].lower())
        name = next((n for h, n in LINK_HOSTS if host == h or host.endswith("." + h)), None)
        if name is None and r.get("type") == "official homepage":
            name = "site"   # the page gives it its word
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "url": url})
    return out


def _text(source: str, text: str, lang: str, url: str, limit: int = 1100) -> dict | None:
    text = clip(text or "", limit)
    return {"source": source, "text": text, "lang": lang, "url": url} if len(text) > 40 else None


def lastfm_clean(html: str) -> str:
    """Last.fm's write-up as plain text, without its "Read more" link and licence line."""
    s = re.split(r"<a href=\"https?://www\.last\.fm/[^\"]*\">Read more on Last\.fm", html or "")[0]
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&quot;", '"'), ("&apos;", "'"), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        s = s.replace(a, b)
    return s.strip()


async def lastfm(http, key: str, method: str, node: str, lang: str, **params) -> dict | None:
    """A Last.fm write-up ({"text", "url"}), with the listener's own API key. None: none, or no key."""
    if not key:
        return None
    for lg in dict.fromkeys((lang, "en")):   # many write-ups exist in English only
        d = await _json(http, "https://ws.audioscrobbler.com/2.0/", {
            "method": method, "api_key": key, "format": "json", "autocorrect": 1, "lang": lg, **params})
        e = (d or {}).get(node) or {}
        wiki = e.get("bio") or e.get("wiki") or {}
        text = lastfm_clean(wiki.get("content") or wiki.get("summary") or "")
        url = e.get("url") or ""
        if len(text) > 40:
            return {"text": text, "lang": lg, "url": url if url.startswith("https://www.last.fm/") else "https://www.last.fm/"}
    return None


async def wikidata_facts(http, qid: str, lang: str, props: dict[str, str], most: int = 5) -> list[list]:
    """Facts Wikidata holds as links to other entries (members, awards): [[key, "A, B, C"], ...]."""
    d = await _json(http, "https://www.wikidata.org/w/api.php", {"action": "wbgetentities", "ids": qid, "props": "claims", "format": "json"})
    claims = (((d or {}).get("entities") or {}).get(qid) or {}).get("claims") or {}
    ids: dict[str, list[str]] = {}
    for prop in props:
        for c in claims.get(prop, []):
            v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if c.get("rank") != "deprecated" and isinstance(v, dict) and v.get("id"):
                ids.setdefault(prop, []).append(v["id"])
    flat = [q for qs in ids.values() for q in qs[:most]]
    if not flat:
        return []
    d = await _json(http, "https://www.wikidata.org/w/api.php", {
        "action": "wbgetentities", "ids": "|".join(dict.fromkeys(flat)), "props": "labels", "languages": f"{lang}|en", "format": "json"})
    ents = (d or {}).get("entities") or {}
    label = lambda q: ((ents.get(q, {}).get("labels") or {}).get(lang) or (ents.get(q, {}).get("labels") or {}).get("en") or {}).get("value", "")   # noqa: E731
    out = []
    for prop, key in props.items():
        names = list(dict.fromkeys(n for n in (label(q) for q in ids.get(prop, [])[:most]) if n))
        if names:
            out.append([key, ", ".join(names)])
    return out


# ---- who made it: MusicBrainz' credits for a release and each of its recordings
ROLES = {"producer": "producer", "engineer": "engineer", "recording": "engineer", "mix": "mix", "mastering": "mastering",
         "arranger": "arranger", "composer": "composer", "lyricist": "lyricist", "writer": "writer", "conductor": "conductor"}


def credits_of(relations: list) -> dict:
    """{"people": [[name, "electric guitar, lead vocals"], ...], "roles": [[key, "Name, Name"], ...]}"""
    people: dict[str, list[str]] = {}
    roles: dict[str, list[str]] = {}

    def take(rels):
        for r in rels or []:
            a, t = (r.get("artist") or {}).get("name"), r.get("type")
            if a and t in ("instrument", "vocal", "performer"):
                what = [x for x in r.get("attributes", []) if x not in ("guest", "solo", "additional")] or (["vocals"] if t == "vocal" else [])
                people.setdefault(a, [])
                people[a] += [w for w in what if w not in people[a]]
            elif a and t in ROLES and a not in roles.setdefault(ROLES[t], []):
                roles[ROLES[t]].append(a)
            if r.get("work"):
                take(r["work"].get("relations"))
    take(relations)
    order = ("composer", "lyricist", "writer", "arranger", "conductor", "producer", "engineer", "mix", "mastering")
    return {"people": [[n, ", ".join(w)] for n, w in people.items()][:24],
            "roles": [[k, ", ".join(roles[k][:6])] for k in order if roles.get(k)]}


async def release_credits(http, rg: dict) -> tuple[dict, dict]:
    """(the album's credits, {folded track title: {"credits", "length"}}) from the release group's
    first official release, which is the one that tends to be credited in full."""
    rels = sorted((r for r in rg.get("releases", []) if r.get("status") == "Official" and r.get("id")),
                  key=lambda r: r.get("date") or "9999")
    if not rels:
        return {}, {}
    d = await _mb(http, f"release/{rels[0]['id']}", inc="artist-rels+recordings+recording-level-rels+work-rels+work-level-rels")
    tracks, everything = {}, list(d.get("relations", []))
    for m in d.get("media", []):
        for t in m.get("tracks", []):
            rec = t.get("recording") or {}
            c = credits_of(rec.get("relations", []))
            everything += rec.get("relations", [])
            if c["people"] or c["roles"]:
                tracks.setdefault(_fold(plain(t.get("title", ""))), {"credits": c, "length": t.get("length")})
    whole = credits_of(everything)
    whole["people"] = whole["people"][:14]
    return (whole if whole["people"] or whole["roles"] else {}), tracks


async def _article(http, qid: str, langs: tuple[str, ...]) -> dict | None:
    """The entry's Wikipedia article in the first of langs that has one."""
    wt = await wiki_titles(http, [qid], langs) if qid else {}
    return await wiki_article(http, *wt[qid]) if wt else None


async def _nothing():
    return None


async def artist(http, mbid: str, langs: tuple[str, ...], token: str = "", lastfm_key: str = "") -> dict | None:
    """What is known about the artist with this MusicBrainz id. The sources are asked at the
    same time: each takes a second or so, and one after another that adds up."""
    full = await _mb(http, f"artist/{mbid}", inc="url-rels+genres")
    if not full.get("id"):
        return None
    name, rels = full.get("name", ""), _rels(full)
    qid, group = rels.get("wikidata", ""), full.get("type") == "Group"

    async def lf():
        return await lastfm(http, lastfm_key, "artist.getinfo", "artist", langs[0], mbid=mbid) \
            or await lastfm(http, lastfm_key, "artist.getinfo", "artist", langs[0], artist=name)

    art, lfm, picture, more, dg = await asyncio.gather(
        _article(http, qid, langs), lf(),
        wikidata_image(http, qid) if qid else _nothing(),
        wikidata_facts(http, qid, langs[0], {**({"P527": "members"} if group else {}), "P166": "awards"}, 6) if qid else _nothing(),
        discogs(http, rels["discogs"], token) if "discogs" in rels else _nothing())
    out = {"name": name, "mbid": full["id"], "links": [{"name": "Wikipedia", "url": art["url"]}] if art else []}
    texts = [_text("Wikipedia", art["intro"], art["lang"], art["url"])] if art else []
    if lfm:
        texts.append(_text("Last.fm", lfm["text"], lfm["lang"], lfm["url"]))
    out["image"] = (art or {}).get("image") or picture or ""
    span, area = full.get("life-span") or {}, (full.get("area") or {}).get("name")
    out["facts"] = [f for f in (
        ["from", area] if area else None,
        ["active" if group else "born", span["begin"][:4]] if span.get("begin") else None,
        ["genre", ", ".join(_genres(full))] if _genres(full) else None) if f] + (more or [])
    if "discogs" in rels:
        url = f"https://www.discogs.com/{rels['discogs']}"
        out["links"].append({"name": "Discogs", "url": url})
        if dg and dg.get("profile"):
            texts.append(_text("Discogs", discogs_text(dg["profile"]), "en", url))
    out["links"] += [l for l in out_links(full) if l["name"] not in {x["name"] for x in out["links"]}]
    out["links"].append({"name": "MusicBrainz", "url": f"https://musicbrainz.org/artist/{full['id']}"})
    out["texts"] = [t for t in texts if t]
    return out


async def album(http, mbid: str, artist_name: str, title: str, langs: tuple[str, ...], token: str = "", lastfm_key: str = "") -> dict | None:
    d = await _mb(http, "release-group", query=f"releasegroup:{_q(plain(title))} AND arid:{mbid}", limit=5)
    hits = [g for g in (d or {}).get("release-groups", []) if g.get("score", 0) >= 85]
    if not hits:
        return None
    hits.sort(key=lambda g: (_fold(g.get("title")) != _fold(plain(title)), -g.get("score", 0)))
    full = await _mb(http, f"release-group/{hits[0]['id']}", inc="url-rels+genres+releases")
    if not full.get("id"):
        return None
    rels = _rels(full)
    qid = rels.get("wikidata", "")

    async def article():
        """The article, and what the critics said: from it, or from the English one, where reviews are mostly written up."""
        art = await _article(http, qid, langs)
        rec = reception(art["sections"]) if art else ""
        if rec:
            return art, {"text": rec, "lang": art["lang"], "url": art["url"]}
        if art and art["lang"] != "en" and (ea := await _article(http, qid, ("en",))) and (rec := reception(ea["sections"])):
            return art, {"text": rec, "lang": "en", "url": ea["url"]}
        return art, None

    async def from_discogs():
        if "discogs" not in rels:
            return {}, None
        dg = await discogs(http, rels["discogs"], token) or {}
        return dg, (await discogs(http, f"release/{dg['main_release']}", token) if dg.get("main_release") else None)

    (art, review), lfm, (dg, rel), awards, (credits, track_credits) = await asyncio.gather(
        article(), lastfm(http, lastfm_key, "album.getinfo", "album", langs[0], artist=artist_name, album=full.get("title", title)),
        from_discogs(), wikidata_facts(http, qid, langs[0], {"P166": "awards"}, 4) if qid else _nothing(),
        release_credits(http, full))
    out = {"name": full.get("title", title), "links": [{"name": "Wikipedia", "url": art["url"]}] if art else []}
    texts = [_text("Wikipedia", art["intro"], art["lang"], art["url"])] if art else []
    if lfm:
        texts.append(_text("Last.fm", lfm["text"], lfm["lang"], lfm["url"]))
    if review:
        out["review"] = review
    facts = [["released", full["first-release-date"][:4]]] if full.get("first-release-date") else []
    genres = _genres(full)
    if "discogs" in rels:
        url = f"https://www.discogs.com/{rels['discogs']}"
        out["links"].append({"name": "Discogs", "url": url})
        genres = (dg.get("styles") or dg.get("genres") or genres)[:4]
        if rel:
            if rel.get("labels"):
                facts.append(["label", rel["labels"][0].get("name", "")])
            rating = (rel.get("community") or {}).get("rating") or {}
            if rating.get("count", 0) >= 10:
                facts.append(["discogs", f"{rating['average']:.1f}".rstrip("0").rstrip("."), rating["count"]])
            if rel.get("notes"):
                texts.append(_text("Discogs", discogs_text(rel["notes"]), "en", url, 800))
    if genres:
        facts.append(["genre", ", ".join(genres)])
    out["facts"] = facts + (awards or [])
    out["credits"], out["track_credits"] = credits, track_credits
    out["links"] += [l for l in out_links(full) if l["name"] not in {x["name"] for x in out["links"]}]
    out["links"].append({"name": "MusicBrainz", "url": f"https://musicbrainz.org/release-group/{full['id']}"})
    out["texts"] = [t for t in texts if t]
    return out


async def track(http, artist_name: str, title: str, langs: tuple[str, ...], lastfm_key: str = "") -> dict | None:
    """The song's own article and write-up. (Its credits come with the album: album()["track_credits"].)"""
    a, lf = await asyncio.gather(wiki_song(http, artist_name, plain(title), langs),
                                 lastfm(http, lastfm_key, "track.getinfo", "track", langs[0], artist=artist_name, track=plain(title)))
    texts = [_text("Wikipedia", a["intro"], a["lang"], a["url"], 900)] if a else []
    if lf:
        texts.append(_text("Last.fm", lf["text"], lf["lang"], lf["url"], 900))
    texts = [t for t in texts if t]
    if not texts:
        return None
    out = {"name": plain(title), "texts": texts, "facts": [], "links": [{"name": "Wikipedia", "url": a["url"]}] if a else [],
           "image": (a or {}).get("image") or ""}
    if a and (rec := reception(a["sections"])):
        out["review"] = {"text": rec, "lang": a["lang"], "url": a["url"]}
    return out


# ---- the words, with their times
def lrc_lines(text: str) -> list[list]:
    """"[01:02.50] words" lines as [[milliseconds, "words"], ...]."""
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"\s*\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]\s?(.*)$", line)
        if m:
            frac = (m.group(3) or "0").ljust(3, "0")[:3]
            out.append([int(m.group(1)) * 60000 + int(m.group(2)) * 1000 + int(frac), m.group(4).strip()[:300]])
    return out[:600]


def _scalar(v: str) -> str:
    """A YAML scalar as LRCLIB writes them: plain, 'single-quoted' or "double-quoted"."""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    return v


def word_lines(text: str) -> list[dict]:
    """LRCLIB's "lyricsfile" (lines with the time of each word) as
    [{"ms": start, "text": line, "words": [[ms, "word "], ...]}, ...]. Its format is a small
    part of YAML, read here line by line: no YAML library is needed for it."""
    out, cur, word, inside = [], None, None, False
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        if not raw.startswith((" ", "-")):          # a top-level key: only "lines:" is ours
            inside = raw.strip() == "lines:"
            continue
        if not inside:
            continue
        m = re.match(r"^(\s*)(- )?(\w+):\s?(.*)$", raw)
        if not m:
            continue
        indent, dash, key, val = len(m.group(1)), bool(m.group(2)), m.group(3), m.group(4)
        if indent == 0 and dash:                     # "- text: a line"
            cur, word = {"ms": None, "text": "", "words": []}, None
            out.append(cur)
        elif indent >= 2 and dash and cur is not None:   # "  - text: 'a word '"
            word = [None, ""]
            cur["words"].append(word)
        target_word = word is not None and (dash or indent >= 4) and indent >= 2
        if key == "text":
            if target_word:
                word[1] = _scalar(val)[:80]
            elif cur is not None:
                cur["text"] = _scalar(val)[:300]
        elif key == "start_ms" and val.strip().lstrip("-").isdigit():
            if target_word:
                word[0] = int(val)
            elif cur is not None:
                cur["ms"] = int(val)
        elif key == "words":
            word = None
    lines = [l for l in out if isinstance(l["ms"], int)]
    for l in lines:
        l["words"] = [w for w in l["words"] if isinstance(w[0], int) and w[1]]
    return lines[:600]


async def lyrics(http, artist_name: str, title: str, album_title: str = "", duration_ms: int = 0) -> dict | None:
    """The song's words from LRCLIB (an open, community-kept database): {"synced": [[ms, line], ...],
    "plain": "...", "instrumental": bool}. None: it has none."""
    q = {"artist_name": artist_name, "track_name": plain(title)}
    d = await _json(http, "https://lrclib.net/api/get", {**q, **({"album_name": plain(album_title)} if album_title else {}),
                                                         **({"duration": round(duration_ms / 1000)} if duration_ms else {})}, missing={})
    if not (d or {}).get("id"):   # not under exactly that album and length: the nearest of the same song
        hits = await _json(http, "https://lrclib.net/api/search", q, missing=[]) or []
        hits = [h for h in hits if isinstance(h, dict) and _fold(h.get("trackName")) == _fold(plain(title))]
        if duration_ms:
            hits = [h for h in hits if abs((h.get("duration") or 0) * 1000 - duration_ms) < 4000]
        hits.sort(key=lambda h: not h.get("syncedLyrics"))
        d = hits[0] if hits else None
    if not d:
        return None
    synced, words = lrc_lines(d.get("syncedLyrics") or ""), None
    timed = word_lines(d.get("lyricsfile") or "")
    if timed and any(len(l["words"]) > 1 for l in timed):
        # the file with a time for every word: its lines and their times go together
        synced, words = [[l["ms"], l["text"]] for l in timed], [l["words"] for l in timed]
    plain_text = (d.get("plainLyrics") or "")[:8000]
    if not synced and not plain_text and not d.get("instrumental"):
        return None
    return {"synced": synced, "words": words, "plain": plain_text, "instrumental": bool(d.get("instrumental"))}


if __name__ == "__main__":  # python3 lookup.py "Artist" "Album" "Song" [lang] [spotify artist id]
    import json
    import sys

    from aiohttp import ClientSession, ClientTimeout

    async def main():
        langs = tuple(dict.fromkeys((*sys.argv[4:5], "en")))
        async with ClientSession(timeout=ClientTimeout(total=20)) as http:
            mbid = await find_artist(http, sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[5] if len(sys.argv) > 5 else "")
            a = await artist(http, mbid, langs) if mbid else None
            al = await album(http, mbid, sys.argv[1], sys.argv[2], langs) if a else None
            ly = await lyrics(http, sys.argv[1], sys.argv[3], sys.argv[2])
            print(json.dumps({"artist": a, "album": al, "track": await track(http, sys.argv[1], sys.argv[3], langs),
                              "lyrics": ly and {"lines": len(ly["synced"]), "first": ly["synced"][:2]}}, ensure_ascii=False, indent=1))
    asyncio.run(main())
