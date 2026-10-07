"""PhoneAgent with a click cache: replay verified steps, ask the model only when the screen is new."""

from __future__ import annotations

import time
from dataclasses import dataclass

from phone_agent.actions.handler import ActionHandler, ActionResult
from phone_agent.adb.screenshot import Screenshot
from phone_agent.agent import AgentConfig, PhoneAgent, StepResult
from phone_agent.config.apps import APP_PACKAGES
from phone_agent.fast.cache import ActionCache, CachedStep
from phone_agent.fast.driver import FastDriver
from phone_agent.fast.fingerprint import fingerprint
from phone_agent.fast.guard import DangerPolicy
from phone_agent.fast.ui import Hierarchy, Selector
from phone_agent.model import ModelConfig
from phone_agent.model.client import MessageBuilder

_POINT_ACTIONS = ("Tap", "Double Tap", "Long Press")


class DriverActionHandler(ActionHandler):
    """Taps through the fast driver (no fixed post-tap sleep); every other action is unchanged."""

    def __init__(self, driver: FastDriver, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.driver = driver

    def _handle_tap(self, action: dict, width: int, height: int) -> ActionResult:
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")
        if "message" in action and not self.confirmation_callback(action["message"]):
            return ActionResult(success=False, should_finish=True, message="User cancelled sensitive operation")
        self.driver.tap(*self._convert_relative_to_absolute(element, width, height))
        return ActionResult(True, False)


@dataclass
class CacheStats:
    model_calls: int = 0
    replays: int = 0
    fallbacks: int = 0
    refused: int = 0
    recorded: int = 0
    screenshots: int = 0


class CachedPhoneAgent(PhoneAgent):
    """
    Same loop as PhoneAgent, with a buffer in front of the model.

    Each step reads the UI tree (fast) and fingerprints the screen. If the cache holds an action
    recorded for this task, step and fingerprint, it is replayed without a screenshot or a model
    call: taps re-locate their element by selector and use its current centre, and dangerous
    elements need an exact selector match (DangerPolicy). After a replay the agent waits for the
    screen recorded as its result; if it does not appear, the cached tail is dropped and the model
    takes over from the real screen. Model steps are recorded for next time.
    """

    def __init__(self, model_config: ModelConfig | None = None, agent_config: AgentConfig | None = None,
                 cache_path: str = ".phone_agent_cache/actions.json", driver: FastDriver | None = None,
                 policy: DangerPolicy | None = None, verify_timeout: float = 6.0, settle_timeout: float = 3.0,
                 **kwargs):
        super().__init__(model_config, agent_config, **kwargs)
        self.driver = driver or FastDriver(self.agent_config.device_id)
        self.action_handler = DriverActionHandler(
            self.driver, device_id=self.agent_config.device_id,
            confirmation_callback=kwargs.get("confirmation_callback"),
            takeover_callback=kwargs.get("takeover_callback"))
        self.cache = ActionCache(cache_path, self.driver.device_profile())
        self.policy = policy or DangerPolicy()
        self.verify_timeout = verify_timeout
        self.settle_timeout = settle_timeout
        self.stats = CacheStats()
        self._task = ""
        self._index = 0
        self._version_cache: dict[str, str] = {}
        request = self.model_client.request

        def counted(*args, **kw):
            self.stats.model_calls += 1
            return request(*args, **kw)

        self.model_client.request = counted

    # --- loop ------------------------------------------------------------------------------
    def run(self, task: str) -> str:
        self._task, self._index, self.stats = task, 0, CacheStats()
        self.policy._dangerous_taps = 0
        try:
            return super().run(task)
        finally:
            self.cache.save()

    def step(self, task: str | None = None) -> StepResult:
        if len(self._context) == 0 and task:
            self._task, self._index = task, 0
        return super().step(task)

    def _execute_step(self, user_prompt: str | None = None, is_first: bool = False) -> StepResult:
        hierarchy = self.driver.hierarchy()
        activity = self.driver.current_activity()
        fp = fingerprint(activity, hierarchy)
        package = activity.split("/")[0]
        cached = self.cache.lookup(self._task, self._index, fp, self._version(package))
        if cached is not None:
            replayed = self._replay(cached, hierarchy, is_first, user_prompt)
            if replayed is not None:
                self._index += 1
                self._step_count += 1
                return replayed
        result = super()._execute_step(user_prompt, is_first)
        self._record(result, hierarchy, fp, package)
        self._index += 1
        return result

    def _capture_screen(self):
        data, width, height = self.driver.screenshot_base64()
        self.stats.screenshots += 1
        package = self.driver.current_package()
        app = next((name for name, pkg in APP_PACKAGES.items() if pkg == package), package or "System Home")
        return Screenshot(base64_data=data, width=width, height=height), app

    # --- replay ----------------------------------------------------------------------------
    def _replay(self, step: CachedStep, hierarchy: Hierarchy, is_first: bool, user_prompt: str | None) -> StepResult | None:
        action = dict(step.action)
        width, height = self._screen_size(hierarchy)
        selector = Selector.from_dict(step.selector) if step.selector else None
        element = hierarchy.find(selector) if selector else None
        if action.get("action") in _POINT_ACTIONS:
            if selector is None and action.get("element"):
                # Coordinate-only recording: judge what is under that point now, so a shutter or
                # pay button can never be pressed just because a stale coordinate lands on it.
                rx, ry = action["element"]
                if self.policy.is_dangerous(hierarchy.element_at(int(rx / 1000 * width), int(ry / 1000 * height))):
                    self.stats.refused += 1
                    return self._give_up(step, "coordinate-only step lands on a dangerous element")
            if selector is not None and element is None:
                return self._give_up(step, "recorded element is not on screen")
            allowed, reason = self.policy.may_replay(element, selector, exact_match=element is not None,
                                                     fingerprint_fresh=True)
            if not allowed:
                self.stats.refused += 1
                return self._give_up(step, reason)
            if element is not None:
                if not element.enabled:
                    return self._give_up(step, "recorded element is disabled")
                x, y = element.center
                action["element"] = [round(x * 1000 / width), round(y * 1000 / height)]
        self._append_replay_context(step, is_first, user_prompt)
        result = self.action_handler.execute(action, width, height)
        finished = action.get("_metadata") == "finish" or result.should_finish
        if step.next_fp is not None and not finished and not self._await_screen(step.next_fp):
            # The action ran but led somewhere new: keep where it actually leads, drop the stale tail,
            # and let the model continue (and re-record) from the real screen.
            step.next_fp = self._settled_fingerprint()
            step.misses += 1
            self.cache.record(self._task, step)
            self.cache.invalidate(self._task, step.index + 1)
            self.stats.fallbacks += 1
        else:
            self.cache.mark(self._task, step.index, hit=True)
            self.stats.replays += 1
        if self.agent_config.verbose:
            print(f"⚡ cached step {step.index}: {step.answer}")
        return StepResult(success=result.success, finished=finished, action=action,
                          thinking="(cached replay)", message=result.message or action.get("message"))

    def _give_up(self, step: CachedStep, reason: str) -> None:
        if self.agent_config.verbose:
            print(f"↩︎ cache miss at step {step.index}: {reason}; asking the model")
        self.cache.mark(self._task, step.index, hit=False)
        self.cache.invalidate(self._task, step.index)
        self.stats.fallbacks += 1
        return None

    def _append_replay_context(self, step: CachedStep, is_first: bool, user_prompt: str | None) -> None:
        """Keep the model's history coherent so a later fallback knows what already happened."""
        screen = MessageBuilder.build_screen_info(self.driver.current_package() or "System Home")
        if is_first:
            self._context.append(MessageBuilder.create_system_message(self.agent_config.system_prompt))
            text = f"{user_prompt}\n\n{screen}"
        else:
            text = f"** Screen Info **\n\n{screen}"
        self._context.append(MessageBuilder.create_user_message(text=text))
        self._context.append(MessageBuilder.create_assistant_message(
            f"<think>Replayed a verified step from the action cache.</think><answer>{step.answer}</answer>"))

    def _await_screen(self, expected: str) -> bool:
        deadline = time.monotonic() + self.verify_timeout
        while time.monotonic() < deadline:
            if self.driver.fingerprint() == expected:
                return True
            time.sleep(self.driver.poll_interval)
        return False

    # --- recording -------------------------------------------------------------------------
    def _record(self, result: StepResult, hierarchy: Hierarchy, fp: str, package: str) -> None:
        action = result.action
        if not action or not result.success:
            return
        selector = None
        element = None
        if action.get("action") in _POINT_ACTIONS and action.get("element"):
            width, height = self._screen_size(hierarchy)
            rx, ry = action["element"]
            element = hierarchy.element_at(int(rx / 1000 * width), int(ry / 1000 * height))
            found = hierarchy.selector_for(element) if element else None
            selector = found.to_dict() if found else None
        if not self.policy.may_record(element, Selector.from_dict(selector) if selector else None):
            return
        finished = action.get("_metadata") == "finish"
        next_fp = None if finished else self._settled_fingerprint()
        answer = self._context[-1]["content"] if self._context else ""
        if isinstance(answer, str) and "<answer>" in answer:
            answer = answer.split("<answer>", 1)[1].split("</answer>", 1)[0]
        self.cache.record(self._task, CachedStep(index=self._index, fp=fp, action=action, answer=str(answer),
                                                 selector=selector, next_fp=next_fp, package=package,
                                                 version=self._version(package)))
        self.stats.recorded += 1

    def _settled_fingerprint(self) -> str:
        """Fingerprint once two consecutive reads agree (animations finished) or the timeout passes."""
        deadline = time.monotonic() + self.settle_timeout
        last = self.driver.fingerprint()
        while time.monotonic() < deadline:
            time.sleep(self.driver.poll_interval)
            current = self.driver.fingerprint()
            if current == last:
                return current
            last = current
        return last

    def _screen_size(self, hierarchy: Hierarchy) -> tuple[int, int]:
        right = max((e.bounds[2] for e in hierarchy.elements), default=1080)
        bottom = max((e.bounds[3] for e in hierarchy.elements), default=2400)
        return right, bottom

    def _version(self, package: str) -> str:
        if not package:
            return ""
        if package not in self._version_cache:
            self._version_cache[package] = self.driver.package_version(package)
        return self._version_cache[package]
