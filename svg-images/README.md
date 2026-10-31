# Milelik SVG images

All of Milelik's project-owned SVG artwork lives in this folder.
Keep every new SVG image here and give each file a descriptive
`milelik-*.svg` name. Use flat colours only: no gradients.

| File | Used for |
| --- | --- |
| `milelik-companion.svg` | The black-and-white monitor-cat companion artwork (full scene: sitting on a wall shelf with his seat behind the shelf front and his booted legs angled over it). The front end presents it as a flat desktop-style icon tile: a square bust crop on a solid background with no gradients or background effects. It is inlined at serve time so the existing CSS can animate its eyes and mouth for the idle, thinking, talking, glitch, alert and sleep states. |
| `milelik-icon.svg` | The browser/favicon icon for the main front end and browser setup. The servers also return it for the legacy `/favicon.svg` and `/favicon.ico` requests. |

Do not add PNG, JPG, GIF, WebP, BMP or ICO sprite images. If another sprite is
needed, add its SVG to this folder, document it in the table above, and serve
or inline it from this canonical copy instead of duplicating the markup in
HTML or Python.
