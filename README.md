# text_to_images

Splits `text.txt` (next to the script) into fixed-size character blocks
(3000 by default) and renders each block into its own PNG, in parallel.

```bash
pip install -r requirements.txt
python text_to_images.py                      # uses the defaults
python text_to_images.py -x 1080 -y 1920      # custom image size
python text_to_images.py --remove-newlines false --chunk-size 2000
python text_to_images.py --help               # every option
```

All settings are in the `DEFAULTS` dict at the top of `text_to_images.py`.
Each one is also a command-line flag (for example `chunk_size` is
`--chunk-size`). Images go to `output/`.

Main options:

| Setting | Default | Meaning |
|---|---|---|
| `chunk_size` | 3000 | characters per image |
| `width` / `height` (`-x` / `-y`) | 1200 / 1600 | image size in pixels |
| `remove_newlines` | true | replace newlines with `newline_replacement` (a space) |
| `auto_fit_font` | true | use the largest font size (from `min_font_size` to `max_font_size`) that fits |
| `font_size` | 22 | fixed size used when `auto_fit_font` is false |
| `font_path` | auto | any `.ttf` / `.otf` file |
| `workers` | CPU count | number of parallel workers |
| `executor` | process | `process` uses every core; `thread` is limited by Python's GIL (about 2-3x slower here) |

If a block can't fit even at `min_font_size`, the text is cut off and the
script prints a warning. To fix that, make the image bigger, lower
`min_font_size`, or reduce `chunk_size`.
