"""Enable ``python -m omnilimb_face`` to launch the standalone preview.

Mirrors the ``omnilimb-face`` console entry point declared in pyproject.toml.
"""

from omnilimb_face.preview import main

if __name__ == "__main__":
    raise SystemExit(main())
