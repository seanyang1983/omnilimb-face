#!/usr/bin/env python
"""Dev shim: ``python preview.py`` -> ``omnilimb_face.preview.main()``.

The real preview launcher now lives **inside the package**
(``omnilimb_face/preview.py``) so a plain ``pip install omnilimb-face`` exposes
the ``omnilimb-face`` console command and ``python -m omnilimb_face`` without a
source checkout. This thin wrapper keeps ``python preview.py`` (and start.bat)
working from a source tree.
"""

from omnilimb_face.preview import main

if __name__ == "__main__":
    raise SystemExit(main())
