"""用例脚本运行时。脚本只拿到 case，设备动作经现有派发口出去。"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from mino_nexus.contract.step_pointer import SeqNode, build_seq_nodes
from mino_nexus.core.protocol import EventStatus
from mino_nexus.core.schemas import PlanEvent
from mino_nexus.loop.observe.agent_stream import emit_agent_event

_SCRIPT_FILES = frozenset({
    "project_pretest.py", "case_pretest.py", "case_run.py", "case_posttest.py", "project_posttest.py",
})


_HIDDEN_RESULT_KEYS = frozenset({
    "otp", "password", "text", "image_base64", "image", "secret", "token",
})


def _public_data(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if str(key) not in _HIDDEN_RESULT_KEYS}


def _plain_mark(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    mark: dict[str, Any] = {}
    for key, value in item.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            mark[str(key)] = value if str(key) == "thumb" else (value[:200] if isinstance(value, str) else value)
    return mark or None


def _note_fields(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        name = str(key or "").strip()
        if not name or name in {"name", "text", "summary", "phase", "status", "call_index"}:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[name] = value[:500] if isinstance(value, str) else value
        elif isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) or item is None for item in value[:40]):
            out[name] = value[:40]
        elif isinstance(value, list) and len(value) <= 40 and all(_plain_mark(item) for item in value):
            out[name] = [_plain_mark(item) for item in value]
    return out


def _prep_lines(case: dict[str, Any]) -> dict[int, str]:
    """前置按编号对齐。没有编号时整段算第 1 条。"""
    from mino_nexus.core.case_text import parse_numbered_items_rules

    raw = str(case.get("precondition") or "").strip()
    if not raw:
        return {}
    parsed = [it for it in (parse_numbered_items_rules(raw) or []) if str(it.get("text") or "").strip()]
    if len(parsed) > 1 or (len(parsed) == 1 and raw[:1].isdigit()):
        out: dict[int, str] = {}
        for it in parsed:
            number = int(it.get("num") or 0)
            text = str(it.get("text") or "").strip()
            if number and text:
                out[number] = text
        if out:
            return out
    return {1: raw}


class ScriptHalt(BaseException):
    """墙钟耗尽或任务取消。脚本里的 except Exception 接不住。"""

    def __init__(self, message: str, *, reason_code: str):
        self.reason_code = reason_code
        super().__init__(message)


class ScriptAbort(Exception):
    """步骤、断言或脚本主动失败。"""

    def __init__(self, message: str, *, reason_code: str = "script_failed"):
        self.reason_code = reason_code
        super().__init__(message)


class ActionResult:
    """一次动作的结果。脚本用 ok 决定要不要继续。"""

    def __init__(self, *, ok: bool, summary: str, status: str, data: dict[str, Any] | None = None):
        self.ok = bool(ok)
        self.summary = str(summary or "")
        self.status = str(status or "")
        self.data = dict(data or {})

    def __bool__(self) -> bool:
        return self.ok


class _StepScope:
    def __init__(self, runtime: "CaseRuntime", number: int, role: str):
        self._runtime = runtime
        self._number = number
        self._role = role

    def __enter__(self) -> "CaseRuntime":
        self._runtime._enter_step(self._number, self._role)
        return self._runtime

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._runtime._leave_step(failed=exc is not None)
        return False


class CaseRuntime:
    """注入脚本的 case。操作和校验用同一步编号，分别取步骤原文和预期原文。"""

    def __init__(
        self,
        *,
        case: dict[str, Any],
        ctx: Any,
        proxy: Any,
        writer: Any,
        run_id: str,
        stream_id: str,
        app_id: str,
        provider_id: str,
        deadline: float,
        cancel_check,
    ):
        self.case = case
        self.ctx = ctx
        self.proxy = proxy
        self.writer = writer
        self.run_id = run_id
        self.stream_id = stream_id
        self.case_id = str(case.get("case_id") or "")
        self.app_id = app_id
        self.provider_id = str(provider_id or "")
        self.deadline = float(deadline or 0)
        self.cancel_check = cancel_check
        self.phase = "run"
        self.steps: list[dict[str, Any]] = []
        self._nodes = {node.n: node for node in build_seq_nodes(case)}
        self._prep = _prep_lines(case)
        self._current: SeqNode | None = None
        self._last: SeqNode | None = None
        self._role = ""
        self._depth = 0
        self._seq = 0
        self._log_seq = 0
        self._sample_seq = 0
        self._shot = None
        self._frames: list[Any] = []

    @property
    def current_step(self) -> SeqNode | None:
        return self._current or self._last

    def begin_phase(self, phase: str) -> None:
        self.phase = str(phase or "run")
        if self._depth == 0:
            self._current = None
            self._last = None
            self._role = ""

    def prep(self, number: int) -> _StepScope:
        """前置。日志标题用前置原文。"""
        return self._scope(number, "prep")

    def do(self, number: int) -> _StepScope:
        """操作。日志标题用步骤原文。"""
        return self._scope(number, "do")

    def check(self, number: int) -> _StepScope:
        """校验。日志标题用同号预期原文。"""
        return self._scope(number, "check")

    def _scope(self, number: int, role: str) -> _StepScope:
        try:
            parsed = int(number)
        except (TypeError, ValueError) as exc:
            raise ScriptAbort(f"步骤编号无效：{number}", reason_code="script_step_missing") from exc
        return _StepScope(self, parsed, role)

    def launch(self) -> ActionResult:
        package = str(getattr(self.ctx, "target_package", "") or "")
        return self.event("launch_app", package=package)

    def close(self) -> ActionResult:
        return self.event("close_app")

    def tap(self, target: str, **params: Any) -> ActionResult:
        text = str(target or "")
        extra = dict(params)
        extra.setdefault("selector_text", text)
        return self.event("tap_element", target=text, **extra)

    def input(self, target: str, text: str, **params: Any) -> ActionResult:
        from mino_nexus.core.secret_redaction import remember

        remember(str(text or ""))
        return self.event("input_text", target=str(target or ""), text=str(text or ""), **params)

    def swipe(self, direction: str, **params: Any) -> ActionResult:
        return self.event("swipe", direction=str(direction or ""), **params)

    def back(self) -> ActionResult:
        return self.event("press_key", key="back")

    def wait(self, ms: int) -> ActionResult:
        return self.event("wait_ms", ms=int(ms))

    def screenshot(self) -> ActionResult:
        started = time.perf_counter()
        self._guard()
        shot = self.proxy.observe("screenshot", force_fresh=True)
        self._shot = shot
        self._publish_screen(shot)
        ok = bool(shot.has_image()) if callable(getattr(shot, "has_image", None)) else bool(getattr(shot, "ok", False))
        summary = "已截图" if ok else (str(getattr(shot, "error", "") or "") or "没有截到图")
        result = ActionResult(
            ok=ok,
            summary=summary,
            status="pass" if ok else "fail",
            data={"width": int(getattr(shot, "width", 0) or 0), "height": int(getattr(shot, "height", 0) or 0)},
        )
        self._record("screenshot", result, started=started)
        return result

    def _publish_screen(self, shot, *, timeline: bool = True, name: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """缩略图落到会话目录。timeline 为真时再追加一条 observe/screen。"""
        stored = self._store_thumb(shot, name=name)
        if not stored or not timeline or self.writer is None:
            return stored
        body = {
            "w": stored["w"],
            "h": stored["h"],
            "mime": "image/jpeg",
            "thumb": stored["thumb"],
            "thumb_len": stored["thumb_len"],
            "screenshot_hash": stored["screenshot_hash"],
            "raw_image_hash": stored["raw_image_hash"],
            "source": stored["source"],
            "original_mime": stored["original_mime"],
        }
        if extra:
            body.update(extra)
        self.writer.append("observe/screen", body)
        return stored

    def _store_thumb(self, shot, *, name: str = "") -> dict[str, Any]:
        if self.writer is None or not callable(getattr(shot, "has_image", None)) or not shot.has_image():
            return {}
        from mino_nexus.loop.observe.agent_stream import make_thumb
        from mino_nexus.loop.observe.screen_store import publish_screen

        thumb = make_thumb(shot.image_base64)
        if not thumb:
            return {}
        session_id = str(getattr(self.writer, "session_id", "") or "")
        label = name or f"script-{self._log_seq + 1}"
        ref, shot_hash = publish_screen(session_id, label, thumb)
        if not ref:
            return {}
        import hashlib

        return {
            "w": int(getattr(shot, "width", 0) or 0),
            "h": int(getattr(shot, "height", 0) or 0),
            "thumb": ref,
            "thumb_len": len(thumb),
            "screenshot_hash": shot_hash,
            "raw_image_hash": hashlib.sha256(str(shot.image_base64).encode("ascii", errors="ignore")).hexdigest()[:20],
            "source": str(getattr(shot, "source", "") or "script"),
            "original_mime": str(getattr(shot, "image_mime", "") or ""),
        }

    def note(self, text: str = "", **fields: Any) -> None:
        """把自定义证据写进会话。不查能力目录，也不派到设备。"""
        self._guard()
        self._log_seq += 1
        body = str(text or "")[:500]
        payload = {
            "name": "note",
            "summary": body[:300],
            "phase": self.phase,
            "status": "pass",
            "call_index": self._log_seq,
            **_note_fields(fields),
            **self._step_fields(),
            "text": body,
        }
        if self.writer is not None:
            self.writer.append("script/note", payload)
        self._emit(
            phase="result",
            step=int(payload.get("case_step") or 0),
            role=str(payload.get("role") or ""),
            step_text=body,
            thought=body,
            capability_id="note",
            status="pass",
            summary=body[:300],
        )

    def sample(self, duration_ms: int, interval_ms: int = 300, *, timeline: bool = True) -> dict[str, Any]:
        """按间隔只截图，不调用模型。默认每一帧各进时间线一张。timeline 为假时时间线只留封面，整组帧随记录返回。"""
        duration = max(0, min(int(duration_ms), 30_000))
        interval = max(200, min(int(interval_ms), 5_000))
        started = time.perf_counter()
        deadline = started + duration / 1000
        self._sample_seq += 1
        group = f"sample-{self._sample_seq}"
        frames: list[dict[str, Any]] = []
        self._frames = []
        while len(frames) < 40:
            self._guard()
            shot_started = time.perf_counter()
            shot = self.proxy.observe("screenshot", force_fresh=True)
            self._shot = shot
            index = len(frames)
            stored = self._publish_screen(
                shot,
                timeline=timeline,
                name=f"{group}-{index}",
                extra={"sample_group": group, "sample_index": index} if timeline else None,
            )
            ok = bool(shot.has_image()) if callable(getattr(shot, "has_image", None)) else bool(getattr(shot, "ok", False))
            frame = {
                "index": index,
                "t_ms": int((time.perf_counter() - started) * 1000),
                "ok": ok,
                "width": int(getattr(shot, "width", 0) or 0),
                "height": int(getattr(shot, "height", 0) or 0),
                "thumb": str(stored.get("thumb") or ""),
            }
            frames.append(frame)
            self._frames.append(shot)
            if time.perf_counter() >= deadline:
                break
            remain = interval / 1000 - (time.perf_counter() - shot_started)
            if remain > 0:
                time.sleep(remain)
        elapsed = int((time.perf_counter() - started) * 1000)
        marks = [{"index": frame["index"], "t_ms": frame["t_ms"], "thumb": frame["thumb"]} for frame in frames if frame["thumb"]]
        cover = marks[-1]["thumb"] if marks else ""
        if not timeline and cover and self.writer is not None:
            self.writer.append("observe/screen", {
                "mime": "image/jpeg",
                "thumb": cover,
                "source": "sample",
                "sample_group": group,
                "sample_cover": True,
            })
        text = f"采样 {len(frames)} 帧，耗时 {elapsed} ms"
        step = self._step_fields()
        if self.writer is not None:
            self.writer.append("script/note", {
                "name": "sample",
                "summary": text,
                "phase": self.phase,
                "status": "pass",
                "timeline": timeline,
                "sample_group": group,
                "frames": marks,
                "thumb": "" if timeline else cover,
                **step,
                "text": text,
            })
        total = len(frames)
        if timeline:
            for frame in frames:
                if not frame["thumb"]:
                    continue
                self._emit(
                    phase="result",
                    step=int(step.get("case_step") or 0),
                    role=str(step.get("role") or ""),
                    capability_id="sample_frame",
                    status="pass",
                    summary=f"采集帧 {frame['index'] + 1}/{total}",
                    thumb=frame["thumb"],
                    sample_group=group,
                    sample_index=frame["index"],
                    timeline=True,
                )
        else:
            self._emit(
                phase="result",
                step=int(step.get("case_step") or 0),
                role=str(step.get("role") or ""),
                capability_id="sample",
                status="pass",
                summary=text,
                thumb=cover,
                frames=marks,
                sample_group=group,
                timeline=False,
            )
        return {"elapsed_ms": elapsed, "interval_ms": interval, "timeline": timeline, "sample_group": group, "frames": frames}

    def vlm_frame(self, index: int, prompt: str, *, verdict: str = "") -> str:
        """对 sample 留下的某一帧做视觉判断。采集阶段不调用模型。"""
        try:
            shot = self._frames[int(index)]
        except (IndexError, TypeError, ValueError) as exc:
            raise ScriptAbort(f"没有第 {index} 帧", reason_code="observation_missing") from exc
        return self.vlm(prompt, image=shot, verdict=verdict)

    def hierarchy(self) -> ActionResult:
        started = time.perf_counter()
        self._guard()
        shot = self.proxy.observe("hierarchy", force_fresh=True)
        detail = getattr(shot, "remote_detail", None)
        text = ""
        if isinstance(detail, dict):
            text = str(detail.get("hierarchy_text") or detail.get("text") or "")[:4000]
        ok = bool(getattr(shot, "ok", False))
        result = ActionResult(
            ok=ok,
            summary="已读取界面结构" if ok else (str(getattr(shot, "error", "") or "") or "没有读到界面结构"),
            status="pass" if ok else "fail",
            data={"text": text},
        )
        self._record("hierarchy", result, started=started)
        return result

    def account(self) -> SimpleNamespace:
        self._guard()
        row = dict(getattr(self.ctx, "picked_account", None) or {})
        if not str(row.get("id") or row.get("account_id") or "").strip():
            raise ScriptAbort("这条用例没有租到账号", reason_code="resource_account_missing")
        return SimpleNamespace(
            id=str(row.get("id") or row.get("account_id") or ""),
            email=str(row.get("email") or ""),
            phone=str(row.get("phone") or ""),
            username=str(row.get("username") or ""),
            password=str(row.get("password") or ""),
            otp=str(row.get("otp") or ""),
            ident=str(row.get("ident") or ""),
        )

    def otp_mail(self) -> str:
        result = self.event("get_otp")
        if not result.ok:
            raise ScriptAbort(result.summary or "没有取到验证码", reason_code="otp_environment_unavailable")
        return str((getattr(self.ctx, "picked_account", None) or {}).get("otp") or "")

    def llm(self, prompt: str, *, text: str = "", schema: dict[str, Any] | None = None) -> Any:
        started = time.perf_counter()
        self._guard()
        from mino_nexus.platform.llm.client import call_chat_text
        from mino_nexus.platform.settings import get_ai_provider_credentials

        provider = get_ai_provider_credentials(self.provider_id or None)
        if not provider.get("configured") or not provider.get("case_execution_use"):
            raise ScriptAbort("未配置可用于用例的模型", reason_code="model_unconfigured")
        user = str(prompt or "")
        if text:
            user = f"{user}\n\n{text}"
        messages = [
            {"role": "system", "content": "返回 JSON。没有单独 schema 时只含 text 字段，回答放进 text。"},
            {"role": "user", "content": user},
        ]
        raw, meta = call_chat_text(
            provider=provider,
            messages=messages,
            temperature=0.1,
            max_tokens=1024,
            timeout_sec=self._model_timeout(),
            json_mode=True,
            response_schema=schema,
            deadline_monotonic=self._model_deadline(),
        )
        summary = str((raw or {}).get("text") or meta.get("error") or "")[:300]
        self._record_model("llm", summary, ok=raw is not None, error=str(meta.get("error") or ""), started=started)
        if raw is None:
            raise ScriptAbort(str(meta.get("error") or "") or "模型没有返回结果", reason_code="model_empty")
        if schema:
            return raw
        return str(raw.get("text") or "")

    def vlm(self, prompt: str, *, image: Any = None, verdict: str = "") -> Any:
        started = time.perf_counter()
        self._guard()
        from mino_nexus.platform.llm.client import call_chat_text
        from mino_nexus.platform.settings import get_ai_provider_credentials

        provider = get_ai_provider_credentials(self.provider_id or None)
        if not provider.get("configured") or not provider.get("case_execution_use"):
            raise ScriptAbort("未配置可用于用例的模型", reason_code="model_unconfigured")
        shot = image if image is not None else self._fresh_shot()
        encoded = str(getattr(shot, "image_base64", "") or "")
        if not encoded:
            raise ScriptAbort("没有可用截图，不能做视觉判断", reason_code="observation_missing")
        mime = str(getattr(shot, "image_mime", "") or "image/png")
        ask = str(prompt or "")
        if verdict == "pass":
            ask += '\n只返回 JSON：{"passed": true 或 false, "text": "看到了什么"}'
        else:
            ask += '\n只返回 JSON：{"text": "看到了什么"}'
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": ask},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ],
        }]
        raw, meta = call_chat_text(
            provider=provider,
            messages=messages,
            temperature=0.1,
            max_tokens=1024,
            timeout_sec=self._model_timeout(),
            json_mode=True,
            deadline_monotonic=self._model_deadline(),
        )
        if raw is None:
            self._record_model("vlm", str(meta.get("error") or ""), ok=False, error=str(meta.get("error") or ""), started=started)
            raise ScriptAbort(str(meta.get("error") or "") or "视觉模型没有返回结果", reason_code="model_empty")
        passed = bool(raw.get("passed"))
        summary = str(raw.get("text") or "")[:300]
        self._record_model("vlm", summary, ok=passed or verdict != "pass", error="" if passed or verdict != "pass" else summary, started=started)
        if verdict == "pass" and not passed:
            raise ScriptAbort(summary or "视觉断言未通过", reason_code="vlm_failed")
        return summary

    def event(self, name: str, **params: Any) -> ActionResult:
        started = time.perf_counter()
        self._guard()
        self._seq += 1
        cap = str(name or "").strip()
        if not cap:
            raise ScriptAbort("动作名为空", reason_code="script_failed")
        event = PlanEvent(
            seq=self._seq,
            capability_id=cap,
            event_kind=cap,
            params={key: value for key, value in params.items() if value is not None},
            case_step_index=self._current.n if self._current else None,
            label=self._label(),
        )
        result = self.proxy.dispatch(
            event,
            run_id=self.stream_id,
            step_idx=self._seq,
            ctx=self.ctx,
            shot=self._shot,
        )
        status = str(getattr(result.status, "value", result.status) or "")
        action = ActionResult(
            ok=status == EventStatus.PASS.value,
            summary=str(getattr(result, "summary", "") or ""),
            status=status,
            data=_public_data(getattr(result, "raw_response", None)),
        )
        self._record(cap, action, started=started)
        return action

    def fail(self, message: str) -> None:
        raise ScriptAbort(str(message or "脚本判定失败"), reason_code="script_failed")

    def _enter_step(self, number: int, role: str) -> None:
        self._guard()
        if self._depth:
            raise ScriptAbort("步骤不能嵌套", reason_code="script_step_missing")
        if role == "prep":
            text = self._prep.get(number, "")
            if not str(text or "").strip():
                raise ScriptAbort(f"用例中没有前置 {number}", reason_code="script_step_missing")
            node = SeqNode(n=number, instruction=text, expected="")
        else:
            node = self._nodes.get(number)
            if node is None:
                raise ScriptAbort(f"用例中没有步骤 {number}", reason_code="script_step_missing")
            text = node.instruction if role == "do" else node.expected
            if not str(text or "").strip():
                missing = "操作" if role == "do" else "校验"
                hint = ""
                if role == "check":
                    nums = [str(n) for n, item in sorted(self._nodes.items()) if str(item.expected or "").strip()]
                    hint = f"。check 的编号和操作步骤是同一个号，这条用例写了预期的步骤是 {'、'.join(nums) or '无'}"
                raise ScriptAbort(f"用例中步骤 {number} 没有{missing}{hint}", reason_code="script_step_missing")
        self._current = node
        self._last = node
        self._role = role
        self._depth = 1
        shown = self._shown(node, role)
        if self.writer is not None:
            self.writer.set_turn(number)
            self.writer.set_phase(self.phase)
            self.writer.append("node/enter", {
                "n": node.n,
                "role": role,
                "text": shown,
                "instruction": node.instruction,
                "expected": node.expected,
                "case_step": node.n,
            })
        self._emit(phase="step", step=node.n, role=role, step_text=shown, thought=shown, summary=shown)

    def _leave_step(self, *, failed: bool) -> None:
        node = self._current
        role = self._role
        self._depth = 0
        self._current = None
        self._role = ""
        if node is None:
            return
        shown = self._shown(node, role)
        status = "fail" if failed else "pass"
        if self.writer is not None:
            self.writer.append("node/leave", {
                "n": node.n, "role": role, "status": status, "case_step": node.n, "text": shown,
            })
            self.writer.set_turn(0)
        self._emit(
            phase="result",
            step=node.n,
            role=role,
            step_text=shown,
            thought=shown,
            summary=shown,
            status=status,
        )

    def watch(self, frame, event, arg):
        """脚本自己的每一行都看取消和墙钟，纯循环也能停。"""
        del arg
        if frame.f_code.co_filename not in _SCRIPT_FILES:
            return None
        if event == "line":
            self._guard()
        return self.watch

    def _guard(self) -> None:
        if self.phase == "posttest":
            return
        if self.cancel_check and self.cancel_check():
            raise ScriptHalt("任务已取消", reason_code="task_cancelled")
        if self.deadline and time.time() >= self.deadline:
            raise ScriptHalt("超过用例时间，未再派发动作", reason_code="case_budget_exhausted")

    def _shown(self, node: SeqNode, role: str) -> str:
        if role == "prep":
            text, prefix = node.instruction, "前置"
        elif role == "check":
            text, prefix = node.expected, "校验"
        else:
            text, prefix = node.instruction, "操作"
        return f"{prefix} · {text}"

    def _label(self) -> str:
        node = self._current
        if node is None:
            return "未挂步骤"
        return self._shown(node, self._role or "do")

    def _step_fields(self) -> dict[str, Any]:
        node = self._current
        if node is None:
            return {"case_step": 0, "role": "", "text": "未挂步骤", "instruction": "未挂步骤"}
        shown = self._shown(node, self._role or "do")
        return {
            "case_step": node.n,
            "role": self._role,
            "text": shown,
            "instruction": node.instruction,
            "expected": node.expected,
        }

    def _record(self, name: str, result: ActionResult, *, started: float | None = None) -> None:
        fields = self._step_fields()
        shown = str(fields.get("text") or "")
        summary = f"{shown} · {result.summary}" if shown else result.summary
        duration_ms = int((time.perf_counter() - started) * 1000) if started is not None else None
        self._log_seq += 1
        payload = {
            "name": name,
            "status": result.status,
            "summary": summary[:300],
            "phase": self.phase,
            "duration_ms": duration_ms,
            "call_index": self._log_seq,
            **fields,
        }
        if self.writer is not None:
            self.writer.append("script/call", payload)
        self.steps.append({"name": name, "status": result.status, "summary": summary[:300], "case_step": fields["case_step"]})
        self._emit(
            phase="result",
            step=int(fields["case_step"] or 0),
            role=fields.get("role") or "",
            step_text=shown,
            thought=shown,
            capability_id=name,
            status=result.status,
            duration_ms=duration_ms,
            summary=summary[:300],
        )

    def _record_model(self, kind: str, summary: str, *, ok: bool, error: str, started: float | None = None) -> None:
        result = ActionResult(ok=ok, summary=summary, status="pass" if ok else "fail")
        self._record(kind, result, started=started)
        if error and self.writer is not None:
            self.writer.append("llm/response", {
                "job": f"script-{kind}",
                "status": "pass" if ok else "fail",
                "error": error[:300],
                **self._step_fields(),
            })

    def _fresh_shot(self):
        shot = self.proxy.observe("screenshot", force_fresh=True)
        self._shot = shot
        return shot

    def _model_timeout(self) -> int:
        if not self.deadline:
            return 60
        remaining = int(self.deadline - time.time())
        return max(1, min(60, remaining))

    def _model_deadline(self) -> float | None:
        if not self.deadline:
            return None
        return time.monotonic() + max(0.001, self.deadline - time.time())

    def _emit(self, **payload: Any) -> None:
        emit_agent_event({
            "run_id": self.stream_id,
            "task_id": self.run_id,
            "case_id": self.case_id,
            "app_id": self.app_id,
            "loop_phase": self.phase,
            "source": "case_script",
            **payload,
        })
