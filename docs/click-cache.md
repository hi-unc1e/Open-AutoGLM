# 快速驱动与点击缓冲层（本 fork 新增）

> 上游项目：[zai-org/Open-AutoGLM](https://github.com/zai-org/Open-AutoGLM)。本页介绍的 `phone_agent/fast/` 是 fork 中新增的代码，上游没有。

## 解决什么问题

原版每一步都是“截图 → 调用模型 → 点坐标 → 固定等待 1 秒”。在真机自动化测试里，这带来三个问题：

1. **慢。** 在一加 PGP110（ColorOS）上实测，相机预览一直在刷新时，`uiautomator dump` 要等界面静止，每次约 2.2 秒；`screencap` 约 1.0 秒。
2. **贵。** 同一个任务每跑一次，就要把所有步骤重新问一遍模型。
3. **脆。** 坐标写死，界面稍有移动就会点错；只能用 sleep 等元素出现。

## 组成

| 模块 | 作用 |
|---|---|
| `fast/driver.py` `FastDriver` | 优先使用 uiautomator2 常驻服务，读界面树约 0.18 秒、截图约 0.16 秒（同一台 PGP110、相机界面）；装不上时自动退回 adb。提供 `wait_for`（按元素等待，替代 sleep）和 `tap_element`（每次都按元素当前的位置点击） |
| `fast/ui.py` | 解析界面树；`Selector` 按 resource-id、content-desc、文本定位元素，多个同名元素用 `index` 区分 |
| `fast/fingerprint.py` | 界面指纹：Activity 加上前台 App 各控件的类型、id、描述、是否可点击。不计坐标和自由文本，数字统一打码，系统状态栏不计入，所以相机预览、时钟、电量的变化都不会改变指纹 |
| `fast/cache.py` `ActionCache` | 按“设备参数（分辨率、密度、旋转）→ 任务 → 第几步 → 指纹”记录动作，以及执行后预期到达的指纹。App 一升级，该任务的录制就整体作废 |
| `fast/guard.py` `DangerPolicy` | 危险动作防护：快门、购买、删除、分享、发送等按钮，回放前必须精确匹配到选择器，并且刚刚重新核对过界面；每轮最多回放 3 次。购买、支付类按钮从不录制 |
| `fast/cached_agent.py` `CachedPhoneAgent` | 接进 Agent 主循环：指纹命中就直接回放，不截图、不调模型；回放后到达的界面不符合预期，就丢掉后面的录制，交还给模型继续，模型的步骤会被重新录下 |

## 用法

```bash
pip install uiautomator2   # 可选；没有它时退回 adb
```

按元素驱动（写测试脚本用）：

```python
from phone_agent.fast import FastDriver, Selector

d = FastDriver("设备序列号")
d.tap_element(Selector(text="设置"))            # 等它出现且可用，再点它当前的中心
d.wait_for(Selector(text="关于手机"), timeout=5)
d.wait_for(Selector(desc="加载中"), gone=True)   # 等某个元素消失
```

带缓存的 Agent（接口与 `PhoneAgent` 相同）：

```python
from phone_agent.agent import AgentConfig
from phone_agent.fast.cached_agent import CachedPhoneAgent
from phone_agent.model import ModelConfig

agent = CachedPhoneAgent(ModelConfig(base_url="...", model_name="autoglm-phone-9b"),
                         AgentConfig(device_id="设备序列号"),
                         cache_path=".phone_agent_cache/actions.json")
agent.run("打开设置，进入关于手机")
print(agent.stats)  # model_calls / replays / fallbacks / refused / recorded / screenshots
```

## 实测（2026-10-07/08，一加 PGP110，ColorOS）

- 读界面树：`uiautomator dump` 2.2 秒 → uiautomator2 0.18 秒；截图 1.0 秒 → 0.16 秒。
- 一个相机 App 的“冷启动 → 按快门 → 等 AI 建议 → 读状态 → 取消引导”按元素驱动连跑 20 轮：零误点，每轮 p50 4.99 秒、最长 5.91 秒（含 App 自身约 3 秒的 AI 耗时），读界面 379 次零失败。
- 录制与回放：同一任务第二次运行，模型调用和截图都应为 0（单元测试已覆盖，真机结果见下方更新）。

## 测试

```bash
pip install pytest
pytest tests/test_fast.py
```

测试用的是模拟设备和脚本化的模型，不连手机、不调用任何真实模型。覆盖范围：选择器与指纹、按设备参数隔离缓存、App 升级后作废、元素换位置后跟随点击、界面变化后退回模型并重新录制、只记了坐标的步骤落在快门上时拒绝回放。

## 已知边界

- 指纹只看结构。同一个界面里如果只是文字变了（例如“开始”变成“暂停”，而控件描述没变），会被当成同一个界面。需要区分时，用 `include_text=True` 计算指纹。
- 回放只校验“执行后到达预期界面”，不检查业务结果是否正确。断言仍要写在测试里。
