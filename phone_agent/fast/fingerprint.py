"""Screen fingerprints: a cheap identity for "the same screen" used to key cached actions."""

from __future__ import annotations

import hashlib
import re

from phone_agent.fast.ui import Hierarchy

_DIGITS = re.compile(r"\d+")


def fingerprint(activity: str, hierarchy: Hierarchy, include_text: bool = False) -> str:
    """
    Hash the screen's structure, not its pixels.

    Uses the activity plus (class, resource-id, content-desc, clickable) of every labelled or
    clickable node. Coordinates and free text are left out so that live content (camera
    preview, timers, counters, prices) does not change the fingerprint; digits inside
    descriptions are masked for the same reason. Only the foreground app's nodes count: the
    system bar (clock, battery, notification icons) comes and goes on its own. Screenshot hashes
    are unusable on camera screens because the preview never stops changing.
    """
    parts = [activity]
    package = activity.split("/")[0]
    for e in hierarchy.elements:
        if package and e.package and e.package != package:
            continue
        if not (e.clickable or e.resource_id or e.desc):
            continue
        desc = _DIGITS.sub("#", e.desc)
        text = _DIGITS.sub("#", e.text) if include_text else ""
        parts.append(f"{e.cls}|{e.resource_id}|{desc}|{text}|{int(e.clickable)}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]
