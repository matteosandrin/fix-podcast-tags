#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["mutagen"]
# ///
"""Rewrite ID3 tags and artwork on Apple Podcasts cached MP3s using the Podcasts library DB.

Usage: fix_podcast_tags.py [--dry-run] [--art-size N]
"""

import argparse
import datetime as dt
import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from mutagen.id3 import (
    APIC, COMM, ID3, PCST, TALB, TCAT, TCON, TDAT, TDES, TDRC, TGID, TIME, TIT2, TPE1,
    TPE2, TPOS, TRCK, TYER, WFED, ID3NoHeaderError,
)

GROUP = Path.home() / "Library/Group Containers/243LU875E5.groups.com.apple.podcasts"
CACHE_DIR = GROUP / "Library/Cache"
DB_PATH = GROUP / "Documents/MTLibrary.sqlite"
ART_DIR = CACHE_DIR / "Assets/Artwork"
# The Podcasts app caches most artwork at this size.
DEFAULT_ART_SIZE = 600

# Core Data stores dates as seconds since 2001-01-01 UTC.
CORE_DATA_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)

QUERY = """
SELECT e.ZUUID, e.ZTITLE, e.ZAUTHOR, e.ZPUBDATE, e.ZEPISODENUMBER, e.ZSEASONNUMBER,
       e.ZITEMDESCRIPTIONWITHOUTHTML, e.ZGUID, e.ZARTWORKTEMPLATEURL,
       p.ZTITLE, p.ZAUTHOR, p.ZCATEGORY, p.ZFEEDURL, p.ZARTWORKTEMPLATEURL, p.ZIMAGEURL
FROM ZMTEPISODE e JOIN ZMTPODCAST p ON p.Z_PK = e.ZPODCAST
WHERE e.ZUUID = ?
"""


def open_db():
    # Read-only so we never interfere with the Podcasts app.
    return sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)


def artwork_sources(row, size, use_cache):
    # Yield cached files (Path) and URLs (str), best first.
    ep_template, pod_template, pod_image = row[8], row[13], row[14]
    for kind, template in (("Episode", ep_template), ("Show", pod_template)):
        if not template:
            continue
        if use_cache:
            # The app names cached art by the MD5 of the template URL with its placeholders.
            digest = hashlib.md5(template.encode()).hexdigest()
            yield ART_DIR / f"{kind}-{digest}-{size}x{size}-bb.heic"
        yield template.replace("{w}", str(size)).replace("{h}", str(size)).replace("{f}", "jpg")
    if pod_image:
        yield pod_image


def read_cached_art(path):
    if not path.exists():
        return None
    # The cache stores HEIC. iPods need JPEG.
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "art.jpg"
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg", str(path), "--out", str(out)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  warn: sips failed for {path.name}: {result.stderr.strip()}", file=sys.stderr)
            return None
        return out.read_bytes()


def download_art(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except Exception as exc:
        print(f"  warn: artwork fetch failed for {url}: {exc}", file=sys.stderr)
        return None


_art_cache = {}


def fetch_art(sources):
    for source in sources:
        if source not in _art_cache:
            load = read_cached_art if isinstance(source, Path) else download_art
            _art_cache[source] = load(source)
        data = _art_cache[source]
        if data and data[:3] == b"\xff\xd8\xff":
            return data, "image/jpeg"
        if data and data[:8] == b"\x89PNG\r\n\x1a\n":
            return data, "image/png"
    return None, None


def desired_frames(row, art_size, use_cache):
    (_, title, ep_author, pubdate, ep_num, season, desc, guid, _,
     show, show_author, category, feed_url, _, _) = row
    when = None
    if pubdate is not None:
        # Zero seconds: ID3v2.3 TIME has no seconds, so this round-trips cleanly.
        when = (CORE_DATA_EPOCH + dt.timedelta(seconds=pubdate)).strftime("%Y-%m-%dT%H:%M:00")
    text_values = {
        "TIT2": (TIT2, title),
        "TPE1": (TPE1, ep_author or show_author),
        "TPE2": (TPE2, show_author),
        "TALB": (TALB, show),
        # iTunes convention. Sync tools can use this genre to classify the track as a podcast.
        "TCON": (TCON, "Podcast"),
        "TCAT": (TCAT, category),
        "TGID": (TGID, guid),
        "TDES": (TDES, desc),
        "TDRC": (TDRC, when),
        "TRCK": (TRCK, str(ep_num) if ep_num else None),
        "TPOS": (TPOS, str(season) if season else None),
    }
    # If the DB has no value, keep the existing frame.
    frames = {k: cls(encoding=3, text=v) for k, (cls, v) in text_values.items() if v}
    frames["PCST"] = PCST(value=1)
    if desc:
        frames["COMM::eng"] = COMM(encoding=3, lang="eng", desc="", text=desc)
    if feed_url:
        frames["WFED"] = WFED(url=feed_url)

    art, mime = fetch_art(artwork_sources(row, art_size, use_cache))
    if art:
        frames["APIC"] = APIC(encoding=3, mime=mime, type=3, desc="", data=art)
    return frames


def describe(frame):
    if frame is None:
        return "<none>"
    if isinstance(frame, APIC):
        return f"{frame.mime} {len(frame.data)}B sha1:{hashlib.sha1(frame.data).hexdigest()[:8]}"
    text = frame._pprint()
    return text if len(text) <= 60 else text[:57] + "..."


def process(path, db, args):
    row = db.execute(QUERY, (path.stem,)).fetchone()
    if row is None:
        print(f"{path.name}: not found in Podcasts DB, skipped")
        return False

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    changes = []
    for key, new in desired_frames(row, args.art_size or DEFAULT_ART_SIZE, args.art_size is None).items():
        if key == "APIC":
            old_frames = tags.getall("APIC")
            old = old_frames[0] if len(old_frames) == 1 else None
            if old is None or old.data != new.data or old.type != new.type:
                changes.append((key, old_frames[0] if old_frames else None, new))
                tags.delall("APIC")
                tags.add(new)
            continue
        old = tags.get(key)
        if old is None or old._pprint() != new._pprint():
            changes.append((key, old, new))
            tags.delall(key)
            tags.add(new)

    needs_v23 = tags.version[:2] != (2, 3)
    label = f"{path.name} [{row[9]} - {row[1]}]"
    if not changes and not needs_v23:
        print(f"{label}: up to date")
        return False

    print(f"{label}:")
    for key, old, new in changes:
        print(f"  {key}: {describe(old)} -> {describe(new)}")
    if needs_v23:
        print(f"  ID3 version: 2.{tags.version[1]} -> 2.3")
    if not args.dry_run:
        # v2.3 is the most widely supported version among iPod sync tools.
        # mutagen keeps TDRC in v2.3, which is not valid. Write TYER/TDAT/TIME instead.
        date = tags.get("TDRC")
        if date:
            stamp = date.text[0]
            tags.delall("TDRC")
            tags.add(TYER(encoding=3, text=f"{stamp.year:04d}"))
            if stamp.month and stamp.day:
                tags.add(TDAT(encoding=3, text=f"{stamp.day:02d}{stamp.month:02d}"))
            if stamp.hour is not None and stamp.minute is not None:
                tags.add(TIME(encoding=3, text=f"{stamp.hour:02d}{stamp.minute:02d}"))
        tags.save(path, v2_version=3)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    parser.add_argument(
        "--art-size", type=int,
        help=f"download artwork at this edge in px, skip the local cache (default {DEFAULT_ART_SIZE})",
    )
    args = parser.parse_args()

    files = sorted(CACHE_DIR.glob("*.mp3"))
    changed = 0
    with open_db() as db:
        for path in files:
            changed += process(path, db, args)
    verb = "would update" if args.dry_run else "updated"
    print(f"\n{verb} {changed} of {len(files)} files")


if __name__ == "__main__":
    main()
