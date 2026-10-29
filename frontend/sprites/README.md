# Milelik visual sprites

All of Milelik's project-owned sprite and icon artwork lives in this folder.
Keep new visual sprites here, use SVG only, and give each file a descriptive
`milelik-*.svg` name.

| File | Used for |
| --- | --- |
| `milelik-companion.svg` | The black-and-white monitor-cat companion in the main front end and browser setup. It is inlined at serve time so the existing CSS can animate its eyes, mouth and pixel accents for the idle, thinking, talking, glitch, alert and sleep states. |
| `milelik-icon.svg` | The browser/favicon icon for the main front end and browser setup. The servers also return it for the legacy `/favicon.svg` and `/favicon.ico` requests. |

Do not add PNG, JPG, GIF, WebP, BMP or ICO sprite images. If another sprite is
needed, add its SVG to this folder, document it in the table above, and serve
or inline it from this canonical copy instead of duplicating the markup in
HTML or Python.
