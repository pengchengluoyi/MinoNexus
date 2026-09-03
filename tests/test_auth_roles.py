"""账号角色简化：admin / user；Console 仅管理员；注册免验证码。

    python tests/test_auth_roles.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mino-auth-roles-")
os.environ["MINO_NEXUS_DATA_DIR"] = _TMP
os.environ["MINO_BOOTSTRAP_PASSWORD"] = "Mino@local"

from mino_nexus import auth_store as auth  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{' — ' + detail if detail else ''}")
        failures.append(name)


print("== 角色归一 ==")
check("platform_admin → admin", auth.normalize_role("platform_admin") == "admin")
check("operator → user", auth.normalize_role("operator") == "user")
check("admin 保持", auth.normalize_role("admin") == "admin")
check("is_admin_role", auth.is_admin_role("platform_admin") is True)
check("user 非管理员", auth.is_admin_role("user") is False)

print("== 种子管理员 ==")
admin = auth.login(username="admin", password="Mino@local", client="console")
check("admin console 登录", bool(admin.get("token")) and admin.get("role") == "admin", str(admin.get("role")))

print("== 普通用户不能进 Console ==")
user = auth.create_local_user(username="qauser", password="QaUser@123", name="用户", role="user")
check("创建 user", user.get("role") == "user", str(user))
try:
    auth.login(username="qauser", password="QaUser@123", client="console")
    check("user console 被拒", False, "should raise")
except PermissionError as e:
    check("user console 被拒", "仅管理员" in str(e), str(e))

studio = auth.login(username="qauser", password="QaUser@123", client="studio")
check("user studio 可登录", bool(studio.get("token")) and studio.get("role") == "user")

print("== 邮箱注册免验证码 ==")
reg = auth.register(email="newbie@example.com", password="Newbie@123", name="新用户")
check("注册成功", bool(reg.get("token")) and reg.get("role") == "user", str(reg.get("role")))
check("无验证码也能注册", True)
try:
    auth.register(email="newbie@example.com", password="Newbie@123")
    check("重复邮箱被拒", False)
except ValueError as e:
    check("重复邮箱被拒", "已经注册" in str(e), str(e))
try:
    auth.register(email="not-an-email", password="Newbie@123")
    check("坏邮箱被拒", False)
except ValueError as e:
    check("坏邮箱被拒", "有效邮箱" in str(e), str(e))

print("== status / bootstrap 门禁 ==")
try:
    auth.status(studio["token"], client="console")
    check("status console 拒 user", False)
except PermissionError as e:
    check("status console 拒 user", "仅管理员" in str(e), str(e))
boot = auth.bootstrap(admin["token"], client="console")
check("admin bootstrap", boot.get("capabilities", {}).get("manage_users") is True)

if failures:
    print(f"\nFAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("\nALL OK — auth roles")
