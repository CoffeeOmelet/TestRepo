#!/usr/bin/env python3
"""
Split a large text file into fixed-size character blocks and render each
block into its own PNG image, in parallel.

Every setting lives in DEFAULTS below. Edit them there, or override any of
them from the command line (run with --help to see every flag).

Example:
    python text_to_images.py
    python text_to_images.py --chunk-size 2000 --width 1080 --height 1920 \
        --keep-newlines --workers 16 --font /path/to/font.ttf
"""

import argparse
import os
import re
import sys
import threading
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)

from PIL import Image, ImageDraw, ImageFont

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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

    # Chunking
    "chunk_size": 3000,             # characters per image
    "remove_newlines": True,        # strip newlines out of the text
    "newline_replacement": " ",     # what a newline becomes when removed
    "collapse_whitespace": False,   # squeeze runs of spaces/tabs into one space
    "strip_chunks": False,          # trim leading/trailing whitespace per image

    # Image
    "width": 1200,                  # image size x
    "height": 1600,                 # image size y
    "background_color": "#FFFFFF",
    "text_color": "#000000",
    "margin_left": 40,
    "margin_right": 40,
    "margin_top": 40,
    "margin_bottom": 40,
    "png_compress_level": 6,        # 0 (fast, big) .. 9 (slow, small)

    # Text layout
    "font_path": None,              # None = auto-detect a system font
    "font_size": 22,                # used when auto_fit_font is False
    "auto_fit_font": True,          # pick the largest size that fits the image
    "min_font_size": 6,
    "max_font_size": 72,
    "line_spacing": 1.2,            # multiple of font size
    "align": "left",                # left | center | right
    "wrap_mode": "word",            # word | char

    # Parallelism
    "workers": os.cpu_count() or 4,
    # "process" uses all CPU cores for real. "thread" is limited by Python's
    # GIL, so it is ~3x slower for this workload, but it's available.
    "executor": "process",          # process | thread
    "max_pending_factor": 4,        # queued blocks per worker (caps memory use)
}

FALLBACK_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
]


# --------------------------------------------------------------------------
# Reading and chunking (streamed, so huge files never sit fully in memory)
# --------------------------------------------------------------------------
_WHITESPACE_RUN = re.compile(r"[ \t]+")


def normalize(text, cfg):
    if cfg["remove_newlines"]:
        text = text.replace("\n", cfg["newline_replacement"])
    if cfg["collapse_whitespace"]:
        text = _WHITESPACE_RUN.sub(" ", text)
    return text


def iter_chunks(cfg, read_size=1 << 20):
    """Yield exact chunk_size blocks of processed text from the input file."""
    size = cfg["chunk_size"]
    buf = ""
    # newline=None turns \r\n and \r into \n, so Windows files behave too.
    with open(cfg["input_file"], "r", encoding=cfg["encoding"],
              errors="replace", newline=None) as f:
        while True:
            raw = f.read(read_size)
            if not raw:
                break
            buf = normalize(buf + raw, cfg)
            # Hold back a trailing space so whitespace collapsing works
            # across read boundaries.
            keep = 1 if cfg["collapse_whitespace"] and buf.endswith(" ") else 0
            while len(buf) - keep >= size:
                yield buf[:size]
                buf = buf[size:]
    if buf:
        yield buf


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
_font_cache = threading.local()


def resolve_font_path(path):
    if path:
        if not os.path.isfile(path):
            sys.exit(f"Font not found: {path}")
        return path
    for candidate in FALLBACK_FONTS:
        if os.path.isfile(candidate):
            return candidate
    return None  # use Pillow's built-in font


def get_font(path, size):
    # FreeType font objects are not shared between threads.
    cache = getattr(_font_cache, "fonts", None)
    if cache is None:
        cache = _font_cache.fonts = {}
    key = (path, size)
    if key not in cache:
        if path:
            cache[key] = ImageFont.truetype(path, size)
        else:
            cache[key] = ImageFont.load_default(size)
    return cache[key]


def break_long(word, font, max_width, widths):
    """Split a run of text into pieces that each fit on one line."""
    pieces, start, current_w = [], 0, 0.0
    for k, ch in enumerate(word):
        w = widths.get(ch)
        if w is None:
            w = widths[ch] = font.getlength(ch)
        if k > start and current_w + w > max_width:
            pieces.append(word[start:k])
            start, current_w = k, 0.0
        current_w += w
    pieces.append(word[start:])
    return pieces


def wrap_paragraph(text, font, max_width, mode, widths):
    if text == "":
        return [""]
    if mode == "char":
        return break_long(text, font, max_width, widths)

    def width(word):  # each distinct word is measured once per font size
        w = widths.get(word)
        if w is None:
            w = widths[word] = font.getlength(word)
        return w

    space = width(" ")
    lines, current, current_w = [], [], 0.0
    for word in text.split(" "):
        w = width(word)
        new_w = w if not current else current_w + space + w
        if new_w <= max_width:
            current.append(word)
            current_w = new_w
            continue
        if current:
            lines.append(" ".join(current))
        if w > max_width:
            parts = break_long(word, font, max_width, widths)
            lines.extend(parts[:-1])
            current = [parts[-1]]
            current_w = width(parts[-1])
        else:
            current, current_w = [word], w
    lines.append(" ".join(current))
    return lines


def wrap_text(text, font, max_width, mode):
    lines, widths = [], {}
    for paragraph in text.split("\n"):
        lines.extend(wrap_paragraph(paragraph, font, max_width, mode, widths))
    return lines


def line_height(size, cfg):
    return max(1, round(size * cfg["line_spacing"]))


def layout(text, size, cfg):
    """Return (font, lines, fits) for a given font size."""
    font = get_font(cfg["_font_path"], size)
    box_w = cfg["width"] - cfg["margin_left"] - cfg["margin_right"]
    box_h = cfg["height"] - cfg["margin_top"] - cfg["margin_bottom"]
    lines = wrap_text(text, font, box_w, cfg["wrap_mode"])
    fits = len(lines) * line_height(size, cfg) <= box_h
    return font, lines, fits


def best_layout(text, cfg):
    if not cfg["auto_fit_font"]:
        size = cfg["font_size"]
        font, lines, fits = layout(text, size, cfg)
        return size, font, lines, fits

    lo, hi = cfg["min_font_size"], cfg["max_font_size"]
    best = None
    while lo <= hi:  # binary search for the largest size that fits
        mid = (lo + hi) // 2
        font, lines, fits = layout(text, mid, cfg)
        if fits:
            best = (mid, font, lines, True)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        size = cfg["min_font_size"]
        font, lines, fits = layout(text, size, cfg)
        best = (size, font, lines, fits)
    return best


def render_chunk(job):
    index, text, cfg = job
    if cfg["strip_chunks"]:
        text = text.strip()

    size, font, lines, fits = best_layout(text, cfg)
    lh = line_height(size, cfg)
    box_w = cfg["width"] - cfg["margin_left"] - cfg["margin_right"]
    box_h = cfg["height"] - cfg["margin_top"] - cfg["margin_bottom"]
    if not fits:
        lines = lines[: max(0, box_h // lh)]

    img = Image.new("RGB", (cfg["width"], cfg["height"]),
                    cfg["background_color"])
    draw = ImageDraw.Draw(img)
    y = cfg["margin_top"]
    for line in lines:
        x = cfg["margin_left"]
        if cfg["align"] != "left":
            extra = box_w - font.getlength(line)
            x += extra if cfg["align"] == "right" else extra / 2
        draw.text((x, y), line, font=font, fill=cfg["text_color"])
        y += lh

    name = cfg["filename_pattern"].format(index=index)
    path = os.path.join(cfg["output_dir"], name)
    img.save(path, "PNG", compress_level=cfg["png_compress_level"])
    return index, path, size, fits


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


def parse_args():
    p = argparse.ArgumentParser(
        description="Render a big text file into PNG images, "
                    "one fixed-size block of characters per image.")
    flag = lambda name: "--" + name.replace("_", "-")

    for key, default in DEFAULTS.items():
        kwargs = {"default": default, "dest": key,
                  "help": f"(default: {default!r})"}
        if isinstance(default, bool):
            kwargs.update(type=str2bool, nargs="?", const=True,
                          metavar="BOOL")
        elif isinstance(default, int):
            kwargs["type"] = int
        elif isinstance(default, float):
            kwargs["type"] = float
        if key == "align":
            kwargs["choices"] = ["left", "center", "right"]
        elif key == "wrap_mode":
            kwargs["choices"] = ["word", "char"]
        elif key == "executor":
            kwargs["choices"] = ["thread", "process"]
        p.add_argument(flag(key), **kwargs)

    # Convenience shortcuts
    p.add_argument("-x", dest="width", type=int, help="alias for --width")
    p.add_argument("-y", dest="height", type=int, help="alias for --height")
    p.add_argument("--keep-newlines", dest="remove_newlines",
                   action="store_false",
                   help="same as --remove-newlines false")
    args = p.parse_args()
    return vars(args)


def validate(cfg):
    errors = []
    if cfg["chunk_size"] < 1:
        errors.append("chunk_size must be >= 1")
    if cfg["width"] <= cfg["margin_left"] + cfg["margin_right"]:
        errors.append("width must be larger than left + right margins")
    if cfg["height"] <= cfg["margin_top"] + cfg["margin_bottom"]:
        errors.append("height must be larger than top + bottom margins")
    if cfg["min_font_size"] > cfg["max_font_size"]:
        errors.append("min_font_size must be <= max_font_size")
    if cfg["workers"] < 1:
        errors.append("workers must be >= 1")
    if not os.path.isfile(cfg["input_file"]):
        errors.append(f"input file not found: {cfg['input_file']}")
    if errors:
        sys.exit("Config error:\n  " + "\n  ".join(errors))


def main():
    cfg = parse_args()
    validate(cfg)
    cfg["_font_path"] = resolve_font_path(cfg["font_path"])
    os.makedirs(cfg["output_dir"], exist_ok=True)

    pool_cls = (ProcessPoolExecutor if cfg["executor"] == "process"
                else ThreadPoolExecutor)
    max_pending = cfg["workers"] * max(1, cfg["max_pending_factor"])

    print(f"Input:   {cfg['input_file']}")
    print(f"Output:  {cfg['output_dir']}")
    print(f"Font:    {cfg['_font_path'] or 'Pillow default'}")
    print(f"Images:  {cfg['width']}x{cfg['height']}, "
          f"{cfg['chunk_size']} chars each")
    print(f"Workers: {cfg['workers']} ({cfg['executor']}s)")

    start = time.time()
    done = overflowed = 0
    with pool_cls(max_workers=cfg["workers"]) as pool:
        pending = set()

        def collect(futures):
            nonlocal done, overflowed
            for fut in futures:
                index, path, size, fits = fut.result()
                done += 1
                if not fits:
                    overflowed += 1
                    print(f"  warning: block {index} did not fit at font "
                          f"size {size}; text was cut off", file=sys.stderr)
                if done % 100 == 0:
                    rate = done / (time.time() - start)
                    print(f"  {done} images ({rate:.1f}/s)")

        for i, chunk in enumerate(iter_chunks(cfg), start=cfg["start_index"]):
            pending.add(pool.submit(render_chunk, (i, chunk, cfg)))
            if len(pending) >= max_pending:
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                collect(finished)
        collect(pending)

    elapsed = time.time() - start
    print(f"Done: {done} images in {elapsed:.2f}s")
    if overflowed:
        print(f"{overflowed} image(s) were cut off. Increase the image size, "
              f"lower --min-font-size, or reduce --chunk-size.")


if __name__ == "__main__":
    main()
