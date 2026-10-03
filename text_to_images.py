#!/usr/bin/env python3
"""
Split a large text file into blocks and render each block into its own PNG,
using every CPU core.

Two ways to split the text:
  chars  -- a fixed number of characters per image (default 3000)
  count  -- a fixed number of images; the text is shared evenly between them

Every setting lives in DEFAULTS below. Edit them there, or override any of
them from the command line (run with --help to see every flag).

Examples:
    python text_to_images.py
    python text_to_images.py -x 1080 -y 1920 --chunk-size 2000
    python text_to_images.py --images 6          # count mode, 6 images
    python text_to_images.py --remove-whitespace
"""

import argparse
import math
import os
import re
import struct
import sys
import threading
import time
import zlib
from array import array
from bisect import bisect_right
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from collections import namedtuple
from itertools import accumulate
from multiprocessing import shared_memory

from PIL import Image, ImageColor, ImageDraw, ImageFont

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def available_cores():
    """Cores this process may actually run on (respects affinity/containers)."""
    count = getattr(os, "process_cpu_count", None)
    if count and count():
        return count()
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


# --------------------------------------------------------------------------
# Configuration -- change anything here.
# --------------------------------------------------------------------------
DEFAULTS = {
    # Input / output
    "input_file": os.path.join(SCRIPT_DIR, "text.txt"),
    "encoding": "utf-8",
    "output_dir": os.path.join(SCRIPT_DIR, "output"),
    "filename_pattern": "image_{index:05d}.png",  # {index} = block number
    "start_index": 1,

    # Splitting
    "split_mode": "chars",          # chars = chunk_size characters per image
                                    # count = exactly chunk_count images
    "chunk_size": 3000,             # characters per image (chars mode)
    "chunk_count": 10,              # number of images (count mode)
    "snap_to_words": False,         # end blocks at a space, not mid-word
    "strip_chunks": False,          # trim whitespace at block edges

    # Text cleanup (done before splitting, so it changes the block contents)
    "remove_newlines": True,        # replace newlines with newline_replacement
    "newline_replacement": " ",
    "collapse_whitespace": False,   # squeeze runs of spaces/tabs into one
    "remove_whitespace": False,     # delete ALL whitespace: spaces, tabs,
                                    # newlines (overrides the three above)

    # Image
    "width": 1200,                  # image size x
    "height": 1600,                 # image size y
    "background_color": "#FFFFFF",
    "text_color": "#000000",
    "margin_left": 40,
    "margin_right": 40,
    "margin_top": 40,
    "margin_bottom": 40,
    "png_compress_level": 2,        # 0 (fast, big) .. 9 (slow, small);
                                    # 6 = ~20% smaller files, ~2x slower

    # Text layout
    "font_path": None,              # None = auto-detect a system font
    "font_size": 22,                # used when auto_fit_font is False
                                    # (still shrinks if the text won't fit)
    "auto_fit_font": True,          # largest size that fills the image
    "min_font_size": 6,             # smallest readable size: warn below it
                                    # (or cut the text here, overflow="cut")
    "max_font_size": 72,
    "font_size_precision": 0.02,    # auto-fit tries sizes 2% apart
    "overflow": "shrink",           # shrink = text is NEVER cut off; it gets
                                    #          as small as it has to (<1px ok)
                                    # cut    = stop at min_font_size, cut text
    "supersample_below": 6,         # sizes under this (px) are drawn bigger
                                    # and scaled down: sharp, and allows
                                    # sub-pixel text sizes
    "line_spacing": 1.2,            # multiple of font size
    "align": "left",                # left | center | right
    "wrap_mode": "word",            # word | char
    "layout_engine": "basic",       # basic (fast) | raqm (complex scripts)
    "glyph_cache": True,            # render each character once and stamp
                                    # copies (~20x faster drawing, same
                                    # pixels); only used with basic layout

    # Parallelism
    "workers": available_cores(),   # defaults to every core you have
    "executor": "process",          # process = true parallelism on all cores
                                    # thread  = limited by Python's GIL
    "strategy": "auto",             # auto | per-image | split (see below)
    "tasks_per_worker": 3,          # split strategy: band tasks per worker
    "max_pending_factor": 4,        # per-image strategy: queued per worker

    # Progress display
    "progress": True,
    "progress_bar_width": 30,
}

# How work is spread over the cores:
#   per-image  One task per image: layout, drawing and PNG encoding. The best
#              choice when there are many more images than cores.
#   split      Each image is cut into horizontal bands that are drawn and
#              compressed on different cores, and several candidate font
#              sizes are tried at once. Used for a handful of big images
#              (e.g. --images 6 on a 16-core machine), which would otherwise
#              leave most cores idle.
#   auto       split when there are fewer than 2 images per worker,
#              per-image otherwise.

FALLBACK_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
]

READ_BLOCK = 1 << 22                # characters read from disk at a time
WIDTH_CACHE_LIMIT = 500_000         # measured words kept per font size
REF_SIZE = 1000                     # widths are measured at this size and
                                    # scaled, so layout is exactly
                                    # proportional at every font size
LONG_WORD = 1000                    # longer words are measured per character
FONT_CACHE_LIMIT = 64               # font sizes kept in the caches
MAX_SCALE = 64                      # most supersampling (sizes down to
                                    # supersample_below / 64 px)
BAND_PIXELS = 1 << 25               # max pixels drawn at once (memory cap)
KERN_TEST_PAIRS = ("AV", "AW", "AY", "LT", "LY", "TA", "Ta", "Te", "To",
                   "VA", "Wa", "Yo", "P.", "F,", "r.", "y.")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ZLIB_HEADER = b"\x78\x9c"

# Per-process state. Workers fill these in init_worker(); in thread mode the
# main process sets them and every thread shares them.
_CFG = None
_TEXT = None                        # whole text as a str (thread mode)
_SHM = None                         # whole text in shared memory (processes)
_SHM_ENCODING = None
_SHM_CHAR_BYTES = 1
_PALETTE = None
_GRID = None                        # candidate font sizes, ascending
_WIDTHS = {}                        # word/char -> width at REF_SIZE
_tls = threading.local()            # per-thread font objects


# --------------------------------------------------------------------------
# Loading, cleaning and splitting the text
# --------------------------------------------------------------------------
_WHITESPACE_RUN = re.compile(r"[ \t]+")


def normalize(text, cfg):
    if cfg["remove_whitespace"]:
        return "".join(text.split())
    if cfg["remove_newlines"]:
        text = text.replace("\n", cfg["newline_replacement"])
    if cfg["collapse_whitespace"]:
        text = _WHITESPACE_RUN.sub(" ", text)
    return text


def load_text(cfg):
    collapse = cfg["collapse_whitespace"] and not cfg["remove_whitespace"]
    pieces = []
    # newline=None turns \r\n and \r into \n, so Windows files behave too.
    with open(cfg["input_file"], "r", encoding=cfg["encoding"],
              errors="replace", newline=None) as f:
        while True:
            raw = f.read(READ_BLOCK)
            if not raw:
                break
            piece = normalize(raw, cfg)
            # A whitespace run can straddle two reads.
            if (collapse and pieces and pieces[-1].endswith(" ")
                    and piece.startswith(" ")):
                piece = piece[1:]
            if piece:
                pieces.append(piece)
    return "".join(pieces)


def compute_chunks(text, cfg):
    """Return [(start, end), ...] character ranges, one per image."""
    n = len(text)
    if n == 0:
        return []
    snap, strip = cfg["snap_to_words"], cfg["strip_chunks"]
    if cfg["split_mode"] == "count":
        count = min(cfg["chunk_count"], n)
        targets = [(n * (k + 1)) // count for k in range(count)]
        if not (snap or strip):
            return list(zip([0] + targets[:-1], targets))
    else:
        size = cfg["chunk_size"]
        if not (snap or strip):
            return [(s, min(s + size, n)) for s in range(0, n, size)]
        targets = None

    chunks = []
    start = k = 0
    while start < n:
        end = min(targets[k] if targets else start + size, n)
        if snap and end < n:
            # Cut after the last space/newline in the second half of the block.
            floor = start + (end - start) // 2
            cut = max(text.rfind(" ", floor, end), text.rfind("\n", floor, end))
            if cut >= 0:
                end = cut + 1
        s, e = start, end
        if strip:
            while s < e and text[s].isspace():
                s += 1
            while e > s and text[e - 1].isspace():
                e -= 1
        if s < e:
            chunks.append((s, e))
        start = end
        k += 1
    return chunks


def share_text(text):
    """Put the text in shared memory with a fixed width per character, so a
    worker can decode any range without the text being copied per task."""
    try:
        data, encoding, width = text.encode("latin-1"), "latin-1", 1
    except UnicodeEncodeError:
        if max(text) < "\U00010000":
            data, encoding, width = text.encode("utf-16-le"), "utf-16-le", 2
        else:
            data, encoding, width = text.encode("utf-32-le"), "utf-32-le", 4
    shm = shared_memory.SharedMemory(create=True, size=max(1, len(data)))
    shm.buf[:len(data)] = data
    return shm, encoding, width


def text_slice(start, end):
    if _TEXT is not None:
        return _TEXT[start:end]
    w = _SHM_CHAR_BYTES
    return str(_SHM.buf[start * w:end * w], _SHM_ENCODING)


def init_worker(cfg, shm_name, encoding, char_bytes):
    global _CFG, _SHM, _SHM_ENCODING, _SHM_CHAR_BYTES
    _CFG = cfg
    _SHM_ENCODING, _SHM_CHAR_BYTES = encoding, char_bytes
    try:
        _SHM = shared_memory.SharedMemory(name=shm_name, track=False)
    except TypeError:  # Python < 3.13
        _SHM = shared_memory.SharedMemory(name=shm_name)


# --------------------------------------------------------------------------
# Fonts and measuring
# --------------------------------------------------------------------------
def resolve_font_path(path):
    if path:
        if not os.path.isfile(path):
            sys.exit(f"Font not found: {path}")
        return path
    for candidate in FALLBACK_FONTS:
        if os.path.isfile(candidate):
            return candidate
    return None  # use Pillow's built-in font


def get_font(size):
    # FreeType font objects must not be shared between threads.
    fonts = getattr(_tls, "fonts", None)
    if fonts is None or len(fonts) > FONT_CACHE_LIMIT:
        fonts = _tls.fonts = {}
    font = fonts.get(size)
    if font is None:
        path = _CFG["_font_path"]
        if path:
            engine = (ImageFont.Layout.RAQM if _CFG["layout_engine"] == "raqm"
                      else ImageFont.Layout.BASIC)
            font = ImageFont.truetype(path, size, layout_engine=engine)
        else:
            font = ImageFont.load_default(size)
        fonts[size] = font
    return font


def width_table():
    """Widths at REF_SIZE, shared by every font size and every block."""
    if len(_WIDTHS) > WIDTH_CACHE_LIMIT:
        _WIDTHS.clear()
    return _WIDTHS


def measure_missing(items, table, font):
    for item in set(items).difference(table):
        table[item] = font.getlength(item)


class GlyphPainter:
    """Draws text by stamping cached per-character bitmaps: FreeType
    renders each distinct character once per font size instead of once per
    occurrence. Characters are placed using the same proportional widths
    (and kerning) that the line wrapping measured, so lines come out
    exactly as wide as the layout expects at every size. (FreeType's own
    spacing rounds each character to whole pixels, which is off by up to
    ~5% at small sizes.)"""

    def __init__(self, size):
        self.font = get_font(size)
        self.ref = get_font(REF_SIZE)
        self.scale = size / REF_SIZE
        self.ascent = self.font.getmetrics()[0]
        self.glyphs = {}
        # Only track kerning if the font has any worth the lookups.
        ref = self.ref
        self.kerns = {} if any(
            abs(ref.getlength(p) - ref.getlength(p[0]) - ref.getlength(p[1]))
            >= REF_SIZE / 100 for p in KERN_TEST_PAIRS) else None

    def load(self, ch):
        mask, (ox, oy) = self.font.getmask2(ch, "L", anchor="ls")
        if not (mask.size[0] and mask.size[1]):
            mask = None
        table = width_table()
        if ch not in table:
            table[ch] = self.ref.getlength(ch)
        glyph = self.glyphs[ch] = (mask, ox, oy, table[ch] * self.scale)
        return glyph

    def kern(self, pair):
        ref = self.ref
        k = (ref.getlength(pair) - ref.getlength(pair[0])
             - ref.getlength(pair[1])) * self.scale
        self.kerns[pair] = k
        return k

    def paint(self, core_draw, x, y, line):
        glyphs, load = self.glyphs, self.load
        stamp = core_draw.draw_bitmap
        base = y + self.ascent
        kerns, prev = self.kerns, ""
        for ch in line:
            mask, ox, oy, advance = glyphs.get(ch) or load(ch)
            if kerns is not None:
                if prev:
                    pair = prev + ch
                    k = kerns.get(pair)
                    x += self.kern(pair) if k is None else k
                prev = ch
            if mask is not None:
                stamp((int(x) + ox, base + oy), mask, 255)
            x += advance


def get_painter(size):
    painters = getattr(_tls, "painters", None)
    if painters is None or len(painters) > FONT_CACHE_LIMIT:
        painters = _tls.painters = {}
    painter = painters.get(size)
    if painter is None:
        painter = painters[size] = GlyphPainter(size)
    return painter


def get_palette():
    """256 shades from background (index 0) to text colour (index 255).
    Text is drawn as 1-byte coverage values and stored as a palette PNG:
    a third of the memory and compression work of RGB, same look."""
    global _PALETTE
    if _PALETTE is None:
        bg = ImageColor.getrgb(_CFG["background_color"])[:3]
        fg = ImageColor.getrgb(_CFG["text_color"])[:3]
        _PALETTE = bytes(round(b + (f - b) * i / 255)
                         for i in range(256) for b, f in zip(bg, fg))
    return _PALETTE


def box_size(cfg):
    return (cfg["width"] - cfg["margin_left"] - cfg["margin_right"],
            cfg["height"] - cfg["margin_top"] - cfg["margin_bottom"])


# A layout at one font size. Sizes below supersample_below are laid out and
# drawn `scale` times bigger (at `render` px, on a canvas `scale` times
# bigger) and then shrunk, so text can be any size, even under a pixel.
# lh, box_w and box_h are in the scaled-up pixels.
Spec = namedtuple("Spec", "size scale render lh box_w box_h")


def make_spec(size):
    cfg = _CFG
    floor = cfg["supersample_below"]
    scale = (1 if size >= floor
             else min(MAX_SCALE, math.ceil(floor / size - 1e-9)))
    render = round(size * scale, 4)
    box_w, box_h = box_size(cfg)
    lh = max(1.0, render * cfg["line_spacing"])   # fractional is fine
    return Spec(size, scale, render, lh, box_w * scale, box_h * scale)


def size_grid():
    """Every font size auto-fit may pick, ascending, font_size_precision
    apart, from the largest allowed size down to the smallest."""
    global _GRID
    if _GRID is None:
        cfg = _CFG
        top = cfg["max_font_size"] if cfg["auto_fit_font"] else cfg["font_size"]
        if cfg["overflow"] == "cut":
            floor = cfg["min_font_size"] if cfg["auto_fit_font"] else top
        else:
            floor = cfg["supersample_below"] / MAX_SCALE
        floor = min(floor, top)
        sizes, size = {round(floor, 4)}, top
        while size > floor:
            sizes.add(round(size, 4))
            size /= 1 + cfg["font_size_precision"]
        _GRID = sorted(sizes)
    return _GRID


# --------------------------------------------------------------------------
# Line wrapping. Lines are (start, end) offsets into the block's text, kept
# in one flat list: [s0, e0, s1, e1, ...]. Widths are summed from cached
# per-word / per-character measurements, and each line break is found with
# a binary search over prefix sums, so the Python loop runs once per line,
# not once per word.
# --------------------------------------------------------------------------
def break_run(text, start, end, font, max_width, table, out, limit):
    """Hard-wrap text[start:end] at character boundaries."""
    run = text[start:end]
    measure_missing(run, table, font)
    prefix = list(accumulate(map(table.__getitem__, run)))
    pos, base, n = 0, 0.0, len(run)
    while pos < n:
        if len(out) >= limit:
            return True
        cut = bisect_right(prefix, base + max_width, pos)
        if cut == pos:          # a single character wider than the line
            cut = pos + 1
        out.append(start + pos)
        out.append(start + cut)
        base = prefix[cut - 1]
        pos = cut
    return False


def word_widths(words, table, font):
    """Pixel width of every word. Very long "words" (text with few or no
    spaces, e.g. after remove_whitespace) are summed from character widths:
    Pillow refuses to measure strings over 1,000,000 characters, and
    caching huge strings would waste memory."""
    huge = {}
    for word in set(words).difference(table):
        if len(word) > LONG_WORD:
            measure_missing(word, table, font)
            huge[word] = sum(map(table.__getitem__, word))
        else:
            table[word] = font.getlength(word)
    if not huge:
        return map(table.__getitem__, words)
    return [huge[w] if w in huge else table[w] for w in words]


def wrap_words(text, start, end, font, max_width, table, out, limit):
    """Word-wrap text[start:end] (one paragraph)."""
    words = text[start:end].split(" ")
    widths = word_widths(words, table, font)
    space = table[" "] if " " in table else table.setdefault(
        " ", font.getlength(" "))
    # prefix[k]: width of words[:k], each followed by a space.
    prefix = [0.0, *accumulate(map(space.__add__, widths))]
    # starts[k]: text offset where words[k] begins.
    starts = list(accumulate(map((1).__add__, map(len, words)),
                             initial=start))
    i, n = 0, len(words)
    while i < n:
        if len(out) >= limit:
            return True
        j = bisect_right(prefix, prefix[i] + space + max_width, i + 1) - 1
        if j <= i:              # a single word wider than the line
            ws = starts[i]
            if break_run(text, ws, ws + len(words[i]), font, max_width,
                         table, out, limit):
                return True
            i += 1
            continue
        out.append(starts[i])
        out.append(starts[j] - 1)
        i = j
    return False


def wrap(text, spec):
    """Wrap a block at one layout. Returns (offsets, overflowed); stops as
    soon as the image is full."""
    font = get_font(REF_SIZE)           # measure at the reference size...
    table = width_table()
    limit = 2 * int(spec.box_h // spec.lh)
    max_width = spec.box_w * REF_SIZE / spec.render   # ...in its units
    breaker = wrap_words if _CFG["wrap_mode"] == "word" else break_run
    out = []
    pos = 0
    while True:
        nl = text.find("\n", pos)
        end = len(text) if nl < 0 else nl
        if pos == end:          # empty line
            if len(out) >= limit:
                return out, True
            out += (pos, pos)
        elif breaker(text, pos, end, font, max_width, table, out, limit):
            return out, True
        if nl < 0:
            return out, False
        pos = nl + 1


def guess_size(text):
    """Estimate (px) of the font size that just fills the box."""
    cfg = _CFG
    sample = text[:4096].replace("\n", " ")
    if not sample.strip():
        return cfg["max_font_size"]
    table = width_table()
    measure_missing(sample, table, get_font(REF_SIZE))
    per_char = sum(map(table.__getitem__, sample)) / len(sample) / REF_SIZE
    if per_char <= 0:
        return cfg["max_font_size"]
    box_w, box_h = box_size(cfg)
    return math.sqrt(box_w * box_h /
                     (len(text) * per_char * cfg["line_spacing"] * 1.08))


def guess_level(text):
    grid = size_grid()
    return max(0, bisect_right(grid, guess_size(text)) - 1)


def choose_layout(text):
    """Largest font size whose wrapped text fits. Returns
    (spec, offsets, fits). Starts at an estimate and gallops outward
    (1, 2, 4... sizes) until the answer is bracketed, then bisects, so it
    usually needs only a few wraps."""
    grid = size_grid()
    tried = {}
    lo, hi, best, failed = 0, len(grid) - 1, None, False
    level, step = guess_level(text), 1
    while lo <= hi:
        level = min(max(level, lo), hi)
        offsets, over = wrap(text, make_spec(grid[level]))
        tried[level] = offsets
        if over:
            hi, failed, direction = level - 1, True, -1
        else:
            best, lo, direction = level, level + 1, 1
        if best is not None and failed:
            level = (lo + hi) // 2
        else:
            level += direction * step
            step *= 2
    # Nothing fit even at the smallest size: only possible with
    # overflow="cut" (or absurd amounts of text); the text is cut off.
    level = 0 if best is None else best
    return make_spec(grid[level]), tried[level], best is not None


# --------------------------------------------------------------------------
# Drawing and PNG output
# --------------------------------------------------------------------------
def draw_lines(img, text, offsets, first_line, spec, x_shift=0, y_shift=0):
    cfg = _CFG
    s = spec.scale
    font = get_font(spec.render)
    lh, box_w = spec.lh, spec.box_w
    align = cfg["align"]
    draw = ImageDraw.Draw(img)
    if (cfg["glyph_cache"] and cfg["layout_engine"] == "basic"
            and isinstance(font, ImageFont.FreeTypeFont)):
        paint, core_draw = get_painter(spec.render).paint, draw.draw
        put = lambda x, y, line: paint(core_draw, x, y, line)
        ref, ratio = get_font(REF_SIZE), spec.render / REF_SIZE
        measure = lambda line: ref.getlength(line) * ratio
    else:
        put = lambda x, y, line: draw.text((x, y), line, fill=255, font=font)
        measure = font.getlength
    x0 = cfg["margin_left"] * s + x_shift
    top = cfg["margin_top"] * s - y_shift
    for k in range(0, len(offsets), 2):
        line = text[offsets[k]:offsets[k + 1]]
        if line and not line.isspace():
            x = x0
            if align != "left":
                extra = box_w - measure(line)
                x += extra if align == "right" else extra / 2
            # Each line's row comes from its index, so a band and the whole
            # image put it on exactly the same row.
            put(x, round(top + (first_line + k // 2) * lh), line)


def band_lines(spec, y0, y1, nlines):
    """Lines that can touch output rows y0..y1. Glyphs can reach outside
    their line box, so the neighbouring lines are included too."""
    s, lh = spec.scale, spec.lh
    top = _CFG["margin_top"] * s
    i0 = math.floor((y0 * s - top - lh - 2 * spec.render) / lh)
    i1 = math.floor((y1 * s - top + spec.render) / lh) + 1
    return max(0, i0), min(nlines, max(0, i1))


def band_bounds(spec, min_bands=1):
    """Split the output rows into bands small enough to draw in memory."""
    cfg = _CFG
    W, H, s = cfg["width"], cfg["height"], spec.scale
    n = max(min_bands, -(-(W + 1) * s * H * s // BAND_PIXELS))
    n = min(n, H)
    return [H * b // n for b in range(n + 1)]


def render_rows(text, offsets, first_line, spec, y0, y1, extra_col=0):
    """Output rows y0..y1 as an L image of text coverage (0..255). With
    supersampling it is drawn spec.scale times bigger and box-filtered down,
    which matches drawing the whole image at once exactly."""
    cfg = _CFG
    s = spec.scale
    img = Image.new("L", ((cfg["width"] + extra_col) * s, (y1 - y0) * s), 0)
    i0, i1 = band_lines(spec, y0, y1, first_line + len(offsets) // 2)
    i0 = max(i0, first_line)
    if i0 < i1:
        a, b = 2 * (i0 - first_line), 2 * (i1 - first_line)
        draw_lines(img, text, offsets[a:b], i0, spec,
                   x_shift=extra_col * s, y_shift=y0 * s)
    if s > 1:
        img = img.reduce(s)
    if extra_col:
        img.paste(0, (0, 0, extra_col, img.height))
    return img


def output_path(index):
    return os.path.join(_CFG["output_dir"],
                        _CFG["filename_pattern"].format(index=index))


def adler32_combine(adler1, adler2, len2):
    """Checksum of A+B from the checksums of A and B (same as zlib's)."""
    base = 65521
    rem = len2 % base
    sum1 = adler1 & 0xFFFF
    sum2 = (rem * sum1) % base
    sum1 = (sum1 + (adler2 & 0xFFFF) + base - 1) % base
    sum2 = (sum2 + ((adler1 >> 16) & 0xFFFF) + ((adler2 >> 16) & 0xFFFF)
            + base - rem) % base
    return sum1 | (sum2 << 16)


def png_chunk(f, kind, *parts):
    f.write(struct.pack(">I", sum(map(len, parts))))
    f.write(kind)
    crc = zlib.crc32(kind)
    for part in parts:
        f.write(part)
        crc = zlib.crc32(part, crc)
    f.write(struct.pack(">I", crc))


def write_banded_png(path, width, height, palette, bands):
    """Assemble a palette PNG from bands that were deflated separately.
    Each band ends on a byte boundary (Z_SYNC_FLUSH), so the pieces join
    into one valid zlib stream."""
    with open(path, "wb") as f:
        f.write(PNG_SIGNATURE)
        png_chunk(f, b"IHDR", struct.pack(">IIBBBBB", width, height,
                                          8, 3, 0, 0, 0))
        png_chunk(f, b"PLTE", palette)
        adler, last = 1, len(bands) - 1
        for i, (data, band_adler, band_len) in enumerate(bands):
            adler = adler32_combine(adler, band_adler, band_len)
            parts = [data]
            if i == 0:
                parts.insert(0, ZLIB_HEADER)
            if i == last:
                parts.append(struct.pack(">I", adler))
            png_chunk(f, b"IDAT", *parts)
        png_chunk(f, b"IEND")


# --------------------------------------------------------------------------
# Worker tasks
# --------------------------------------------------------------------------
def task_image(index, start, end):
    """per-image strategy: one whole image."""
    cfg = _CFG
    text = text_slice(start, end)
    spec, offsets, fits = choose_layout(text)
    bounds = band_bounds(spec)
    if len(bounds) == 2:
        img = render_rows(text, offsets, 0, spec, 0, cfg["height"])
    else:
        img = Image.new("L", (cfg["width"], cfg["height"]), 0)
        for y0, y1 in zip(bounds, bounds[1:]):
            img.paste(render_rows(text, offsets, 0, spec, y0, y1), (0, y0))
    img.putpalette(get_palette())   # L -> P, the values become indices
    img.save(output_path(index), "PNG",
             compress_level=cfg["png_compress_level"])
    return index, spec.size, fits, end - start


def task_fit(start, end, level):
    """split strategy: does the block fit at this font size?"""
    offsets, over = wrap(text_slice(start, end), make_spec(size_grid()[level]))
    keep = not over or level == 0
    return level, not over, array("I", offsets).tobytes() if keep else b""


def task_band(start, size, y0, y1, first_line, offsets_bytes, final):
    """split strategy: draw and deflate rows y0..y1 of one image."""
    cfg = _CFG
    offsets = array("I")
    offsets.frombytes(offsets_bytes)
    if offsets:
        a = offsets[0]
        text = text_slice(start + a, start + offsets[-1])
        offsets = [o - a for o in offsets]
    else:
        text = ""
    # One extra column on the left holds each row's PNG filter byte (0), so
    # tobytes() is already valid PNG scanline data.
    img = render_rows(text, offsets, first_line, make_spec(size), y0, y1,
                      extra_col=1)
    raw = img.tobytes()
    comp = zlib.compressobj(cfg["png_compress_level"], zlib.DEFLATED, -15, 9)
    data = comp.compress(raw) + comp.flush(
        zlib.Z_FINISH if final else zlib.Z_SYNC_FLUSH)
    return data, zlib.adler32(raw), len(raw)


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------
class Progress:
    def __init__(self, total, workers, cfg):
        self.total, self.workers = total, workers
        self.enabled = cfg["progress"]
        self.width = cfg["progress_bar_width"]
        self.stream = sys.stderr
        self.tty = self.stream.isatty()
        try:
            "█░".encode(self.stream.encoding or "ascii")
            self.full, self.empty = "█", "░"
        except (UnicodeEncodeError, LookupError):
            self.full, self.empty = "#", "-"
        self.start = time.perf_counter()
        self.last_draw = 0.0
        self.last_step = -1
        self.last_len = 0
        self.last_done = None

    @staticmethod
    def clock(seconds):
        if seconds is None:
            return "--:--"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def update(self, done, busy, force=False):
        if not self.enabled:
            return
        now = time.perf_counter()
        frac = min(1.0, done / self.total)
        if self.tty:
            if not force and now - self.last_draw < 0.1:
                return
        else:  # log files: one line per 10%
            step = int(frac * 10)
            if not force and step == self.last_step:
                return
            self.last_step = step
        self.last_draw = now
        self.last_done = done
        elapsed = now - self.start
        rate = done / elapsed if elapsed > 0 else 0.0
        eta = (self.total - done) / rate if rate > 0 else None
        filled = int(self.width * frac)
        line = (f"{self.full * filled}{self.empty * (self.width - filled)} "
                f"{frac * 100:5.1f}%  {int(done):,}/{self.total:,} images  "
                f"{rate:,.1f} img/s  {self.clock(elapsed)} elapsed, "
                f"ETA {self.clock(eta)}  "
                f"[{min(busy, self.workers)}/{self.workers} workers busy]")
        if self.tty:
            pad = " " * max(0, self.last_len - len(line))
            self.stream.write("\r" + line + pad)
            self.last_len = len(line)
        else:
            self.stream.write(line + "\n")
        self.stream.flush()

    def close(self, done):
        if self.last_done != done:
            self.update(done, 0, force=True)
        if self.enabled and self.tty:
            self.stream.write("\n")


def run_per_image(pool, chunks, cfg, progress, workers):
    jobs = enumerate(chunks, start=cfg["start_index"])
    window = workers * max(1, cfg["max_pending_factor"])
    pending, results = set(), []

    def fill():
        for index, (start, end) in jobs:
            pending.add(pool.submit(task_image, index, start, end))
            if len(pending) >= window:
                break

    fill()
    while pending:
        finished, _ = wait(pending, return_when=FIRST_COMPLETED)
        pending.difference_update(finished)
        results.extend(fut.result() for fut in finished)
        fill()
        progress.update(len(results), len(pending))
    return results


class Job:
    """One image in the split strategy. lo..hi is the range of font size
    levels (indices into size_grid()) still in question."""
    __slots__ = ("index", "start", "end", "lo", "hi", "best", "failed",
                 "probe", "step", "inflight", "results", "spec", "fits",
                 "bands", "bands_left", "nbands")

    def __init__(self, index, start, end, top, probe):
        self.index, self.start, self.end = index, start, end
        self.lo, self.hi, self.probe = 0, top, probe
        self.best = self.spec = self.fits = self.bands = None
        self.failed = False
        self.step, self.inflight, self.bands_left, self.nbands = 1, 0, 0, 1
        self.results = {}


def run_split(pool, chunks, cfg, text, progress, workers):
    """Few images: try several font sizes at once, then draw and compress
    each image as horizontal bands on all cores."""
    W, H = cfg["width"], cfg["height"]
    palette = get_palette()
    grid = size_grid()
    min_bands = max(1, min(-(-workers * cfg["tasks_per_worker"]
                             // len(chunks)), H // 8))
    max_probes = 8

    jobs = [Job(index, start, end, len(grid) - 1,
                guess_level(text[start:end]))
            for index, (start, end) in enumerate(chunks,
                                                 start=cfg["start_index"])]
    futures = {}
    searching = len(jobs)
    state = {"done": 0.0}
    results = []

    def submit_fits(job):
        k = max(1, min(max_probes, workers // max(1, searching)))
        lo, hi = job.lo, job.hi
        if job.probe is not None:           # first: around the estimate
            c, job.probe = job.probe, None
            cands = [c + d for d in range(-((k - 1) // 2), k - (k - 1) // 2)]
        elif job.best is not None and job.failed:   # bracketed: bisect
            span = hi - lo + 1
            cands = (range(lo, hi + 1) if span <= k else
                     [lo + span * (i + 1) // (k + 1) for i in range(k)])
        else:                               # gallop up or down
            up = job.best is not None
            base = job.best if up else hi + 1
            cands = [base + (1 if up else -1) * job.step * (i + 1)
                     for i in range(k)]
            job.step *= k + 1
        levels = sorted({min(max(c, lo), hi) for c in cands})
        for level in levels:
            job.inflight += 1
            fut = pool.submit(task_fit, job.start, job.end, level)
            futures[fut] = ("fit", job, None)

    def resolve(job):
        nonlocal searching
        searching -= 1
        level = job.best if job.best is not None else 0
        spec = job.spec = make_spec(grid[level])
        job.fits = job.best is not None
        offsets = array("I")
        offsets.frombytes(job.results[level])
        job.results = None
        results.append((job.index, spec.size, job.fits, job.end - job.start))
        nlines = len(offsets) // 2
        bounds = band_bounds(spec, min_bands)
        job.nbands = job.bands_left = len(bounds) - 1
        job.bands = [None] * job.nbands
        for b in range(job.nbands):
            y0, y1 = bounds[b], bounds[b + 1]
            i0, i1 = band_lines(spec, y0, y1, nlines)
            sub = offsets[2 * i0:2 * i1].tobytes() if i0 < i1 else b""
            fut = pool.submit(task_band, job.start, spec.size, y0, y1, i0,
                              sub, b == job.nbands - 1)
            futures[fut] = ("band", job, b)

    def on_fit(job, level, fits, payload):
        job.inflight -= 1
        if job.spec is not None:            # already decided
            return
        if payload:
            job.results[level] = payload
        if fits:
            job.best = level if job.best is None else max(job.best, level)
            job.lo = max(job.lo, level + 1)
        else:
            job.hi = min(job.hi, level - 1)
            job.failed = True
        if job.lo > job.hi:
            resolve(job)
        elif job.inflight == 0:
            submit_fits(job)

    def on_band(job, b, result):
        job.bands[b] = result
        job.bands_left -= 1
        state["done"] += 1 / job.nbands
        if job.bands_left == 0:
            write_banded_png(output_path(job.index), W, H, palette, job.bands)
            job.bands = None

    for job in jobs:
        submit_fits(job)
    while futures:
        finished, _ = wait(futures, return_when=FIRST_COMPLETED)
        for fut in finished:
            kind, job, band = futures.pop(fut)
            if kind == "fit":
                on_fit(job, *fut.result())
            else:
                on_band(job, band, fut.result())
        progress.update(state["done"], len(futures))
    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def str2bool(value):
    if isinstance(value, bool):
        return value
    if value.lower() in ("1", "true", "yes", "y", "on"):
        return True
    if value.lower() in ("0", "false", "no", "n", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected a boolean, got {value!r}")


CHOICES = {
    "split_mode": ["chars", "count"],
    "align": ["left", "center", "right"],
    "wrap_mode": ["word", "char"],
    "layout_engine": ["basic", "raqm"],
    "executor": ["process", "thread"],
    "strategy": ["auto", "per-image", "split"],
    "overflow": ["shrink", "cut"],
}
FLOAT_KEYS = {"font_size", "min_font_size", "max_font_size",
              "supersample_below"}


class ImagesAction(argparse.Action):
    """--images N  ==  --split-mode count --chunk-count N"""
    def __call__(self, parser, namespace, values, option_string=None):
        namespace.chunk_count = values
        namespace.split_mode = "count"


def parse_args():
    p = argparse.ArgumentParser(
        description="Render a big text file into PNG images, one block of "
                    "text per image, on every CPU core.")
    for key, default in DEFAULTS.items():
        kwargs = {"default": default, "dest": key,
                  "help": f"(default: {default!r})"}
        if isinstance(default, bool):
            kwargs.update(type=str2bool, nargs="?", const=True,
                          metavar="BOOL")
        elif key in FLOAT_KEYS:
            kwargs["type"] = float
        elif isinstance(default, int):
            kwargs["type"] = int
        elif isinstance(default, float):
            kwargs["type"] = float
        if key in CHOICES:
            kwargs["choices"] = CHOICES[key]
        p.add_argument("--" + key.replace("_", "-"), **kwargs)

    # Shortcuts
    p.add_argument("-x", dest="width", type=int, help="alias for --width")
    p.add_argument("-y", dest="height", type=int, help="alias for --height")
    p.add_argument("--images", dest="chunk_count", type=int,
                   action=ImagesAction, metavar="N",
                   help="split into exactly N images (count mode)")
    p.add_argument("--keep-newlines", dest="remove_newlines",
                   action="store_false",
                   help="same as --remove-newlines false")
    return vars(p.parse_args())


def validate(cfg):
    errors = []
    if cfg["chunk_size"] < 1:
        errors.append("chunk_size must be >= 1")
    if cfg["chunk_count"] < 1:
        errors.append("chunk_count must be >= 1")
    if cfg["width"] <= cfg["margin_left"] + cfg["margin_right"]:
        errors.append("width must be larger than left + right margins")
    if cfg["height"] <= cfg["margin_top"] + cfg["margin_bottom"]:
        errors.append("height must be larger than top + bottom margins")
    if not 0 < cfg["min_font_size"] <= cfg["max_font_size"]:
        errors.append("need 0 < min_font_size <= max_font_size")
    if cfg["font_size"] <= 0 or cfg["supersample_below"] <= 0:
        errors.append("font_size and supersample_below must be > 0")
    if not 0 < cfg["font_size_precision"] <= 1:
        errors.append("font_size_precision must be in (0, 1]")
    if cfg["line_spacing"] <= 0:
        errors.append("line_spacing must be > 0")
    if not 0 <= cfg["png_compress_level"] <= 9:
        errors.append("png_compress_level must be 0..9")
    if cfg["workers"] < 1 or cfg["tasks_per_worker"] < 1:
        errors.append("workers and tasks_per_worker must be >= 1")
    if not os.path.isfile(cfg["input_file"]):
        errors.append(f"input file not found: {cfg['input_file']}")
    try:
        ImageColor.getrgb(cfg["background_color"])
        ImageColor.getrgb(cfg["text_color"])
    except ValueError as e:
        errors.append(str(e))
    if errors:
        sys.exit("Config error:\n  " + "\n  ".join(errors))


def main():
    global _CFG, _TEXT
    cfg = parse_args()
    validate(cfg)
    cfg["_font_path"] = resolve_font_path(cfg["font_path"])
    _CFG = cfg

    t0 = time.perf_counter()
    text = load_text(cfg)
    chunks = compute_chunks(text, cfg)
    load_time = time.perf_counter() - t0
    if not chunks:
        print("No text to render (the file is empty after cleanup).")
        return

    workers = cfg["workers"]
    strategy = cfg["strategy"]
    if strategy == "auto":
        strategy = ("split" if workers > 1 and len(chunks) < 2 * workers
                    else "per-image")
    if cfg["split_mode"] == "count":
        if len(chunks) < cfg["chunk_count"]:
            print(f"Note: only {len(text):,} characters, so "
                  f"{len(chunks)} images instead of {cfg['chunk_count']}.")
        split = (f"{len(chunks):,} images, ~{len(text) // len(chunks):,} "
                 f"characters each")
    else:
        split = (f"{cfg['chunk_size']:,} characters per image -> "
                 f"{len(chunks):,} images")
    top = cfg["max_font_size"] if cfg["auto_fit_font"] else cfg["font_size"]
    sizing = (f"auto-fit up to {top:g}px" if cfg["auto_fit_font"]
              else f"{top:g}px")
    sizing += (", shrinks as needed, never cuts text"
               if cfg["overflow"] == "shrink"
               else f", cuts text below {cfg['min_font_size']:g}px")
    kind = "processes" if cfg["executor"] == "process" else "threads"

    print(f"Input:    {cfg['input_file']}")
    print(f"Text:     {len(text):,} characters after cleanup "
          f"(loaded and split in {load_time:.2f}s)")
    print(f"Split:    {split}")
    print(f"Images:   {cfg['width']}x{cfg['height']} px -> "
          f"{cfg['output_dir']}")
    print(f"Font:     {cfg['_font_path'] or 'Pillow default'} ({sizing})")
    print(f"CPU:      {available_cores()} cores available, using {workers} "
          f"worker {kind}, strategy: {strategy}")
    if cfg["executor"] == "thread":
        print("          (threads share one Python interpreter lock; "
              "--executor process is faster)")
    # Warn up front if the text is too dense to be readable.
    s0, e0 = max(chunks, key=lambda c: c[1] - c[0])
    estimate = min(top, guess_size(text[s0:e0]))
    if estimate < cfg["min_font_size"]:
        fate = ("it will not be cut off" if cfg["overflow"] == "shrink"
                else "the rest will be cut off")
        print(f"Warning:  {e0 - s0:,} characters won't fit in "
              f"{cfg['width']}x{cfg['height']} at a readable size. Expect "
              f"text around {estimate:.2g}px tall ({fate}).")
    sys.stdout.flush()

    os.makedirs(cfg["output_dir"], exist_ok=True)
    shm = None
    start = time.perf_counter()
    try:
        if cfg["executor"] == "process":
            shm, encoding, char_bytes = share_text(text)
            pool = ProcessPoolExecutor(
                max_workers=workers, initializer=init_worker,
                initargs=(cfg, shm.name, encoding, char_bytes))
        else:
            _TEXT = text
            pool = ThreadPoolExecutor(max_workers=workers)
        progress = Progress(len(chunks), workers, cfg)
        with pool:
            try:
                if strategy == "split":
                    results = run_split(pool, chunks, cfg, text, progress,
                                        workers)
                else:
                    results = run_per_image(pool, chunks, cfg, progress,
                                            workers)
            except KeyboardInterrupt:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
        done = len(results)
        progress.close(done)
    finally:
        if shm is not None:
            shm.close()
            shm.unlink()

    elapsed = time.perf_counter() - start
    print(f"Done: {done:,} images in {elapsed:.2f}s "
          f"({done / elapsed:,.1f} images/s)")
    report(results, len(text), cfg)


def report(results, total_chars, cfg):
    """Explain any images whose text is cut off or too small to read."""
    W, H, readable = cfg["width"], cfg["height"], cfg["min_font_size"]
    sizes = sorted(size for _, size, _, _ in results)
    print(f"Font sizes used: {sizes[0]:.3g}px to {sizes[-1]:.3g}px "
          f"(median {sizes[len(sizes) // 2]:.3g}px)")
    cut = sorted(index for index, _, fits, _ in results if not fits)
    if cut:
        shown = ", ".join(map(str, cut[:10]))
        more = f" and {len(cut) - 10:,} more" if len(cut) > 10 else ""
        print(f"{len(cut):,} image(s) had text cut off: {shown}{more}. "
              f"Use --overflow shrink to never cut text.")
    small = [(size, n) for _, size, _, n in results if size < readable]
    if not small:
        return
    # Text area scales with size squared: an image holding n characters
    # at `size` px holds about n * (size / readable)^2 at the readable size.
    per_image = sum(n * (size / readable) ** 2 for size, n in small) / len(small)
    images = math.ceil(total_chars / max(1.0, per_image))
    grow = readable / min(size for size, _ in small)
    print(f"{len(small):,} image(s) have text smaller than "
          f"{readable:g}px (smallest {min(size for size, _ in small):.2g}px), "
          f"too small to read.")
    if cfg["split_mode"] == "count":
        fewer = f"about {images:,} images (--images {images})"
    else:
        chunk = max(1, int(per_image))
        fewer = (f"about {chunk:,} characters per image "
                 f"(--chunk-size {chunk})")
    print(f"For {readable:g}px text, use {fewer}, or images about "
          f"{grow:.1f}x wider and taller "
          f"(-x {round(W * grow)} -y {round(H * grow)}).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
