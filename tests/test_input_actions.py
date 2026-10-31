"""Computer-use actions (#34): the real tools through the real providers.

Linux runs on the fake X11 display of ``test_coordinate_contract_tools`` (only ``_run_x`` is
stubbed), Windows and macOS on the fake OS APIs of ``test_provider_geometry``, so the argv and the
events each OS really gets are what these tests read. Expected codes are literals.
"""

from __future__ import annotations

import types

from PIL import Image

from remoteos import ocr


def test_ocr_of_another_monitor_grabs_from_all_screens(monkeypatch):
    # without all_screens PIL crops the bbox out of a primary-only grab: a black region
    grabs = []

    def _grab(bbox=None, all_screens=False):
        grabs.append((bbox, all_screens))
        return Image.new("RGB", (8, 8))

    monkeypatch.setattr(ocr, "ImageGrab", types.SimpleNamespace(grab=_grab))
    ocr._screenshot_region(-1920, 0, 0, 1080)
    ocr._screenshot_region()
    assert grabs == [((-1920, 0, 0, 1080), True), (None, True)]
