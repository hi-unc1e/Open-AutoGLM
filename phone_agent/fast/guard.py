"""Rules that keep cached replays from pressing buttons with side effects by mistake."""

from __future__ import annotations

from dataclasses import dataclass, field

from phone_agent.fast.ui import Element, Selector

DEFAULT_DANGEROUS = (
    # capture / payment / destructive / outward-facing
    "拍照", "快门", "录像", "购买", "支付", "付款", "订阅", "开通", "充值", "删除", "清空", "卸载",
    "分享", "发布", "发送", "确认支付", "Shutter", "Capture", "Buy", "Purchase", "Pay", "Subscribe",
    "Delete", "Remove", "Share", "Publish", "Send", "Post",
)


@dataclass
class DangerPolicy:
    """
    Decide whether a cached action may run without asking the model again.

    Dangerous elements are never replayed from coordinates: they need an exact selector match on a
    screen whose fingerprint was just re-read, and at most ``max_dangerous_per_run`` such taps run
    per session. Elements whose label matches ``never_cache`` (payments by default) are not even
    recorded, so a model or a human must decide every time.
    """

    dangerous: tuple[str, ...] = DEFAULT_DANGEROUS
    never_cache: tuple[str, ...] = ("购买", "支付", "付款", "订阅", "充值", "确认支付", "Buy", "Purchase", "Pay", "Subscribe")
    max_dangerous_per_run: int = 3
    _dangerous_taps: int = field(default=0, repr=False)

    @staticmethod
    def _label(element: Element | None, selector: Selector | None) -> str:
        parts = []
        if element is not None:
            parts += [element.desc, element.text, element.resource_id]
        if selector is not None:
            parts += [selector.desc or "", selector.text or "", selector.resource_id or ""]
        return " ".join(p for p in parts if p)

    def is_dangerous(self, element: Element | None, selector: Selector | None = None) -> bool:
        label = self._label(element, selector).lower()
        return any(word.lower() in label for word in self.dangerous)

    def may_record(self, element: Element | None, selector: Selector | None = None) -> bool:
        label = self._label(element, selector).lower()
        return not any(word.lower() in label for word in self.never_cache)

    def may_replay(self, element: Element | None, selector: Selector | None, exact_match: bool,
                   fingerprint_fresh: bool) -> tuple[bool, str]:
        if not self.is_dangerous(element, selector):
            return True, ""
        if selector is None or not exact_match:
            return False, "dangerous element without an exact selector match"
        if not fingerprint_fresh:
            return False, "dangerous element on a screen that was not re-verified"
        if self._dangerous_taps >= self.max_dangerous_per_run:
            return False, f"more than {self.max_dangerous_per_run} dangerous replays in this run"
        self._dangerous_taps += 1
        return True, ""
