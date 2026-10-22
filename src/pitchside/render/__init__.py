"""Render stage: the timed overlay contract built from a run bundle, demo data for the web UI, and a local server.

* ``overlay.py``: ``overlay.json``, the machine-readable, timed overlays the UI replays (``docs/OVERLAY.md``).
* ``provenance.py``: the evidence chain behind each overlay (text -> claims -> numbers -> events).
* ``crests.py``: generated club colours and crest patterns (all clubs are invented).
* ``demo_data.py``: publishes bundles into ``web/data`` with an index (``pitchside demo-data``).
* ``serve.py``: a static file server for ``web/`` (``pitchside serve``).
"""
