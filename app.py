#!/usr/bin/env python3
"""Canonical Milelik web-application entry point.

Run Milelik as a local web app with:

    python3 app.py

Use ``python3 setup_milelik.py`` for first-time browser setup/installation.
The application implementation lives in milelik_web.py and uses the local
Milelik agent/tools from milelik.py; this small module is the conventional
web-app entry point used by source checkouts and the installed copy.
"""

from milelik_web import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
