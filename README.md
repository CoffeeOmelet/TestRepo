# text_to_images

Splits `text.txt` (next to the script) into blocks and renders each block
into its own PNG, using every CPU core.

```bash
pip install -r requirements.txt
python text_to_images.py                      # 3000 characters per image
python text_to_images.py --images 6           # exactly 6 images
python text_to_images.py -x 1080 -y 1920      # custom image size
python text_to_images.py --remove-whitespace  # strip every space/tab/newline
python text_to_images.py --help               # every option
```

All settings are in the `DEFAULTS` dict at the top of `text_to_images.py`.
Each one is also a command-line flag (for example `chunk_size` is
`--chunk-size`). Images go to `output/`.

While it runs, the script shows the total number of images (worked out from
the text length before rendering starts), how many CPU cores are available,
how many workers it uses, and a live progress bar with speed, ETA and how
many workers are busy.

## Splitting modes

| Mode | Flags | Result |
|---|---|---|
| characters | `--split-mode chars --chunk-size 3000` | one image per 3000 characters |
| image count | `--images 6` (= `--split-mode count --chunk-count 6`) | exactly 6 images, the text shared evenly |

Either mode can use `--snap-to-words` (end blocks at a space instead of
mid-word) and `--strip-chunks` (trim whitespace at block edges).

## Text cleanup

| Setting | Default | Meaning |
|---|---|---|
| `remove_newlines` | true | replace newlines with `newline_replacement` (a space) |
| `collapse_whitespace` | false | squeeze runs of spaces/tabs into one space |
| `remove_whitespace` | false | delete all whitespace (overrides the two above) |

## Speed

- **All cores, any number of images.** With many images, each core
  renders whole images. With only a few (fewer than 2 per core, e.g.
  `--images 6` on a 16-core machine), each image is cut into horizontal
  bands that are drawn and compressed on different cores, and several
  font sizes are tried at once. The output is pixel-identical either way.
  Force one with `--strategy per-image` or `--strategy split`.
- **Glyph cache.** Each character is rendered by FreeType once per font
  size and then stamped wherever it appears: about 20x faster than
  drawing text normally, with identical pixels (kerning included).
- **Fast layout.** Word widths are measured once and reused across images,
  line breaks come from a binary search instead of re-measuring text, and
  the font size search starts from an estimate, so it usually needs 2
  tries instead of ~7.
- **Palette PNGs.** Text is drawn as 1-byte shades and saved as an 8-bit
  palette PNG, a third of the data of RGB, with the same colours.
- **Shared memory.** The text is loaded once and shared with the worker
  processes, so blocks are never copied between processes.
- **PNG compression level 2** by default. `--png-compress-level 6` gives
  ~20% smaller files at roughly half the speed.

`--executor thread` is available, but Python threads can't draw in
parallel, so it is several times slower than the default processes.

## Other notes

- If a block doesn't fit even at `min_font_size`, the text is cut off and
  the script lists those images at the end. Make the images bigger, lower
  `--min-font-size`, or put less text in each image.
- The default font (DejaVu Sans) has no Chinese/Japanese/Korean characters.
  For those, point `--font-path` at a font that does, such as Noto Sans CJK.
  For scripts that need text shaping (Arabic, Hindi...), also add
  `--layout-engine raqm`.
