"""唯一模型 SDK 边界。密钥由执行账号显式传入，不使用环境或全局 Key。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from functools import lru_cache
from importlib.metadata import version
from typing import Any

# 使用所固定 SDK 自带的注册表，禁止启动时联网覆盖价格/能力数据。
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_TELEMETRY"] = "False"

PROVIDER_ALIASES = {"google": "gemini", "qwen": "dashscope"}
KEY_PROVIDERS = ("openai", "anthropic", "gemini", "deepseek", "dashscope", "volcengine",
                 "groq", "mistral", "xai", "openrouter", "azure", "aihubmix", "moonshot", "cerebras", "together_ai", "fireworks_ai")


@lru_cache(maxsize=1)
def sdk():
    import litellm
    litellm.suppress_debug_info = True
    litellm.set_verbose = False
    litellm.turn_off_message_logging = True
    litellm.log_raw_request_response = False
    litellm.drop_params = False
    return litellm


def sdk_provider(provider: dict) -> str:
    explicit = str(provider.get("sdk_provider") or "").strip()
    pid = str(provider.get("id") or "").strip()
    native = PROVIDER_ALIASES.get(pid, pid)
    result = explicit or (native if native in KEY_PROVIDERS else str(provider.get("api_type") or "openai"))
    if result not in KEY_PROVIDERS:
        raise ValueError("请选择受支持的 API Key 接入方式；兼容接口请选择 openai")
    return result


def config_fingerprint(provider: dict) -> str:
    data = {k: provider.get(k) for k in ("id", "sdk_provider", "api_type", "base_url", "model", "embedding_model", "api_key", "model_parameters", "pricing")}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def connection(provider: dict, *, model: str | None = None) -> dict:
    from mino_nexus.services.settings import validate_provider_owner
    validate_provider_owner(provider)
    key = str(provider.get("api_key") or "").strip()
    model = str(model if model is not None else provider.get("model") or "").strip()
    if not key or not model:
        raise ValueError("当前执行账号未配置 Key 或模型 ID")
    native = sdk_provider(provider)
    result = {"model": model, "custom_llm_provider": native, "api_key": key,
              "num_retries": 0, "drop_params": False}
    base = str(provider.get("base_url") or "").strip().rstrip("/")
    if base:
        from urllib.parse import urlparse
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("模型地址须为不含凭据或查询参数的 HTTP(S) 地址")
        result["api_base"] = base
    return result


def model_info(native: str, model: str) -> dict:
    llm = sdk()
    key = f"{native}/{model}"
    info = llm.model_cost.get(key)
    if info is None:
        plain = llm.model_cost.get(model)
        if isinstance(plain, dict) and plain.get("litellm_provider") == native:
            info = plain
    info = info if isinstance(info, dict) else {}
    try:
        supported = llm.get_supported_openai_params(model=model, custom_llm_provider=native) or []
    except Exception:
        supported = []
    return {"id": model, "sdk_registered": bool(info), "mode": info.get("mode", "unknown"), "supported_parameters": supported,
            "supports_vision": info.get("supports_vision"),
            "supports_function_calling": info.get("supports_function_calling"),
            "supports_response_schema": info.get("supports_response_schema"),
            "max_input_tokens": info.get("max_input_tokens"), "max_output_tokens": info.get("max_output_tokens"),
            "input_cost_per_token": info.get("input_cost_per_token"),
            "output_cost_per_token": info.get("output_cost_per_token"),
            "deprecation_date": info.get("deprecation_date"), "maintenance_status": "unknown",
            "account_access": "unverified", "source": "litellm_bundled_registry"}


@lru_cache(maxsize=32)
def catalog(native: str) -> dict:
    if native not in KEY_PROVIDERS:
        raise ValueError("不支持的接入方式")
    rows = {}
    for key, info in sdk().model_cost.items():
        if not isinstance(info, dict) or info.get("litellm_provider") != native:
            continue
        model = key[len(native) + 1:] if key.startswith(native + "/") else key
        if info.get("mode") not in ("chat", "embedding"):
            continue
        rows[model] = model_info(native, model)
    return {"sdk": "LiteLLM", "sdk_version": version("litellm"), "sdk_provider": native,
            "models": sorted(rows.values(), key=lambda row: row["id"]), "providers": list(KEY_PROVIDERS),
            "notice": "SDK 注册表表示适配和估价信息，不证明服务商仍维护或当前账号有权限；自定义 ID/接入点需主动验证。"}


def error_meta(exc: Exception, provider: dict) -> dict:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    body = getattr(exc, "body", None)
    if not isinstance(body, dict) and response is not None:
        try:
            body = response.json()
        except Exception:
            body = None
    err = body.get("error", body) if isinstance(body, dict) else {}
    err = err if isinstance(err, dict) else {}
    def identifier(value):
        text = str(value or "")
        key = str(provider.get("api_key") or "")
        if key:
            text = text.replace(key, "redacted")
        return re.sub(r"[^\w.\-:/]", "", text)[:160]
    code = identifier(err.get("code") or getattr(exc, "code", ""))
    param = identifier(err.get("param") or getattr(exc, "param", ""))
    if not param and type(exc).__name__ == "UnsupportedParamsError":
        unsupported = re.search(r"does not support parameters:\s*\[([^\]]+)\]", str(exc))
        if unsupported:
            names = re.findall(r"['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]", unsupported.group(1))
            param = identifier("/".join(names))
    if not param and isinstance(err.get("message"), str):
        named_param = re.search(r"(?:parameter|param)\s+[`'\"]?([A-Za-z_][A-Za-z0-9_.]*)", err["message"], re.IGNORECASE)
        if named_param and named_param.group(1).split(".")[0] in {"model", "messages", "temperature", "max_tokens", "max_completion_tokens", "top_p", "thinking", "stream", "stream_options", "tools", "tool_choice", "parallel_tool_calls", "response_format"}:
            param = identifier(named_param.group(1))
    headers = getattr(response, "headers", {}) or {}
    sdk_headers = getattr(exc, "litellm_response_headers", {}) or {}
    request_id = identifier(headers.get("x-request-id") or headers.get("request-id") or sdk_headers.get("x-request-id") or sdk_headers.get("request-id") or sdk_headers.get("x-litellm-provider-request-id") or getattr(exc, "request_id", "") or err.get("request_id") or (body.get("request_id") if isinstance(body, dict) else ""))
    kind = "http" if status else "transport"
    if isinstance(exc, ValueError) or type(exc).__name__ == "UnsupportedParamsError":
        kind = "configuration"
    transient = status in (408, 409, 425, 429, 500, 502, 503, 504) or (not status and type(exc).__name__ in ("Timeout", "APIConnectionError", "ConnectTimeout", "ReadTimeout"))
    # SDK 异常字符串常包含原始请求/响应；只持久化稳定错误码、参数和请求 ID。
    detail = f"模型调用失败：{type(exc).__name__}" + (f"（HTTP {status}）" if status else "")
    if code:
        detail += f"；服务商代码 {code}"
    if param:
        detail += f"；参数 {param}"
    return {"error": detail, "fail_kind": kind, "http_status": status,
            "provider_error_code": code, "provider_error_param": param,
            "provider_request_id": request_id, "retryable": transient}


def cost_meta(response: dict | None, provider: dict, *, model: str, kind: str) -> dict:
    usage = response.get("usage") if isinstance(response, dict) else None
    usage = usage if isinstance(usage, dict) and usage else None
    # SDK 为缺失 usage 的响应补全全零对象。非空模型请求没有可证明的零 token，
    # 保守记为未知，避免把 SDK 默认值冒充服务商实际返回。
    if usage and not any(usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens")):
        usage = None
    result = {"usage": usage, "usage_status": "reported" if usage else "unknown",
              "estimated_cost_usd": None, "cost_status": "unknown", "cost_source": "unknown",
              "pricing_version": version("litellm"), "currency": "USD"}
    if not usage:
        return result
    native = sdk_provider(provider)
    info = model_info(native, model)
    price = provider.get("pricing") if kind == "chat" else provider.get("embedding_pricing")
    price = price if isinstance(price, dict) else {}
    custom = None
    if price.get("input_per_million_usd") is not None and (kind == "embedding" or price.get("output_per_million_usd") is not None):
        custom = {"input_cost_per_token": float(price["input_per_million_usd"]) / 1e6,
                  "output_cost_per_token": float(price.get("output_per_million_usd") or 0) / 1e6}
    # 自定义代理地址不保证与官方同价，只有显式单价才能估价。
    known_price = info.get("input_cost_per_token") is not None and (kind == "embedding" or info.get("output_cost_per_token") is not None)
    if not custom and (not known_price or provider.get("custom_pricing_required")):
        return result
    result["pricing_snapshot"] = custom or {k: v for k, v in sdk().model_cost.get(f"{native}/{model}", sdk().model_cost.get(model, {})).items() if "cost" in k and isinstance(v, (int, float))}
    try:
        amount = sdk().completion_cost(completion_response=response, model=model,
                                      custom_llm_provider=native, call_type="embedding" if kind == "embedding" else "completion",
                                      custom_cost_per_token=custom)
        if amount is not None and math.isfinite(amount) and amount >= 0:
            result.update(estimated_cost_usd=round(amount, 12), cost_status="estimated",
                          cost_source="configured_unit_price" if custom else "litellm_bundled_registry")
    except Exception:
        pass
    return result


def chat_response_structure(value: Any, *, offered_names: set[str]) -> dict:
    """只取响应形状，不保存正文、参数值、任意字段名或未声明的工具名。"""
    if not isinstance(value, dict):
        return {"available": False}
    choices = value.get("choices")
    if not isinstance(choices, list):
        return {"available": False}
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
    body = message or delta
    calls = body.get("tool_calls")
    calls = calls if isinstance(calls, list) else []
    legacy = body.get("function_call")
    if not calls and isinstance(legacy, dict):
        calls = [{"function": legacy}]
    rows = []
    named_count = 0
    for call in calls:
        fn = call.get("function") if isinstance(call, dict) else None
        fn = fn if isinstance(fn, dict) else {}
        name = fn.get("name")
        named = isinstance(name, str) and bool(name.strip())
        named_count += int(named)
        args = fn.get("arguments")
        if len(rows) < 16:
            rows.append({"name_present": named, "name_in_menu": name in offered_names if isinstance(name, str) else False,
                         "arguments_type": "string" if isinstance(args, str) else "object" if isinstance(args, dict) else "null" if args is None else "other",
                         "arguments_chars": len(args) if isinstance(args, str) else 0})
    finish = choice.get("finish_reason")
    return {"available": True, "choice_count": len(choices),
            "finish_reason": finish if finish is None or isinstance(finish, str) and finish in {"stop", "length", "tool_calls", "function_call", "content_filter", "error", "abort"} else "other",
            "body_kind": "message" if message else "delta" if delta else "none",
            "content_present": bool(body.get("content")), "refusal_present": bool(body.get("refusal")),
            "reasoning_present": bool(body.get("reasoning_content") or body.get("reasoning") or body.get("thinking_blocks")),
            "tool_count": len(calls), "named_tool_count": named_count, "calls": rows}


class ChatResponseTrace:
    """每个请求独立观察 SDK 边界；回调不保留包含 Key/正文的完整 details。

    当前 OpenAI 兼容路径的 post_call 在 provider SDK 解析后触发，不是 HTTP
    原始响应。流式路径可能只给包装器，因此明确标注不可观察，不推测上游。
    """
    def __init__(self, tools: Any):
        self.offered_names = {str(t["function"]["name"]) for t in (tools or [])
                              if isinstance(t, dict) and isinstance(t.get("function"), dict) and isinstance(t["function"].get("name"), str)}
        self.data = {"version": 1, "wire_response_available": False,
                     "request": {"available": False},
                     "provider_parsed": {"available": False, "source": "sdk_post_call"},
                     "sdk": {"available": False}}
        self.stream_chunks = 0
        self.stream_named_chunks = 0
        self.stream_tool_chunks = 0
        self.stream_finish = None

    def logger(self, details: dict) -> None:
        try:
            event = details.get("log_event_type")
            if event == "pre_api_call":
                additional = details.get("additional_args") or {}
                request = additional.get("complete_input_dict")
                if isinstance(request, dict):
                    request_tools = request.get("tools")
                    request_tools = request_tools if isinstance(request_tools, list) else []
                    names = {t["function"]["name"] for t in request_tools
                             if isinstance(t, dict) and isinstance(t.get("function"), dict) and isinstance(t["function"].get("name"), str)}
                    choice = request.get("tool_choice")
                    self.data["request"] = {"available": True, "tool_count": len(request_tools),
                                            "tool_names_match": names == self.offered_names,
                                            "tool_choice": choice if isinstance(choice, str) and choice in {"required", "auto", "none"} else "named" if isinstance(choice, dict) else "unknown"}
            elif event == "post_api_call":
                original = details.get("original_response")
                if isinstance(original, str):
                    if len(original) > 1024 * 1024:
                        return
                    try:
                        original = json.loads(original)
                    except (ValueError, TypeError):
                        return
                structure = chat_response_structure(original, offered_names=self.offered_names)
                if structure["available"]:
                    self.data["provider_parsed"] = {**structure, "source": "sdk_post_call"}
                headers = details.get("response_headers")
                if isinstance(headers, dict):
                    request_id = headers.get("x-request-id") or headers.get("request-id")
                    if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", request_id):
                        self.data["provider_request_id"] = request_id
        except Exception:
            # 诊断不能中断执行，也不把可能含敏感信息的异常文本写入日志。
            self.data["capture_error"] = True

    def observe_stream_chunk(self, chunk: dict) -> None:
        structure = chat_response_structure(chunk, offered_names=self.offered_names)
        if not structure["available"]:
            return
        self.stream_chunks += 1
        self.stream_tool_chunks += int(structure["tool_count"] > 0)
        self.stream_named_chunks += int(structure["named_tool_count"] > 0)
        self.stream_finish = structure["finish_reason"] or self.stream_finish

    def finish(self, response: dict | None, meta: dict, *, stream: bool) -> None:
        if stream:
            # response 是 Nexus 拼接后的消息；SDK 层事实来自 observe_stream_chunk。
            self.data["sdk"] = {"available": False, "source": "sdk_stream"}
            self.data["sdk_stream"] = {"chunks": self.stream_chunks, "tool_chunks": self.stream_tool_chunks,
                                       "named_tool_chunks": self.stream_named_chunks, "finish_reason": self.stream_finish}
        else:
            self.data["sdk"] = {**chat_response_structure(response, offered_names=self.offered_names),
                                "source": "sdk_normalized_message"}
        if self.data.get("provider_request_id"):
            meta["provider_request_id"] = self.data["provider_request_id"]
        meta["response_trace"] = self.data


def record_decoded_response_trace(meta: dict, parse_meta: dict) -> None:
    trace = meta.get("response_trace")
    if not isinstance(trace, dict):
        return
    calls = parse_meta.get("tool_calls") or []
    named = sum(1 for call in calls if isinstance(call, dict) and isinstance(call.get("function"), dict)
                and isinstance(call["function"].get("name"), str) and call["function"]["name"].strip())
    trace["decoder"] = {"tool_count": len(calls), "named_tool_count": named}
    provider = trace.get("provider_parsed") or {}
    normalized = trace.get("sdk") or {}
    stream = trace.get("sdk_stream") or {}
    origin = "not_empty" if named else "unobserved"
    if not named:
        if normalized.get("named_tool_count") or stream.get("named_tool_chunks"):
            origin = "nexus_decoder"
        elif provider.get("named_tool_count"):
            origin = "sdk_normalization"
        elif provider.get("available"):
            origin = "provider_parsed_output_empty"
    trace["empty_call_origin"] = origin


class ChatStream:
    """把 SDK 标准 chunk 接入现有流式取消/工具拼接逻辑。"""
    def __init__(self, stream, trace: ChatResponseTrace | None = None):
        self.stream = stream
        self.trace = trace
        self.encoding = "utf-8"

    def iter_lines(self, **_kwargs):
        for chunk in self.stream:
            data = chunk if isinstance(chunk, dict) else chunk.model_dump(exclude_none=True)
            if self.trace is not None:
                self.trace.observe_stream_chunk(data)
            yield "data: " + json.dumps(data, ensure_ascii=False)
        yield "data: [DONE]"

    def close(self):
        close = getattr(self.stream, "close", None)
        if close:
            close()
