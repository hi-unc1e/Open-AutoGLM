"""Fast Android driver: uiautomator2's resident service when available, plain adb otherwise.

Measured on a OnePlus PGP110 (ColorOS) with a live camera preview on screen:
``uiautomator dump`` 2.2 s (it waits for an idle UI that a preview never reaches) vs
uiautomator2 ``dump_hierarchy`` 0.18 s; ``screencap`` 1.0 s vs uiautomator2 screenshot 0.16 s.
"""

from __future__ import annotations

import base64
import subprocess
import time
from io import BytesIO
from typing import Callable

from PIL import Image

from phone_agent.fast.fingerprint import fingerprint
from phone_agent.fast.ui import Element, Hierarchy, Selector, parse_hierarchy


class FastDriver:
    """
    Element-based device access.

    Args:
        device_id: adb serial; None uses the only connected device.
        use_uiautomator2: try the uiautomator2 service first (pip install uiautomator2).
        poll_interval: seconds between hierarchy reads while waiting.
    """

    def __init__(self, device_id: str | None = None, use_uiautomator2: bool = True, poll_interval: float = 0.15):
        self.device_id = device_id
        self.poll_interval = poll_interval
        self._u2 = None
        self.backend = "adb"
        if use_uiautomator2:
            try:
                import uiautomator2

                self._u2 = uiautomator2.connect(device_id)
                self._u2.info  # fail fast if the service cannot start
                self.backend = "uiautomator2"
            except Exception as error:  # missing package, blocked install, offline device
                print(f"uiautomator2 unavailable ({error}); falling back to adb")
                self._u2 = None

    # --- raw device access -----------------------------------------------------------------
    def adb(self, *args: str, timeout: float = 15) -> subprocess.CompletedProcess:
        prefix = ["adb", "-s", self.device_id] if self.device_id else ["adb"]
        return subprocess.run(prefix + list(args), capture_output=True, timeout=timeout)

    def hierarchy(self, retries: int = 3) -> Hierarchy:
        """Current UI tree. Retries the empty dumps that busy screens sometimes return."""
        last: Exception | None = None
        for _ in range(retries):
            try:
                if self._u2 is not None:
                    xml = self._u2.dump_hierarchy()
                else:
                    xml = self.adb("exec-out", "uiautomator", "dump", "/dev/tty").stdout.decode("utf-8", "replace")
                    xml = xml[: xml.rfind(">") + 1]
                hierarchy = parse_hierarchy(xml)
                if hierarchy.elements:
                    return hierarchy
                last = ValueError("UI hierarchy has no nodes")
            except Exception as error:
                last = error
            time.sleep(self.poll_interval)
        raise RuntimeError(f"Could not read the UI hierarchy: {last}")

    def screenshot(self) -> Image.Image:
        if self._u2 is not None:
            return self._u2.screenshot()
        return Image.open(BytesIO(self.adb("exec-out", "screencap", "-p").stdout))

    def screenshot_base64(self, max_side: int | None = None, fmt: str = "PNG") -> tuple[str, int, int]:
        """Screenshot as base64 plus the device size the coordinates refer to."""
        image = self.screenshot()
        width, height = image.size
        if max_side and max(width, height) > max_side:
            scale = max_side / max(width, height)
            image = image.resize((int(width * scale), int(height * scale)))
        buffer = BytesIO()
        (image.convert("RGB") if fmt == "JPEG" else image).save(buffer, format=fmt)
        return base64.b64encode(buffer.getvalue()).decode("utf-8"), width, height

    def current_package(self) -> str:
        if self._u2 is not None:
            return self._u2.app_current().get("package", "")
        return self.current_activity().split("/")[0]

    def current_activity(self) -> str:
        """``package/.Activity`` of the resumed activity (dumpsys activity, ~0.07 s)."""
        if self._u2 is not None:
            current = self._u2.app_current()
            return f"{current.get('package', '')}/{current.get('activity', '')}"
        out = self.adb("shell", "dumpsys activity activities | grep -m1 ResumedActivity").stdout.decode()
        for token in out.split():
            if "/" in token:
                return token.rstrip("}")
        return ""

    def fingerprint(self, hierarchy: Hierarchy | None = None) -> str:
        return fingerprint(self.current_activity(), hierarchy or self.hierarchy())

    def tap(self, x: int, y: int) -> None:
        if self._u2 is not None:
            self._u2.click(x, y)
        else:
            self.adb("shell", "input", "tap", str(x), str(y))

    # --- element-based helpers -------------------------------------------------------------
    def find(self, selector: Selector, hierarchy: Hierarchy | None = None) -> Element | None:
        return (hierarchy or self.hierarchy()).find(selector)

    def wait_for(self, condition: Selector | Callable[[Hierarchy], bool], timeout: float = 10.0,
                 gone: bool = False) -> Hierarchy:
        """
        Poll the UI until ``condition`` holds (or, with ``gone``, stops holding).

        Replaces fixed sleeps: returns as soon as the screen is ready, raises TimeoutError otherwise.
        """
        check = condition if callable(condition) else (lambda h: h.find(condition) is not None)
        deadline = time.monotonic() + timeout
        while True:
            hierarchy = self.hierarchy()
            if bool(check(hierarchy)) != gone:
                return hierarchy
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{condition!r} {'still present' if gone else 'not found'} after {timeout}s")
            time.sleep(self.poll_interval)

    def tap_element(self, selector: Selector, timeout: float = 10.0, require_enabled: bool = True) -> Element:
        """Wait for the element, then tap its centre as it is laid out now (never a stale coordinate)."""
        def ready(h: Hierarchy) -> bool:
            e = h.find(selector)
            return e is not None and (e.enabled or not require_enabled)

        hierarchy = self.wait_for(ready, timeout)
        element = hierarchy.find(selector)
        assert element is not None
        self.tap(*element.center)
        return element

    def device_profile(self) -> dict:
        """What invalidates cached coordinates: size, density, rotation."""
        if self._u2 is not None:
            info = self._u2.info
            width, height = self._u2.window_size()
            return {"size": f"{width}x{height}", "density": info.get("displaySizeDpX"),
                    "rotation": info.get("displayRotation")}
        size = self.adb("shell", "wm", "size").stdout.decode().strip().split()[-1]
        density = self.adb("shell", "wm", "density").stdout.decode().strip().split()[-1]
        return {"size": size, "density": density}

    def package_version(self, package: str) -> str:
        out = self.adb("shell", "dumpsys", "package", package).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("versionCode="):
                return line.split()[0].split("=", 1)[1]
        return ""
