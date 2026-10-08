"""naming.py — where a finished file goes: output folder + template + extension."""
import os

from ..core.text import render_template

MAX_PATH_LEN = 240


def relative_name(track, template, ext):
    """'Artist/Album/03 Title.mp3' style relative path (always '/'-separated) for a Track."""
    values = {"title": track.title, "artist": track.artist, "album": track.album, "year": str(track.year or "")[:4],
              "track": str(track.track_no or ""), "disc": str(track.disc_no or ""), "genre": track.genre}
    rel = render_template(template, values)
    head, _, tail = rel.rpartition("/")
    room = max(20, MAX_PATH_LEN - len(head) - len(ext) - 1)
    tail = tail[:room].rstrip(". ")
    return (head + "/" if head else "") + tail + ext


def full_path(outdir, rel):
    return os.path.join(outdir, *rel.split("/"))
