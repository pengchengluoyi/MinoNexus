"""两套 settings 的分工，以及 env 覆盖。

钉住一条真实踩过的坑：`settings.py` 曾经自己读 `ai_providers.json`，而 UI 写的是
`settings_store` 的 `settings.json` —— 在设置页填了 key，`llm_client` 读不到，
所有 LLM 调用报"未配置 provider"。更隐蔽的是 `llm_client` 还检查
`case_execution_use`，而当时 `settings.py` 不返回这个 key。

    python tests/test_settings.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 隔离数据目录，别碰真配置
_TMP = tempfile.mkdtemp(prefix="mino-settings-test-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP
for _k in ("MINO_AI_API_KEY", "MINO_AI_PROVIDER", "MINO_AI_BASE_URL", "MINO_AI_MODEL"):
    os.environ.pop(_k, None)

from mino_nexus import settings, settings_store  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== settings.py 不得自己存配置：字段形状必须来自 settings_store ==")
a = set(settings.get_ai_provider_credentials("openai"))
b = set(settings_store.get_ai_provider_credentials("openai"))
missing = b - a
check("字段是 settings_store 的超集", not missing, f"缺 {sorted(missing)}")
check("多出的只有 source", (a - b) == {"source"}, str(sorted(a - b)))
for key in ("case_execution_use", "api_type", "plan_compress_ratio", "web_compress_ratio", "name"):
    check(f"含 llm_client 会读的 {key}", key in a)

print("== 未配 key 时：不该谎报可用 ==")
cred = settings.get_ai_provider_credentials("openai")
check("configured=False", cred["configured"] is False, str(cred["configured"]))
check("case_execution_use=False", not cred["case_execution_use"])
check("source=settings_store", cred["source"] == "settings_store", cred["source"])

print("== UI 写入后，settings.py 立刻读到（同一份真源）==")
settings_store.save_ai_provider("volcengine", {
    "api_key": "sk-from-ui", "model": "doubao-test",
    "enabled": True, "case_execution_use": True,
})
cred = settings.get_ai_provider_credentials("volcengine")
check("读到 UI 写的 key", cred["api_key"] == "sk-from-ui", cred["api_key"][:12])
check("configured=True", cred["configured"] is True)
check("case_execution_use 透传", cred["case_execution_use"] is True)
check("base_url 用了预设", cred["base_url"].startswith("http"), cred["base_url"])
check("source=settings_store", cred["source"] == "settings_store")

print("== env 覆盖：CI / 本地不经 UI 也能配 ==")
os.environ["MINO_AI_API_KEY"] = "sk-from-env"
os.environ["MINO_AI_PROVIDER"] = "deepseek"
os.environ["MINO_AI_MODEL"] = "deepseek-chat"
cred = settings.get_ai_provider_credentials()          # 不传 id → 走 env 的 provider
check("provider 取 env 的", cred["id"] == "deepseek", cred["id"])
check("key 来自 env", cred["api_key"] == "sk-from-env")
check("model 来自 env", cred["model"] == "deepseek-chat", cred["model"])
check("source=env", cred["source"] == "env")
# 关键：三道检查都要过，否则 llm_client 仍会拒
check("configured=True", cred["configured"] is True)
check("enabled=True", cred["enabled"] is True)
check("case_execution_use=True（否则 llm_client 拒）", cred["case_execution_use"] is True)
check("default_provider_id 跟 env", settings.default_provider_id() == "deepseek")
check("find_case_execution_provider_id 跟 env",
      settings.find_case_execution_provider_id() == "deepseek")

print("== env 只覆盖它指名的那个 provider，不污染别的 ==")
other = settings.get_ai_provider_credentials("volcengine")
check("volcengine 仍是 UI 的 key", other["api_key"] == "sk-from-ui", other["api_key"][:12])
check("volcengine source=settings_store", other["source"] == "settings_store")

print("== summary() 不泄明文 key ==")
s = settings.summary()
check("source=env", s["source"] == "env", s["source"])
blob = repr(s)
check("不含 sk-from-env", "sk-from-env" not in blob)
check("不含 sk-from-ui", "sk-from-ui" not in blob)
check("标了 via=env", s["providers"].get("deepseek", {}).get("via") == "env", str(s["providers"].get("deepseek")))

print("== llm_client 需要的三个函数都在（漏一个就 500）==")
# 实际漏过 should_use_ai_planning：AttributeError 让 /case-runner/run 直接 500
for fn in ("get_ai_provider_credentials", "find_case_execution_provider_id",
           "should_use_ai_planning"):
    check(f"settings.{fn} 存在", callable(getattr(settings, fn, None)))

print("== should_use_ai_planning：env 要能越过「使用大模型能力」总开关 ==")
os.environ["MINO_AI_API_KEY"] = "sk-from-env"
os.environ["MINO_AI_PROVIDER"] = "deepseek"
gate = settings.should_use_ai_planning("case_execution")
check("env 下闸门放行", gate["enabled"] is True, str(gate.get("reason")))
check("reason 清空", gate["reason"] == "")
check("gate 里不含明文 key", "sk-from-env" not in repr(gate))
os.environ.pop("MINO_AI_API_KEY", None)
gate = settings.should_use_ai_planning("case_execution")
check("没 env 时按 UI 开关（这里没开→拦住）", gate["enabled"] is False)
check("拦住时给了原因", bool(gate["reason"]), gate["reason"])

print("== 未显式包装的设置项透明转发给 settings_store ==")
for fn in ("get_mail_settings", "get_layer_stack", "get_figma_settings", "list_ai_providers"):
    check(f"settings.{fn} 可转发", callable(getattr(settings, fn, None)))
check("邮件设置转发结果同源",
      settings.get_mail_settings() == settings_store.get_mail_settings())
try:
    settings.definitely_not_a_setting
    check("未知属性应报错", False)
except AttributeError as exc:
    check("未知属性报错并指向 §8", "settings_store" in str(exc), str(exc)[:50])

print("== 压缩比：随 OBSERVE 下发给 Scout（协议 §4.4）==")
os.environ.pop("MINO_AI_API_KEY", None)
check("默认 2.0", settings.get_ai_web_compress_ratio("volcengine") == 2.0,
      str(settings.get_ai_web_compress_ratio("volcengine")))

print()
if failures:
    print(f"FAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK — settings 分工与 env 覆盖")
