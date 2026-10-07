"""Tests for the fast driver helpers and the click cache, with a fake device and a scripted model."""

import json

import pytest

from phone_agent.agent import AgentConfig
from phone_agent.fast import ActionCache, CachedStep, DangerPolicy, Selector, fingerprint, parse_hierarchy
from phone_agent.fast.cached_agent import CachedPhoneAgent
from phone_agent.model.client import ModelResponse


def node(bounds, desc="", text="", rid="", clickable=True, enabled=True, cls="android.widget.Button"):
    return (f'<node class="{cls}" resource-id="{rid}" text="{text}" content-desc="{desc}" package="demo.app" '
            f'clickable="{str(clickable).lower()}" enabled="{str(enabled).lower()}" bounds="{bounds}" />')


def screen(*nodes):
    return ('<?xml version="1.0"?><hierarchy rotation="0">'
            '<node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false">'
            + "".join(nodes) + "</node></hierarchy>")


SCREENS = {
    "home": screen(node("[0,0][540,200]", desc="打开列表"), node("[540,0][1080,200]", desc="更多")),
    "list": screen(node("[0,300][1080,400]", text="设置", rid="demo:id/settings"),
                   node("[0,400][1080,500]", text="关于")),
    "settings": screen(node("[0,300][1080,400]", text="完成"), node("[0,1900][1080,2100]", desc="拍照")),
    "shot": screen(node("[0,300][1080,400]", text="已保存")),
}
TRANSITIONS = {("home", "打开列表"): "list", ("list", "设置"): "settings", ("settings", "拍照"): "shot"}


class FakeDriver:
    poll_interval = 0.0

    def __init__(self, screens=None):
        self.screens = dict(screens or SCREENS)
        self.current = "home"
        self.taps = []

    def hierarchy(self):
        return parse_hierarchy(self.screens[self.current])

    def current_activity(self):
        return "demo.app/.Main"

    def current_package(self):
        return "demo.app"

    def fingerprint(self, hierarchy=None):
        return fingerprint(self.current_activity(), hierarchy or self.hierarchy())

    def tap(self, x, y):
        self.taps.append((x, y))
        element = self.hierarchy().element_at(x, y)
        label = element and (element.desc or element.text)
        self.current = TRANSITIONS.get((self.current, label), self.current)

    def screenshot_base64(self, *args, **kwargs):
        return "", 1080, 2400

    def device_profile(self):
        return {"size": "1080x2400", "density": 420}

    def package_version(self, package):
        return "7"


class ScriptedModel:
    def __init__(self, answers):
        self.answers = list(answers)

    def request(self, context):
        answer = self.answers.pop(0)
        return ModelResponse(thinking="", action=answer, raw_content=answer)


TASK = "打开设置"
ANSWERS = ['do(action="Tap", element=[250,50])', 'do(action="Tap", element=[500,145])', 'finish(message="done")']


def make_agent(tmp_path, driver, answers):
    agent = CachedPhoneAgent(agent_config=AgentConfig(verbose=False), cache_path=str(tmp_path / "cache.json"),
                             driver=driver, verify_timeout=0.05, settle_timeout=0.0)
    agent.model_client = ScriptedModel(answers)
    request = agent.model_client.request

    def counted(*a, **k):
        agent.stats.model_calls += 1
        return request(*a, **k)

    agent.model_client.request = counted
    return agent


# --- ui / fingerprint -------------------------------------------------------------------------
def test_element_at_prefers_the_smallest_clickable_labelled_node():
    h = parse_hierarchy(SCREENS["list"])
    assert h.element_at(10, 350).text == "设置"
    assert h.selector_for(h.element_at(10, 350)) == Selector(resource_id="demo:id/settings")


def test_selector_index_disambiguates_duplicates():
    h = parse_hierarchy(screen(node("[0,0][10,10]", text="确定"), node("[0,20][10,30]", text="确定")))
    second = h.elements[2]
    assert h.selector_for(second) == Selector(text="确定", index=1)
    assert h.find(Selector(text="确定", index=1)) == second


def test_fingerprint_ignores_layout_and_digits_but_not_structure():
    a = parse_hierarchy(screen(node("[0,0][10,10]", desc="电量 80%")))
    b = parse_hierarchy(screen(node("[5,5][20,20]", desc="电量 79%")))
    c = parse_hierarchy(screen(node("[0,0][10,10]", desc="设置")))
    assert fingerprint("demo.app/.A", a) == fingerprint("demo.app/.A", b)
    assert fingerprint("demo.app/.A", a) != fingerprint("demo.app/.A", c)
    assert fingerprint("demo.app/.A", a) != fingerprint("demo.app/.B", a)


def test_fingerprint_ignores_the_system_bar():
    app = node("[0,0][10,10]", desc="设置")
    bar = '<node class="android.view.View" resource-id="com.android.systemui:id/clock" text="" content-desc="通知" package="com.android.systemui" clickable="false" bounds="[0,0][9,9]" />'
    assert fingerprint("demo.app/.Main", parse_hierarchy(screen(app))) == fingerprint("demo.app/.Main", parse_hierarchy(screen(app, bar)))


def test_empty_hierarchy_is_an_error():
    with pytest.raises(ValueError):
        parse_hierarchy("")


# --- cache --------------------------------------------------------------------------------------
def test_cache_is_scoped_by_device_profile_and_invalidated_by_app_updates(tmp_path):
    path = str(tmp_path / "c.json")
    cache = ActionCache(path, {"size": "1080x2400"})
    cache.record("t", CachedStep(index=0, fp="f", action={"_metadata": "do"}, answer="a", version="7"))
    cache.save()
    assert ActionCache(path, {"size": "1080x2400"}).lookup("t", 0, "f", "7") is not None
    assert ActionCache(path, {"size": "720x1600"}).lookup("t", 0, "f", "7") is None
    reloaded = ActionCache(path, {"size": "1080x2400"})
    assert reloaded.lookup("t", 0, "other-screen", "7") is None
    assert reloaded.lookup("t", 0, "f", "8") is None  # app updated
    assert reloaded.lookup("t", 0, "f", "7") is None  # and the stale recording is gone


# --- guard --------------------------------------------------------------------------------------
def test_dangerous_replay_needs_an_exact_selector_and_has_a_budget():
    policy = DangerPolicy(max_dangerous_per_run=1)
    shutter = parse_hierarchy(SCREENS["settings"]).find(Selector(desc="拍照"))
    assert policy.may_replay(shutter, None, exact_match=False, fingerprint_fresh=True)[0] is False
    assert policy.may_replay(shutter, Selector(desc="拍照"), exact_match=True, fingerprint_fresh=True)[0] is True
    assert policy.may_replay(shutter, Selector(desc="拍照"), exact_match=True, fingerprint_fresh=True)[0] is False
    assert policy.may_replay(None, Selector(text="完成"), exact_match=True, fingerprint_fresh=False)[0] is True


def test_payment_buttons_are_never_recorded():
    policy = DangerPolicy()
    assert policy.may_record(None, Selector(text="立即购买")) is False
    assert policy.may_record(None, Selector(text="设置")) is True


# --- agent ------------------------------------------------------------------------------------
def test_second_run_replays_without_model_calls(tmp_path):
    driver = FakeDriver()
    first = make_agent(tmp_path, driver, ANSWERS)
    assert first.run(TASK) == "done"
    assert driver.current == "settings"
    assert (first.stats.model_calls, first.stats.recorded, first.stats.replays) == (3, 3, 0)

    driver.current = "home"
    second = make_agent(tmp_path, driver, [])
    assert second.run(TASK) == "done"
    assert driver.current == "settings"
    assert (second.stats.model_calls, second.stats.replays, second.stats.screenshots) == (0, 3, 0)


def test_replayed_tap_follows_the_element_when_the_layout_moves(tmp_path):
    driver = FakeDriver()
    make_agent(tmp_path, driver, ANSWERS).run(TASK)
    moved = dict(SCREENS)
    moved["list"] = screen(node("[0,900][1080,1000]", text="设置", rid="demo:id/settings"),
                           node("[0,1000][1080,1100]", text="关于"))
    driver = FakeDriver(moved)
    # Same structure → same fingerprint; the recorded coordinate (y≈348) would now hit nothing.
    agent = make_agent(tmp_path, driver, [])
    assert agent.run(TASK) == "done"
    assert driver.taps[1][1] == 950 and driver.current == "settings"


def test_changed_screen_falls_back_to_the_model_and_rerecords(tmp_path):
    driver = FakeDriver()
    make_agent(tmp_path, driver, ANSWERS).run(TASK)
    changed = dict(SCREENS)
    changed["list"] = screen(node("[0,300][1080,400]", text="设置", rid="demo:id/settings"),
                             node("[0,400][1080,500]", text="关于"), node("[0,500][1080,600]", text="新入口"))
    driver = FakeDriver(changed)
    agent = make_agent(tmp_path, driver, ['do(action="Tap", element=[500,145])', 'finish(message="done")'])
    assert agent.run(TASK) == "done"
    # Step 0 still runs from the cache but lands on a changed screen: counted as a fallback.
    assert (agent.stats.replays, agent.stats.fallbacks, agent.stats.model_calls) == (0, 1, 2)
    driver.current = "home"
    again = make_agent(tmp_path, driver, [])
    assert again.run(TASK) == "done" and again.stats.model_calls == 0


def test_shutter_without_a_selector_is_never_replayed(tmp_path):
    driver = FakeDriver()
    driver.current = "settings"
    cache = ActionCache(str(tmp_path / "cache.json"), driver.device_profile())
    fp = driver.fingerprint()
    cache.record("拍一张", CachedStep(index=0, fp=fp, action={"_metadata": "do", "action": "Tap", "element": [500, 833]},
                                      answer='do(action="Tap", element=[500,833])', selector=None, next_fp="x", version="7"))
    cache.save()
    agent = make_agent(tmp_path, driver, ['finish(message="model decided")'])
    assert agent.run("拍一张") == "model decided"
    assert driver.taps == [] and agent.stats.refused == 1 and agent.stats.fallbacks == 1
