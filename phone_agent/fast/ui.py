"""UI hierarchy parsing and element selectors for fast, element-based automation."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass(frozen=True)
class Element:
    """One node of a uiautomator hierarchy."""

    cls: str
    resource_id: str
    text: str
    desc: str
    package: str
    clickable: bool
    enabled: bool
    selected: bool
    bounds: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2

    @property
    def area(self) -> int:
        l, t, r, b = self.bounds
        return max(0, r - l) * max(0, b - t)

    def contains(self, x: int, y: int) -> bool:
        l, t, r, b = self.bounds
        return l <= x < r and t <= y < b


@dataclass(frozen=True)
class Selector:
    """
    Stable way to find an element again: resource-id, content-desc or text.

    ``contains`` matches substrings; ``index`` picks among several matches (top-to-bottom order).
    """

    resource_id: str | None = None
    desc: str | None = None
    text: str | None = None
    contains: bool = False
    index: int = 0

    def __post_init__(self):
        if not (self.resource_id or self.desc or self.text):
            raise ValueError("Selector needs resource_id, desc or text")

    def matches(self, element: Element) -> bool:
        def same(expected: str | None, actual: str) -> bool:
            if expected is None:
                return True
            return expected in actual if self.contains else expected == actual

        return (
            same(self.resource_id, element.resource_id)
            and same(self.desc, element.desc)
            and same(self.text, element.text)
        )

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, False, 0)}

    @staticmethod
    def from_dict(data: dict) -> "Selector":
        return Selector(**data)

    def describe(self) -> str:
        parts = [f"{k}={v!r}" for k, v in self.to_dict().items()]
        return "Selector(" + ", ".join(parts) + ")"


@dataclass
class Hierarchy:
    """Parsed hierarchy with lookup helpers."""

    elements: list[Element] = field(default_factory=list)
    xml: str = ""

    def find_all(self, selector: Selector) -> list[Element]:
        found = [e for e in self.elements if selector.matches(e) and e.area > 0]
        return sorted(found, key=lambda e: (e.bounds[1], e.bounds[0]))

    def find(self, selector: Selector) -> Element | None:
        found = self.find_all(selector)
        return found[selector.index] if len(found) > selector.index else None

    def element_at(self, x: int, y: int) -> Element | None:
        """Smallest labelled element under a point, preferring clickable ones (what a tap hit)."""
        hits = [e for e in self.elements if e.contains(x, y) and e.area > 0]
        labelled = [e for e in hits if e.resource_id or e.desc or e.text]
        for pool in ([e for e in labelled if e.clickable], labelled):
            if pool:
                return min(pool, key=lambda e: e.area)
        return None

    def selector_for(self, element: Element) -> Selector | None:
        """Most stable unique selector for ``element``: resource-id, then desc, then text."""
        for key in ("resource_id", "desc", "text"):
            value = getattr(element, key)
            if not value:
                continue
            selector = Selector(**{key: value})
            matches = self.find_all(selector)
            if element in matches:
                return Selector(**{key: value, "index": matches.index(element)})
        return None


def parse_hierarchy(xml: str) -> Hierarchy:
    """Parse ``uiautomator dump`` / uiautomator2 ``dump_hierarchy`` XML."""
    xml = xml.strip()
    start = xml.find("<")
    if start < 0:
        raise ValueError("Empty UI hierarchy")
    root = ET.fromstring(xml[start:])
    elements = []
    for node in root.iter("node"):
        match = _BOUNDS.match(node.get("bounds", ""))
        if not match:
            continue
        elements.append(
            Element(
                cls=node.get("class", ""),
                resource_id=node.get("resource-id", ""),
                text=node.get("text", ""),
                desc=node.get("content-desc", ""),
                package=node.get("package", ""),
                clickable=node.get("clickable") == "true",
                enabled=node.get("enabled", "true") == "true",
                selected=node.get("selected") == "true",
                bounds=tuple(int(v) for v in match.groups()),
            )
        )
    return Hierarchy(elements, xml)
