# fix-podcast-tags

Fix the ID3 tags and artwork of Apple Podcasts downloads so they show up correctly on an iPod.

## Problem

The Podcasts app on macOS stores downloaded episodes as MP3 files named by UUID in:

```
~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Library/Cache/
```

The iPod sync reads its metadata from the ID3 tags inside these files, not from the Podcasts app.
Those tags come straight from the publisher and are often wrong or missing, for example:

- no tags at all
- a guest name in all caps as the title, or the album set to just `Podcast`
- a placeholder year (e.g. `2018`) instead of the real publish date
- no episode number
- missing artwork, or huge (1 MB) or PNG artwork

## Solution

`fix_podcast_tags.py` treats the Podcasts app's own library database
(`Documents/MTLibrary.sqlite`) as the source of truth. For each cached MP3 it:

1. Finds the episode in the database. The file name is the episode's `ZUUID`.
2. Writes the title, artist, album (the show name), album artist, the publish date and time,
   the episode number (as the track) and the season (as the disc).
3. Writes the iTunes podcast frames: podcast flag, category, description, GUID and feed URL.
   The genre is always set to `Podcast`.
4. Downloads the artwork that the Podcasts app shows as a
   600x600 JPEG and embeds it in place of any existing pictures.
5. Saves the tags as ID3v2.3, the version that iPod sync tools support best.

Note:
- The script only writes files that need changes, so you can run it again before every sync.

## Usage

Requires Python 3.9+ and [mutagen](https://mutagen.readthedocs.io/).

```sh
pip install mutagen
```

Quit the Podcasts app first. Then preview the changes, and apply them:

```sh
python3 fix_podcast_tags.py --dry-run   # show what would change, write nothing
python3 fix_podcast_tags.py             # rewrite the tags
```

Options:

| Option         | Description                                    |
| -------------- | ---------------------------------------------- |
| `--dry-run`    | Print the changes for each file, write nothing |
| `--art-size N` | Artwork size in pixels (default `600`)         |

After the script finishes, sync your iPod as usual.

## Note

If Finder still syncs the wrong episode titles after running the script, try
opening Activity Monitor and killing the following processes:
- `AMPLibraryAgent`
- `AMPDevicesAgent`
- `AMPDeviceDiscoveryAgent`

Then try to sync again. This will force Finder to pick up the new episode titles
from the MP3 tags.
