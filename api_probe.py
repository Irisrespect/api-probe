#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API 中转站测活工具 (API Probe)  v1.6.0
=====================================================================
单文件、零第三方依赖的 Windows 桌面小工具（Python 3.9+，自带 tkinter 即可运行）

工作流程（一条线走到底）
------------------------
    输入 API 地址 + Key（或从站点下拉选一个已保存的站点）
        ↓
    ① 获取模型列表      只读探测：站点指纹、协议族、真实模型清单
        ↓
    ② 对选中模型发测试  真发一次最小对话：能不能出字、首字多快（流畅度）
        ↓
    ③ 接入 Claude Code  检测 /v1/messages → 映射模型别名 → 写配置 → 端到端验证
    ④ 接入 Codex        检测 /v1/responses 与 /v1/chat/completions → 写 config.toml → 端到端验证

    ③④ 是两个独立页面：一页管 Anthropic 格式，一页管 OpenAI 格式，互不干扰。
    顶部有「▶ 全流程（探测 → 测速 → 接入）」一键跑完；
    两个「一键接入」各有硬门禁：② 生成测试通过 且 对应通道探测通过 才可点。

多站点管理
----------
    顶部「站点」下拉里可以放任意多个中转站，每个站点各自记住 地址 / Key / 默认模型：
      ＋ 添加站点   填名称 + 地址（可用常用预设快速填充）+ Key + 默认模型
      编辑 / 删除   对当前选中的站点操作
      ✎ 自定义（临时）  内置的一格，不参与站点列表，但同样记住你敲进去的内容
    切换站点会自动带出该站的地址与 Key；正在编辑的内容会先存回原来那一格。

Claude Code 接入细节
--------------------
      检测通道       依次试 Authorization: Bearer 与 x-api-key，并补流式测试（CLI 全程走流式）
      模型映射       sonnet/opus/haiku/fable 别名 → 网关真实模型 id（不做映射必 404）
      一键接入       可多选：写 ~/.claude/settings.json（合并 + 自动备份）/
                     生成启动脚本（不落盘 Key）/ 生成隔离配置目录（CLAUDE_CONFIG_DIR）
      端到端验证     在空的隔离配置目录里跑 claude -p PONG，证明整条链路真的通
    前提：CLI 需 2.x；网关必须实现 Anthropic Messages 协议（只提供 OpenAI 协议的站无法直连）。

配置持久化（~/.api_probe.json，config_version 3）
------------------------------------------------
    * 明文保存：站点列表（名称/地址/默认模型）、超时、各开关、窗口大小
    * 加密保存：各站点的 Key 与批量列表 —— 仅在勾选「记住 Key」时写入，
      且用 Windows DPAPI 按当前用户加密（同一台机器的同一个用户才能解开）
    * 旧版（v1/v2）配置会自动迁移：原来那个地址会变成一个名为「默认站点」的站点

功能
----
1. 连通性测试：HTTP 状态、各阶段耗时、错误原文
2. 协议自动识别：OpenAI 兼容 / Anthropic / Gemini，自动选择端点与鉴权头
3. 模型列表：表格化展示、过滤、只看主流、复制、导出
4. 真实生成测试：流式最小对话，测「首字延迟」与流畅度
5. 掺水鉴别：请求模型 vs 返回模型比对、异常低延迟、usage 缺失、模型不在列表等信号
6. 站点指纹：探测 /api/status 与首页 title，识别是否 New API / one-api / Veloera 系面板
7. 批量测试多站（可一键从站点列表导入）+ 导出 CSV / JSON
8. Claude Code 一键接入：通道检测 → 模型映射 → 写配置/启动脚本 → 端到端验证

用法
----
    python api_probe.py                          # 启动图形界面
    python api_probe.py --url URL --key KEY --list-only          # 只做第 ① 步，列出模型
    python api_probe.py --url URL --key KEY --model M            # 指定模型直接做第 ② 步
    python api_probe.py --url URL --key KEY --json               # 一次性两步，JSON 输出
    python api_probe.py --url URL --key KEY --claude-check       # 检测能否接 Claude Code
    python api_probe.py --selftest               # 核心逻辑自检（本地 mock，不访问外网）
    python api_probe.py --uitest                 # 界面 + 站点管理 + 两段式 + 接入页自检

作者说明：本工具只做「客户端主动探测」，不代理、不存储任何请求内容。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import gzip
import json
import os
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

APP_NAME = "API 中转站测活工具"
APP_VERSION = "1.6.0"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) api-probe/1.0"
DEFAULT_TIMEOUT = 20.0
CONFIG_PATH = Path.home() / ".api_probe.json"

# 主流模型（用于「站点都提供哪些主流模型」的命中统计，按归一化名匹配）
MAINSTREAM_MODELS = [
    "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-5", "o3", "o4-mini",
    "claude-opus-4-1", "claude-sonnet-4-5", "claude-3-5-sonnet", "claude-3-7-sonnet",
    "gemini-2-5-pro", "gemini-2-5-flash", "gemini-2-0-flash",
    "deepseek-chat", "deepseek-reasoner", "qwen3", "qwen-max", "glm-4-6", "kimi-k2",
    "grok-4", "llama-3-3-70b",
]

# 常见端点后缀，用于把用户粘贴的完整 URL 还原成 base
KNOWN_SUFFIXES = (
    "/v1/chat/completions", "/chat/completions",
    "/v1/completions", "/v1/responses", "/responses",
    "/v1/messages", "/messages",
    "/v1/embeddings", "/v1/rerank",
    "/v1/models", "/models",
    "/v1beta/models", "/v1beta",
    "/v1",
)
# 注意：不要剥离 "/api/v1" 这类自定义前缀，否则 OpenRouter(https://openrouter.ai/api/v1)
# 会被错误地还原成 https://openrouter.ai，导致请求打到 /v1/models 而 404。

# 中转站/面板特征串（出现在错误信息或响应体里）
PANEL_SIGNATURES = [
    ("new_api_error", "New API"), ("one_api_error", "one-api"),
    ("newapi", "New API"), ("veloera", "Veloera"),
    ("无可用渠道", "New API/one-api 系（无可用渠道）"),
    ("当前分组上游负载已饱和", "New API/one-api 系（分组负载饱和）"),
    ("渠道", "New API/one-api 系"),
    ("令牌", "New API/one-api 系"),
]

VENDOR_PREFIXES = {
    "openai", "anthropic", "google", "meta-llama", "deepseek", "qwen", "x-ai",
    "mistralai", "moonshotai", "z-ai", "minimax", "cohere", "amazon", "microsoft",
    "nvidia", "perplexity", "ai21", "thudm", "01-ai",
}

DATE_SUFFIX_RE = re.compile(r"-(?:20\d{2}-\d{2}-\d{2}|20\d{6}|\d{6})$")


# =====================================================================
# 一、通用小工具
# =====================================================================

def now_ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def get_header(headers: dict, name: str, default: str = "") -> str:
    if not headers:
        return default
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v or default
    return default


def decode_body(raw: bytes, headers: dict | None = None) -> str:
    if not raw:
        return ""
    enc = get_header(headers or {}, "Content-Encoding").lower()
    try:
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            raw = zlib.decompress(raw)
    except Exception:
        pass
    for cs in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(cs)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def safe_json(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    # 有些站点在 JSON 前后夹了多余内容，尝试截取首个 { 到末个 }
    s = text.find("{")
    e = text.rfind("}")
    if 0 <= s < e:
        try:
            return json.loads(text[s:e + 1])
        except Exception:
            return None
    return None


def mask_key(key: str) -> str:
    if not key:
        return "(空)"
    if len(key) <= 10:
        return key[:3] + "***"
    return f"{key[:6]}...{key[-4:]} (len={len(key)})"


# --------------------------- 本机凭据加密（Windows DPAPI） ---------------------------
# 用当前 Windows 用户的凭据加解密，密钥由系统托管：同一台机器的同一个用户才能解开。
# 非 Windows 或调用失败时返回 None，调用方回退为明文并在配置里标记「未加密」。

def dpapi_crypt(data: bytes, encrypt: bool) -> bytes | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        blob_out = DATA_BLOB()
        if encrypt:
            ok = crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                          ctypes.byref(blob_out))
        else:
            ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                            ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def dpapi_available() -> bool:
    return dpapi_crypt(b"probe", True) is not None


def encode_secret(text: str) -> tuple[str, bool]:
    """把敏感文本变成「可写进配置文件的字符串」。返回 (内容, 是否已加密)。"""
    if not text:
        return "", True
    blob = dpapi_crypt(text.encode("utf-8"), True)
    if blob is None:
        return text, False
    return base64.b64encode(blob).decode("ascii"), True


def decode_secret(value: str, encrypted: bool) -> str:
    if not value:
        return ""
    if not encrypted:
        return value
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=False)
    except Exception:
        return ""
    plain = dpapi_crypt(raw, False)
    return plain.decode("utf-8", "replace") if plain is not None else ""


def ssl_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def normalize_base(url: str) -> str:
    """把用户输入的各种写法归一化为 base（不含 /v1 等端点后缀）。"""
    u = (url or "").strip().strip('"').strip("'").strip()
    if not u:
        return ""
    u = u.split("#")[0]
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", u):
        u = "https://" + u
    u = u.split("?")[0].rstrip("/")
    changed = True
    while changed:
        changed = False
        low = u.lower()
        for suf in KNOWN_SUFFIXES:
            if low.endswith(suf) and len(u) > len(suf) + 8:
                u = u[: -len(suf)].rstrip("/")
                changed = True
                break
    return u


def norm_model(name: str) -> str:
    """归一化模型名，用于「请求 vs 返回」比对：去厂商前缀、去日期后缀、统一分隔符。"""
    s = (name or "").strip().lower()
    if s.startswith("models/"):
        s = s[len("models/"):]
    if "/" in s:
        head, _, tail = s.partition("/")
        if head in VENDOR_PREFIXES and tail:
            s = tail
    s = s.split(":")[0]
    s = s.replace(".", "-").replace("_", "-")
    s = re.sub(r"-+", "-", s)
    s = DATE_SUFFIX_RE.sub("", s)
    s = re.sub(r"-latest$", "", s)
    return s


def extract_error_message(text: str, code: int | None = None) -> str:
    """从错误响应体里挖出人能看懂的一句话。"""
    t = (text or "").strip()
    if not t:
        return f"HTTP {code}（空响应体）" if code else "空响应"
    obj = safe_json(t)
    if isinstance(obj, dict):
        msg = None
        for key in ("error", "message", "msg", "detail", "error_msg", "reason"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                msg = v.strip()
                break
            if isinstance(v, dict):
                for k2 in ("message", "msg", "detail", "type"):
                    v2 = v.get(k2)
                    if isinstance(v2, str) and v2.strip():
                        msg = v2.strip()
                        break
                if msg:
                    break
        if not msg and isinstance(obj.get("errors"), list) and obj["errors"]:
            first = obj["errors"][0]
            if isinstance(first, dict):
                msg = str(first.get("message") or first)
            else:
                msg = str(first)
        if msg:
            etype = ""
            err = obj.get("error")
            if isinstance(err, dict) and isinstance(err.get("type"), str):
                etype = err["type"]
            elif isinstance(obj.get("type"), str) and obj.get("type") != "error":
                etype = obj["type"]
            return f"{msg}" + (f"  [{etype}]" if etype and etype not in msg else "")
    return t[:400]


def detect_panel_hint(text: str) -> str:
    low = (text or "").lower()
    for sig, label in PANEL_SIGNATURES:
        if sig in low or sig in (text or ""):
            return label
    return ""


# =====================================================================
# 二、HTTP 核心（GET / POST / 流式 SSE）
# =====================================================================

class HttpResult:
    __slots__ = ("ok", "status", "reason", "headers", "text", "latency_ms", "error", "url")

    def __init__(self, ok, status, reason, headers, text, latency_ms, error, url):
        self.ok = ok
        self.status = status
        self.reason = reason
        self.headers = headers or {}
        self.text = text or ""
        self.latency_ms = latency_ms
        self.error = error or ""
        self.url = url

    @property
    def json(self):
        return safe_json(self.text)

    def __repr__(self):
        return f"<HttpResult {self.status} {self.latency_ms}ms ok={self.ok} err={self.error[:60]!r}>"


def http_request(method: str, url: str, headers: dict | None = None, payload=None,
                 timeout: float = DEFAULT_TIMEOUT, verify: bool = True,
                 read_limit: int = 4_000_000) -> HttpResult:
    hdrs = {k: v for k, v in (headers or {}).items() if v is not None}
    hdrs.setdefault("User-Agent", UA)
    hdrs.setdefault("Accept", "application/json")
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    ctx = ssl_context(verify) if url.lower().startswith("https") else None
    t0 = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read(read_limit)
        except Exception:
            pass
        return HttpResult(False, e.code, str(e.reason), dict(e.headers or {}),
                          decode_body(raw, dict(e.headers or {})), now_ms(t0), "", url)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return HttpResult(False, 0, "", {}, "", now_ms(t0), f"网络错误: {reason}", url)
    except (socket.timeout, TimeoutError):
        return HttpResult(False, 0, "", {}, "", now_ms(t0), f"超时（>{timeout:g}s）", url)
    except Exception as e:  # noqa: BLE001
        return HttpResult(False, 0, "", {}, "", now_ms(t0), f"{type(e).__name__}: {e}", url)
    with resp:
        try:
            raw = resp.read(read_limit)
        except (socket.timeout, TimeoutError):
            return HttpResult(False, resp.status, "", dict(resp.headers or {}), "",
                              now_ms(t0), "读取响应超时", url)
        except Exception as e:  # noqa: BLE001
            return HttpResult(False, resp.status, "", dict(resp.headers or {}), "",
                              now_ms(t0), f"读取失败: {e}", url)
        return HttpResult(True, resp.status, str(resp.reason), dict(resp.headers or {}),
                          decode_body(raw, dict(resp.headers or {})), now_ms(t0), "", url)


class SseCollector:
    """收集一次流式生成的关键指标。"""

    def __init__(self):
        self.status = 0
        self.first_ms: float | None = None
        self.total_ms: float | None = None
        self.model: str = ""
        self.text: str = ""
        self.usage = None
        self.error: str = ""
        self.chunks = 0
        self.done = False
        self.timed_out = False


def stream_generate(url: str, headers: dict, payload: dict, feeder, timeout: float,
                    verify: bool, stop_event: threading.Event | None = None,
                    max_text: int = 64) -> SseCollector:
    """发一次流式请求，用 feeder 解析 SSE，返回 SseCollector。"""
    col = SseCollector()
    hdrs = {k: v for k, v in (headers or {}).items() if v is not None}
    hdrs.setdefault("User-Agent", UA)
    hdrs["Accept"] = "text/event-stream"
    hdrs["Content-Type"] = "application/json"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    ctx = ssl_context(verify) if url.lower().startswith("https") else None
    t0 = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read(200_000)
        except Exception:
            pass
        col.status = e.code
        col.error = extract_error_message(decode_body(raw, dict(e.headers or {})), e.code)
        col.total_ms = now_ms(t0)
        return col
    except urllib.error.URLError as e:
        col.error = f"网络错误: {getattr(e, 'reason', e)}"
        col.total_ms = now_ms(t0)
        return col
    except (socket.timeout, TimeoutError):
        col.error = f"超时（>{timeout:g}s）"
        col.total_ms = now_ms(t0)
        return col
    except Exception as e:  # noqa: BLE001
        col.error = f"{type(e).__name__}: {e}"
        col.total_ms = now_ms(t0)
        return col

    col.status = resp.status
    event = ""
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                col.error = "已手动停止"
                break
            if time.perf_counter() - t0 > timeout:
                col.timed_out = True
                break
            try:
                line = resp.readline()
            except (socket.timeout, TimeoutError):
                col.timed_out = True
                break
            except Exception as e:  # noqa: BLE001
                col.error = f"读取流失败: {e}"
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if not text:
                event = ""
                continue
            if text.startswith(":"):
                continue
            if text.startswith("event:"):
                event = text[6:].strip()
                continue
            if not text.startswith("data:"):
                continue
            chunk = text[5:].strip()
            if chunk == "[DONE]":
                col.done = True
                break
            obj = safe_json(chunk)
            if not isinstance(obj, (dict, list)):
                continue
            col.chunks += 1
            feeder(col, event, obj, t0)
            if col.done or len(col.text) >= max_text or col.error:
                break
    finally:
        try:
            resp.close()
        except Exception:
            pass
    col.total_ms = now_ms(t0)
    return col


# --------------------------- SSE 解析器 ---------------------------

def feed_openai(col: SseCollector, event: str, obj, t0: float) -> None:
    if isinstance(obj, dict):
        if isinstance(obj.get("error"), (dict, str)) and not obj.get("choices"):
            col.error = extract_error_message(json.dumps(obj), None)
            col.done = True
            return
        if not col.model and isinstance(obj.get("model"), str):
            col.model = obj["model"]
        if isinstance(obj.get("usage"), dict):
            col.usage = obj["usage"]
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            delta = ch.get("delta") or ch.get("message") or {}
            if isinstance(delta, dict):
                piece = delta.get("content") or delta.get("reasoning_content") or ""
                if isinstance(piece, str) and piece:
                    if col.first_ms is None:
                        col.first_ms = now_ms(t0)
                    col.text += piece


def feed_anthropic(col: SseCollector, event: str, obj, t0: float) -> None:
    if not isinstance(obj, dict):
        return
    etype = obj.get("type") or event
    if etype == "error" or (isinstance(obj.get("error"), dict) and obj["error"]):
        col.error = extract_error_message(json.dumps(obj), None)
        col.done = True
        return
    if etype == "message_start":
        msg = obj.get("message") or {}
        if isinstance(msg, dict):
            if isinstance(msg.get("model"), str):
                col.model = msg["model"]
            if isinstance(msg.get("usage"), dict):
                col.usage = msg["usage"]
    elif etype == "content_block_delta":
        delta = obj.get("delta") or {}
        piece = delta.get("text") or delta.get("partial_json") or ""
        if isinstance(piece, str) and piece:
            if col.first_ms is None:
                col.first_ms = now_ms(t0)
            col.text += piece
    elif etype == "message_delta":
        if isinstance(obj.get("usage"), dict):
            col.usage = obj["usage"]


def feed_gemini(col: SseCollector, event: str, obj, t0: float) -> None:
    if not isinstance(obj, dict):
        return
    if isinstance(obj.get("error"), dict):
        col.error = extract_error_message(json.dumps(obj), None)
        col.done = True
        return
    if not col.model and isinstance(obj.get("modelVersion"), str):
        col.model = obj["modelVersion"]
    if isinstance(obj.get("usageMetadata"), dict):
        col.usage = obj["usageMetadata"]
    for cand in obj.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        content = cand.get("content") or {}
        for part in (content.get("parts") or []) if isinstance(content, dict) else []:
            piece = part.get("text") if isinstance(part, dict) else None
            if isinstance(piece, str) and piece:
                if col.first_ms is None:
                    col.first_ms = now_ms(t0)
                col.text += piece


# =====================================================================
# 三、协议适配
# =====================================================================

def gemini_models_url(base: str) -> list[str]:
    return [f"{base}/v1beta/models", f"{base}/v1/models"]


def parse_models_openai(obj) -> list[dict]:
    out: list[dict] = []
    if not isinstance(obj, dict):
        return out
    data = obj.get("data")
    if isinstance(data, dict):
        data = data.get("models") or data.get("data") or data.get("list") or []
    if isinstance(data, list):
        for it in data:
            if isinstance(it, str):
                out.append({"id": it, "owner": ""})
            elif isinstance(it, dict):
                mid = it.get("id") or it.get("model") or it.get("name") or it.get("model_name")
                if mid:
                    out.append({
                        "id": str(mid),
                        "owner": str(it.get("owned_by") or it.get("owner") or it.get("provider") or ""),
                    })
    if not out:
        for key in ("models", "model", "result"):
            arr = obj.get(key)
            if isinstance(arr, list):
                for it in arr:
                    if isinstance(it, str):
                        out.append({"id": it, "owner": ""})
                    elif isinstance(it, dict):
                        mid = it.get("id") or it.get("model") or it.get("name")
                        if mid:
                            out.append({"id": str(mid), "owner": str(it.get("owned_by") or "")})
                if out:
                    break
    # 去重保序
    seen, uniq = set(), []
    for m in out:
        if m["id"] not in seen:
            seen.add(m["id"])
            uniq.append(m)
    return uniq


def parse_models_gemini(obj) -> list[dict]:
    out: list[dict] = []
    if not isinstance(obj, dict):
        return out
    for it in obj.get("models") or []:
        if isinstance(it, dict):
            name = str(it.get("name") or it.get("id") or "")
            if name:
                out.append({
                    "id": name.split("/")[-1],
                    "owner": "google",
                    "display": str(it.get("displayName") or ""),
                })
        elif isinstance(it, str):
            out.append({"id": it.split("/")[-1], "owner": "google"})
    return out


PROTOCOLS = {
    "openai": {
        "label": "OpenAI 兼容",
        "models_probes": [
            ("Bearer", lambda base: f"{base}/v1/models"),
            ("Bearer", lambda base: f"{base}/models"),
        ],
        "parse_models": parse_models_openai,
        "feed": feed_openai,
    },
    "anthropic": {
        "label": "Anthropic",
        "models_probes": [
            ("x-api-key", lambda base: f"{base}/v1/models"),
        ],
        "parse_models": parse_models_openai,
        "feed": feed_anthropic,
    },
    "gemini": {
        "label": "Gemini",
        "models_probes": [
            ("x-goog-api-key", lambda base: f"{base}/v1beta/models"),
            ("x-goog-api-key", lambda base: f"{base}/v1/models"),
        ],
        "parse_models": parse_models_gemini,
        "feed": feed_gemini,
    },
}


def auth_headers(protocol: str, key: str, scheme: str) -> dict:
    if not key:
        return {}
    if scheme == "Bearer":
        return {"Authorization": f"Bearer {key}"}
    if scheme == "x-api-key":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if scheme == "x-goog-api-key":
        return {"x-goog-api-key": key}
    return {}


def generation_request(protocol: str, base: str, key: str, model: str, stream: bool):
    """返回 (url, headers, payload) 或 (None, error)。"""
    prompt = "Say OK"
    if protocol == "openai":
        return (f"{base}/v1/chat/completions",
                {"Authorization": f"Bearer {key}"} if key else {},
                {"model": model, "messages": [{"role": "user", "content": prompt}],
                 "max_tokens": 8, "stream": stream})
    if protocol == "anthropic":
        hdrs = {"anthropic-version": "2023-06-01"}
        if key:
            hdrs["x-api-key"] = key
        return (f"{base}/v1/messages", hdrs,
                {"model": model, "max_tokens": 8,
                 "messages": [{"role": "user", "content": prompt}], "stream": stream})
    if protocol == "gemini":
        path = "streamGenerateContent?alt=sse" if stream else "generateContent"
        url = f"{base}/v1beta/models/{urllib.parse.quote(model)}:{path}"
        hdrs = {"x-goog-api-key": key} if key else {}
        return (url, hdrs,
                {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                 "generationConfig": {"maxOutputTokens": 8}})
    return (None, "未知协议")


def parse_nonstream_model_usage(protocol: str, obj) -> tuple[str, object]:
    """从非流式响应里取 (返回模型, usage)。"""
    if not isinstance(obj, dict):
        return "", None
    if protocol == "gemini":
        return str(obj.get("modelVersion") or ""), obj.get("usageMetadata")
    if protocol == "anthropic":
        return str(obj.get("model") or ""), obj.get("usage")
    return str(obj.get("model") or ""), obj.get("usage")


# =====================================================================
# 四、站点指纹
# =====================================================================

def probe_fingerprint(base: str, timeout: float, verify: bool, log=None) -> dict:
    info = {"panel": "", "system_name": "", "version": "", "title": "", "detail": {}}
    r = http_request("GET", f"{base}/api/status", timeout=min(timeout, 12), verify=verify,
                     read_limit=200_000)
    if log:
        log(f"  [指纹] GET {base}/api/status -> {r.status or r.error} {r.latency_ms}ms")
    if r.ok and isinstance(r.json, dict):
        d = r.json.get("data") if isinstance(r.json.get("data"), dict) else r.json
        if isinstance(d, dict):
            ver = str(d.get("version") or d.get("Version") or "")
            name = str(d.get("system_name") or d.get("systemName") or d.get("name") or "")
            if ver or name:
                info["version"], info["system_name"] = ver, name
                info["detail"] = {k: d.get(k) for k in
                                  ("version", "system_name", "quota_per_unit", "display_in_currency",
                                   "enable_online_topup", "enable_online_topup_pay_method",
                                   "price", "footer_html", "chat_link", "docs_link", "start_time")
                                  if k in d}
                if any(k in d for k in ("quota_per_unit", "display_in_currency", "enable_online_topup")):
                    info["panel"] = "New API（含在线支付/额度字段）"
                else:
                    info["panel"] = "one-api / 衍生面板"
    if not info["panel"]:
        root = http_request("GET", base + "/", timeout=min(timeout, 12), verify=verify,
                            read_limit=200_000)
        if log:
            log(f"  [指纹] GET {base}/ -> {root.status or root.error} {root.latency_ms}ms")
        m = re.search(r"<title[^>]*>(.*?)</title>", root.text or "", re.I | re.S)
        if m:
            info["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
        blob = (root.text or "")[:4000]
        hint = detect_panel_hint(blob)
        if hint:
            info["panel"] = hint
        elif "new-api" in blob.lower() or "new api" in blob.lower():
            info["panel"] = "New API（首页特征）"
    return info


# =====================================================================
# 五、主探测流程
# =====================================================================

MAINSTREAM_ORDER = {name: idx for idx, name in enumerate(MAINSTREAM_MODELS)}

# 明显不适合做对话测试的模型（向量/语音/图像/审核）
NON_CHAT_MARKERS = ("embed", "rerank", "tts", "whisper", "audio", "speech", "moderation",
                    "dall-e", "image", "sora", "veo", "flux", "stable-diffusion",
                    "vision-encoder", "codex-mini-latest", "computer-use")
# 变体后缀：batch/free/extended 这类通常不能直接拿来做普通对话
VARIANT_MARKERS = (":batch", ":free", ":extended", ":online", ":thinking", ":nitro",
                   ":floor", "-preview", "-beta", "-exp", "-search", "-online", "-thinking")


def choose_default_model(models: list[dict], requested: str = "") -> str:
    """挑一个最适合做「最小生成测试」的模型。"""
    ids = [m.get("id") for m in models if m.get("id")]
    if not ids:
        return requested or ""
    if requested:
        if requested in ids:
            return requested
        by_norm = {}
        for i in ids:
            by_norm.setdefault(norm_model(i), i)
        return by_norm.get(norm_model(requested), requested)

    scored: list[tuple[float, str]] = []
    for i in ids:
        low = i.lower()
        if any(b in low for b in NON_CHAT_MARKERS):
            continue
        nm = norm_model(i)
        score = 0.0
        if nm in MAINSTREAM_ORDER:
            score += 1000 - MAINSTREAM_ORDER[nm] * 5      # 主流优先，名单越靠前越优先
        if any(v in low for v in VARIANT_MARKERS):
            score -= 300                                  # batch/free 之类靠后
        if ":" not in i:
            score += 20                                   # 无变体后缀的更可能是标准入口
        score -= len(i) * 0.1                             # 名字短的更干净
        scored.append((score, i))
    if not scored:
        scored = [(0.0, i) for i in ids]
    scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    return scored[0][1]


def probe_site(base_input: str, key: str, timeout: float = DEFAULT_TIMEOUT,
               verify: bool = True, log=None,
               stop_event: threading.Event | None = None) -> dict:
    """【第 1 步】站点指纹 + 协议识别 + 拉取模型列表。

    只做「读」操作，不发任何生成请求，拿到列表交给人工挑模型。
    """
    def L(msg):
        if log:
            log(msg)

    res = {
        "input": base_input, "base": "", "protocol": "", "protocol_label": "",
        "models": [], "models_ms": None, "models_status": None, "models_error": "",
        "fingerprint": "", "fingerprint_detail": {},
        "requested_model": "", "used_model": "", "returned_model": "",
        "first_token_ms": None, "gen_ms": None, "stream_ok": False, "gen_error": "",
        "usage": None, "sample_text": "", "http_status": None, "attempts": [],
        "warnings": [], "gen_warnings": [], "auth_error": "",
        "verdict": "", "verdict_level": "error", "ok": False, "ok_site": False,
    }
    base = normalize_base(base_input)
    res["base"] = base
    if not base:
        res["verdict"] = "地址为空或格式不正确"
        return res

    L(f"[1/2] 站点指纹探测: {base}")
    fp = probe_fingerprint(base, timeout, verify, log=L)
    res["fingerprint"] = fp.get("panel") or fp.get("title") or ""
    res["fingerprint_detail"] = fp
    if fp.get("system_name"):
        res["fingerprint"] = f"{fp['panel']} · {fp['system_name']} {fp.get('version','')}".strip()

    # ---- 协议识别 ----
    L(f"[2/2] 识别协议族并拉取模型列表: {base}")
    order = ["openai", "gemini", "anthropic"]
    low_base = base.lower()
    if "generativelanguage" in low_base or "googleapis" in low_base:
        order = ["gemini", "openai", "anthropic"]
    elif key.startswith("sk-ant") or "anthropic" in low_base:
        order = ["anthropic", "openai", "gemini"]

    auth_fail_msg = ""
    for proto in order:
        spec = PROTOCOLS[proto]
        for scheme, urlf in spec["models_probes"]:
            url = urlf(base)
            for extra_q in ([{}] if scheme != "x-goog-api-key" else [{}, {"key": key}]):
                u = url if not extra_q else f"{url}?key={urllib.parse.quote(key)}"
                hdrs = auth_headers(proto, key, scheme)
                r = http_request("GET", u, headers=hdrs, timeout=timeout, verify=verify)
                attempt = {"protocol": proto, "url": u, "status": r.status,
                           "ms": r.latency_ms, "error": r.error}
                L(f"  [{spec['label']}] GET {u} -> {r.status or r.error} {r.latency_ms}ms")
                if r.ok:
                    models = spec["parse_models"](r.json)
                    if models or isinstance(r.json, (dict, list)):
                        res["protocol"] = proto
                        res["protocol_label"] = spec["label"]
                        res["models"] = models
                        res["models_ms"] = r.latency_ms
                        res["models_status"] = r.status
                        res["http_status"] = r.status
                        attempt["models"] = len(models)
                        res["attempts"].append(attempt)
                        break
                    attempt["error"] = "响应不是可解析的模型列表"
                else:
                    attempt["error"] = r.error or extract_error_message(r.text, r.status)
                    if r.status in (401, 403):
                        auth_fail_msg = attempt["error"]
                        hint = detect_panel_hint(r.text)
                        if hint and not res["fingerprint"]:
                            res["fingerprint"] = hint
                res["attempts"].append(attempt)
            if res["protocol"]:
                break
        if res["protocol"]:
            break

    if not res["protocol"]:
        # 模型端点全挂：仍按 OpenAI 兼容处理，让用户手工输入模型名再测
        L("  /v1/models 全部不可用；仍按 OpenAI 兼容处理，可手工输入模型名再测试")
        res["protocol"] = "openai"
        res["protocol_label"] = "OpenAI 兼容（推测）"
        res["models_error"] = auth_fail_msg or "所有模型列表端点均失败"

    res["model_count"] = len(res["models"])
    L(f"  -> 协议={res['protocol_label']} 模型数={res['model_count']}")
    if stop_event is not None and stop_event.is_set():
        res["verdict"] = "已手动停止"
        return res
    _finalize_site(res)
    return res


def probe_generate(res: dict, key: str, model: str, timeout: float = DEFAULT_TIMEOUT,
                   verify: bool = True, deep: bool = True, log=None,
                   stop_event: threading.Event | None = None) -> dict:
    """【第 2 步】用人工选定的模型发一次最小生成请求：测首字延迟 + 掺水/降级鉴别。

    就地更新并返回第 1 步的 res（可以反复换模型重测，不会累积旧结论）。
    """
    def L(msg):
        if log:
            log(msg)

    base = res.get("base") or ""
    used = (model or "").strip() or choose_default_model(res.get("models") or [])
    res["requested_model"] = (model or "").strip()
    res["used_model"] = used
    res["returned_model"] = ""
    res["first_token_ms"] = None
    res["gen_ms"] = None
    res["stream_ok"] = False
    res["gen_error"] = ""
    res["usage"] = None
    res["sample_text"] = ""
    res["gen_warnings"] = []
    res["ok"] = False
    if stop_event is not None and stop_event.is_set():
        res["verdict"] = "已手动停止"
        return res
    if not base or not used:
        res["verdict"] = "缺少地址或模型，无法做生成测试"
        res["verdict_level"] = "warn"
        return res

    L(f"[1/2] 生成测试（流式）: 协议={res.get('protocol_label')} 请求模型 = {used}")
    url, hdrs, payload = generation_request(res["protocol"], base, key, used, stream=True)
    col = stream_generate(url, hdrs, payload, PROTOCOLS[res["protocol"]]["feed"],
                          timeout=timeout, verify=verify, stop_event=stop_event)
    res["gen_ms"] = col.total_ms
    res["http_status"] = col.status or res["http_status"]
    attempted_alt = False
    if col.error and re.search(r"max_tokens|max_completion_tokens|unsupported parameter",
                               col.error, re.I) and res["protocol"] == "openai":
        attempted_alt = True
        payload2 = dict(payload)
        payload2.pop("max_tokens", None)
        payload2["max_completion_tokens"] = 8
        L("  该模型不接受 max_tokens，改用 max_completion_tokens 重试")
        col = stream_generate(url, hdrs, payload2, feed_openai, timeout=timeout,
                              verify=verify, stop_event=stop_event)
        res["gen_ms"] = col.total_ms
    if col.first_ms is not None:
        res["first_token_ms"] = col.first_ms
        res["stream_ok"] = True
    res["returned_model"] = col.model
    res["sample_text"] = col.text[:120]
    if col.usage:
        res["usage"] = col.usage
    if col.error:
        res["gen_error"] = col.error

    # 流式失败 -> 非流式重试（部分站点/模型不支持 stream；但 401/403 是定论，不必重试）
    if not res["stream_ok"] and col.status not in (401, 403) and \
            not (stop_event is not None and stop_event.is_set()):
        L("  流式未拿到内容，改用非流式重试一次")
        url2, hdrs2, payload2 = generation_request(res["protocol"], base, key, used, stream=False)
        r2 = http_request("POST", url2, headers=hdrs2, payload=payload2, timeout=timeout,
                          verify=verify)
        res["http_status"] = r2.status or res["http_status"]
        L(f"  POST {url2} -> {r2.status or r2.error} {r2.latency_ms}ms")
        if r2.ok:
            obj = r2.json
            rm, usage = parse_nonstream_model_usage(res["protocol"], obj)
            if rm:
                res["returned_model"] = res["returned_model"] or rm
            if usage:
                res["usage"] = usage
            text = ""
            if isinstance(obj, dict):
                if res["protocol"] == "openai":
                    chs = obj.get("choices") or []
                    if chs and isinstance(chs[0], dict):
                        text = str((chs[0].get("message") or {}).get("content") or "")
                elif res["protocol"] == "gemini":
                    for c in obj.get("candidates") or []:
                        for p in ((c.get("content") or {}).get("parts") or []):
                            text += str(p.get("text") or "")
                elif res["protocol"] == "anthropic":
                    for c in obj.get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text += str(c.get("text") or "")
            if text:
                res["stream_ok"] = True
                res["sample_text"] = res["sample_text"] or text[:120]
                res["gen_warnings"].append(("info", "该站/该模型不支持流式输出，非流式可用"))
            elif not res["gen_error"]:
                res["gen_error"] = "响应成功但内容为空"
        else:
            res["gen_error"] = extract_error_message(r2.text, r2.status) or r2.error

    # 深度检测：补一次非流式拿 usage（校验计费统计是否真实）
    if deep and res["stream_ok"] and not res["usage"] and \
            not (stop_event is not None and stop_event.is_set()):
        u3, h3, p3 = generation_request(res["protocol"], base, key, used, stream=False)
        if res["protocol"] == "openai":
            p3["max_tokens"] = 1
        r3 = http_request("POST", u3, headers=h3, payload=p3, timeout=timeout, verify=verify)
        L(f"[2/2] 深度检测（非流式取 usage）: POST {u3} -> {r3.status or r3.error} {r3.latency_ms}ms")
        if r3.ok:
            rm, usage = parse_nonstream_model_usage(res["protocol"], r3.json)
            if rm and not res["returned_model"]:
                res["returned_model"] = rm
            if usage:
                res["usage"] = usage

    _finalize_generation(res, attempted_alt)
    return res


def probe(base_input: str, key: str, model: str = "", timeout: float = DEFAULT_TIMEOUT,
          verify: bool = True, deep: bool = True, log=None,
          stop_event: threading.Event | None = None) -> dict:
    """一步到位版本（批量测试 / 命令行用）：拉列表 → 自动或按指定挑模型 → 生成测试。

    图形界面走的是 probe_site + probe_generate 两段式，中间留给人工挑模型。
    """
    res = probe_site(base_input, key, timeout=timeout, verify=verify, log=log,
                     stop_event=stop_event)
    if not res.get("base") or (stop_event is not None and stop_event.is_set()):
        return res
    return probe_generate(res, key, model or "", timeout=timeout, verify=verify, deep=deep,
                          log=log, stop_event=stop_event)


def _finalize_site(res: dict) -> None:
    """第 1 步的结论：站点是否可达、协议族、模型列表、指纹。"""
    w = res.setdefault("warnings", [])
    w.clear()
    models = res.get("models") or []
    auth_err = ""
    for a in res.get("attempts") or []:
        if a.get("status") in (401, 403):
            auth_err = a.get("error") or f"HTTP {a['status']}"
            break
    res["auth_error"] = auth_err

    if models:
        res["ok_site"] = True
        w.append(("ok", f"取到 {len(models)} 个模型（{res.get('models_ms')} ms）"))
    elif res.get("protocol"):
        w.append(("warn", res.get("models_error") or "未取到模型列表（部分站点禁用了 /v1/models）"))
    if res.get("fingerprint"):
        w.append(("info", f"站点指纹：{res['fingerprint']}"))
    if models:
        fams = []
        for fam, keys in (("OpenAI", ("gpt", "o1", "o3", "o4")),
                          ("Anthropic", ("claude",)),
                          ("Google", ("gemini",)),
                          ("DeepSeek", ("deepseek",)),
                          ("Qwen", ("qwen",)),
                          ("GLM", ("glm",)),
                          ("Kimi", ("kimi", "moonshot"))):
            if any(any(k in m["id"].lower() for k in keys) for m in models):
                fams.append(fam)
        if len(fams) >= 3:
            w.append(("info", f"聚合站：模型列表覆盖 {'/'.join(fams)} 等多家厂商"))
    if auth_err:
        w.append(("error", f"接口鉴权报错：{auth_err}"))

    if res["ok_site"]:
        res["verdict"] = (f"第 1 步完成：{res.get('protocol_label')}，共 {len(models)} 个模型"
                          "　→ 请在「模型列表」中挑一个模型，再做第 2 步生成测试")
        res["verdict_level"] = "ok"
    elif auth_err:
        res["verdict"] = f"第 1 步失败：{auth_err}"
        res["verdict_level"] = "error"
    elif res.get("protocol"):
        res["verdict"] = "第 1 步完成，但未取到模型列表：可手工输入模型名后做第 2 步"
        res["verdict_level"] = "warn"
    else:
        res["verdict"] = "无法连接或 Key 无效"
        res["verdict_level"] = "error"


def _finalize_generation(res: dict, attempted_alt: bool = False) -> None:
    """第 2 步的结论：可用性判定 + 掺水/降级信号（可反复换模型重测）。"""
    w = res.setdefault("gen_warnings", [])
    w.clear()
    req = res.get("used_model") or res.get("requested_model") or ""
    got = res.get("returned_model") or ""
    models = res.get("models") or []

    if res.get("stream_ok"):
        res["ok"] = True
    elif res.get("gen_error"):
        res["ok"] = False

    # 1) 返回模型 vs 请求模型
    if req and got and norm_model(req) != norm_model(got):
        w.append(("risk", f"返回模型与请求不一致：请求 {req} → 返回 {got}"
                          "（可能是站点做了别名映射，也可能被替换/降级，建议用官方对比验证）"))
    elif req and got:
        w.append(("ok", f"返回模型与请求一致：{got}"))

    # 2) 首字延迟异常低
    ft = res.get("first_token_ms")
    if ft is not None and ft < 250:
        w.append(("warn", f"首字延迟异常低（{ft:.0f}ms），可能命中缓存或并非真实推理"))

    # 3) 请求的模型是否在站点模型列表里
    if req and models:
        ids = {m["id"] for m in models}
        norms = {norm_model(i) for i in ids}
        if req not in ids and norm_model(req) not in norms and not attempted_alt:
            w.append(("warn", f"模型 {req} 不在站点返回的模型列表中（站点列表 {len(models)} 个）"))

    # 4) usage 校验
    usage = res.get("usage")
    if res.get("stream_ok"):
        if not isinstance(usage, dict):
            w.append(("warn", "响应未返回 usage（token 用量），无法核验计费统计"))
        else:
            tot = usage.get("total_tokens") or usage.get("totalTokenCount")
            if not tot:
                w.append(("warn", f"usage 中 token 数为 0 或缺失：{json.dumps(usage, ensure_ascii=False)[:120]}"))

    if res.get("gen_error"):
        hint = detect_panel_hint(res["gen_error"])
        w.append(("error", f"生成失败：{res['gen_error']}" + (f"（{hint}）" if hint else "")))
    if attempted_alt:
        w.append(("info", "该模型要求 max_completion_tokens，已自动适配"))

    # 结论
    if res["ok"]:
        bits = [res.get("protocol_label") or "未知协议", f"模型 {req}"]
        if ft is not None:
            bits.append(f"首字 {ft:.0f}ms")
        risky = [x for lvl, x in w if lvl == "risk"]
        res["verdict"] = ("可用（" + "，".join(bits) + "）" +
                          ("　⚠ " + risky[0] if risky else ""))
        res["verdict_level"] = "risk" if risky else "ok"
    elif res.get("gen_error"):
        res["verdict"] = f"该模型不可用：{res['gen_error']}"
        res["verdict_level"] = "error"
    else:
        res["verdict"] = "生成测试未完成"
        res["verdict_level"] = "warn"


# =====================================================================
# 六、批量解析 / 导出
# =====================================================================

def parse_batch_lines(text: str) -> list[dict]:
    """每行：URL | KEY | [MODEL]，分隔符支持 | 、制表符、逗号、两个以上空格。"""
    items = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p for p in re.split(r"\s*[|\t,]\s*|\s{2,}", line) if p != ""]
        if not parts:
            continue
        if len(parts) == 1:
            items.append({"url": parts[0], "key": "", "model": ""})
        elif len(parts) == 2:
            items.append({"url": parts[0], "key": parts[1], "model": ""})
        else:
            items.append({"url": parts[0], "key": parts[1], "model": parts[2]})
    return items


def result_to_row(res: dict) -> dict:
    return {
        "输入地址": res.get("input", ""),
        "归一化Base": res.get("base", ""),
        "协议": res.get("protocol_label", ""),
        "HTTP状态": res.get("http_status", ""),
        "可用": "是" if res.get("ok") else "否",
        "模型数": res.get("model_count", len(res.get("models") or [])),
        "模型列表耗时ms": res.get("models_ms", ""),
        "请求模型": res.get("used_model", ""),
        "返回模型": res.get("returned_model", ""),
        "首字延迟ms": round(res["first_token_ms"], 1) if res.get("first_token_ms") is not None else "",
        "生成总耗时ms": res.get("gen_ms", ""),
        "站点指纹": res.get("fingerprint", ""),
        "结论": res.get("verdict", ""),
        "提示": " ; ".join(x for lvl, x in
                          (res.get("warnings", []) + res.get("gen_warnings", []))
                          if lvl in ("risk", "warn", "error")),
    }


def export_csv(rows: list[dict], path: str) -> None:
    import csv
    if not rows:
        return
    fields = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        for r in rows:
            wr.writerow({k: r.get(k, "") for k in fields})


def export_json(payload, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# =====================================================================
# 七、Claude Code CLI 接入
# =====================================================================
# Claude Code CLI（2.x）可以直接接第三方网关，但有两条硬性前提：
#   1. 网关必须实现 Anthropic Messages 协议：POST <BASE>/v1/messages
#      只提供 OpenAI 协议（/v1/chat/completions）的站无法直连，需要协议转换层
#   2. 模型名必须按别名映射：CLI 内部用 sonnet/opus/haiku/fable 别名，不映射就会拿官方 id
#      去打你的网关，报 404 model_not_found（认证其实是通的）
# 本节负责：通道检测 → 生成配置 → 合并写入（带备份）/ 生成启动脚本 / 生成隔离配置目录 → 端到端验证

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CLAUDE_CLI_CANDIDATES = (
    Path.home() / ".local" / "bin" / "claude.exe",
    Path.home() / ".local" / "bin" / "claude",
    Path(os.environ.get("APPDATA") or "") / "npm" / "claude.cmd",
    Path(os.environ.get("APPDATA") or "") / "npm" / "claude",
)
# 由本工具管理的键：写入前先清掉这几个，避免 ANTHROPIC_AUTH_TOKEN 与 ANTHROPIC_API_KEY 同时存在
CLAUDE_MANAGED_ENV_KEYS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
)
CLAUDE_ALIASES = ("sonnet", "opus", "haiku", "fable")
CLAUDE_MSG_PATH = "/v1/messages"
CLAUDE_PING_PROMPT = "Reply with exactly: PONG"
# 本工具生成的启动脚本 / 隔离配置目录都放这里，便于查找
CLAUDE_OUTPUT_DIR = Path.home() / ".api_probe"


def claude_auth_headers(auth_style: str, key: str) -> dict:
    """bearer → Authorization: Bearer xxx；x-api-key → x-api-key: xxx"""
    h = {"anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    if key:
        if auth_style == "x-api-key":
            h["x-api-key"] = key
        else:
            h["Authorization"] = f"Bearer {key}"
    return h


def claude_base_for_gateway(base: str) -> str:
    """ANTHROPIC_BASE_URL 只写到前缀，CLI 自己会拼 /v1/messages。

    这里直接复用 normalize_base：它会剥掉 /v1、/v1beta、/v1/messages 等后缀，
    也会给没写协议的地址补上 https://。所以用户把完整端点粘进来也不会拼成 /v1/v1/messages。
    """
    return normalize_base(base)


def claude_ping_payload(model: str, stream: bool = False) -> dict:
    return {"model": model, "max_tokens": 16,
            "messages": [{"role": "user", "content": CLAUDE_PING_PROMPT}],
            "stream": bool(stream)}


def anthropic_reply_text(obj) -> str:
    """从 Anthropic Messages 响应里取纯文本。"""
    if not isinstance(obj, dict):
        return ""
    out = []
    for blk in obj.get("content") or []:
        if isinstance(blk, dict) and blk.get("type") == "text":
            out.append(str(blk.get("text") or ""))
        elif isinstance(blk, str):
            out.append(blk)
    return "".join(out).strip()


def check_anthropic_channel(base: str, key: str, model: str = "",
                            timeout: float = DEFAULT_TIMEOUT, verify: bool = True,
                            log=None, stop_event: threading.Event | None = None) -> dict:
    """检测网关是否实现 Anthropic Messages 协议（Claude Code 直连的前提）。

    会依次尝试两种鉴权头：先 Authorization: Bearer，失败再 x-api-key；
    成功后补一次流式测试（CLI 全程走流式，只测非流式不够）。
    """
    def L(msg):
        if log:
            log(msg)

    res = {"ok": False, "auth_style": "", "status": 0, "error": "", "model_used": model or "",
           "latency_ms": None, "stream_ok": False, "first_ms": None, "reply": "",
           "model_not_found": False, "path_issue": False, "tried": [],
           "url": "", "verdict": "", "verdict_level": "error"}
    base = claude_base_for_gateway(base)
    if not base:
        res["verdict"] = "地址为空"
        return res
    url = base + CLAUDE_MSG_PATH
    res["url"] = url
    payload = claude_ping_payload(res["model_used"])
    # 探测阶段给每次尝试设上限：慢站/挂死的站不至于把整个检测拖成几分钟
    # （流式测试仍用完整 timeout）
    probe_timeout = max(5.0, min(timeout, 20.0))

    for style in ("bearer", "x-api-key"):
        if stop_event is not None and stop_event.is_set():
            res["verdict"] = "已手动停止"
            return res
        r = http_request("POST", url, headers=claude_auth_headers(style, key), payload=payload,
                         timeout=probe_timeout, verify=verify)
        # 网络层错误（超时/连不上）比空响应体更有信息量，优先用它
        msg = "" if r.ok else (r.error or extract_error_message(r.text, r.status))
        info = {"auth_style": style, "status": r.status, "ms": r.latency_ms, "error": msg}
        L(f"  [Anthropic] POST {url}（{style}）→ {r.status or r.error} {r.latency_ms}ms")
        res["tried"].append(info)
        if r.ok:
            res.update(ok=True, auth_style=style, status=r.status, latency_ms=r.latency_ms,
                       reply=anthropic_reply_text(r.json)[:240], error="")
            info["ok"] = True
            break
        low = ((msg or "") + " " + (r.text or "")[:600]).lower()
        if r.status == 404 and ("model" in low or "not supported" in low):
            # 404 且提到模型 → 认证已通过，只是模型 id 不在该 key 的可用集合里
            res.update(model_not_found=True, auth_style=style, status=404, error=msg,
                       latency_ms=r.latency_ms)
            break
        if r.status == 404:
            res.update(path_issue=True, status=404, error=msg, latency_ms=r.latency_ms)
            break
        if r.status in (401, 403):
            res.update(status=r.status, error=msg)
            continue          # 换个头再试
        res.update(status=r.status, error=msg, latency_ms=r.latency_ms)
        break

    # 流式测试（需要已知可用模型）。实测发现有些网关会偶发断连（RemoteDisconnected），
    # 但 CLI 本身仍能跑通，所以探两次再下结论，避免误判。
    if res["ok"] and res["model_used"] and not (stop_event is not None and stop_event.is_set()):
        stream_timeout = max(10.0, min(timeout, 30.0))
        for attempt in (1, 2):
            L(f"  流式测试 POST {url}（stream=true，第 {attempt} 次）")
            col = stream_generate(url, claude_auth_headers(res["auth_style"], key),
                                  claude_ping_payload(res["model_used"], stream=True),
                                  feed_anthropic, timeout=stream_timeout, verify=verify,
                                  stop_event=stop_event)
            res["stream_ok"] = col.first_ms is not None
            res["first_ms"] = col.first_ms
            if col.text:
                res["reply"] = res["reply"] or col.text[:240]
            if res["stream_ok"]:
                L(f"  流式通过：首字 {col.first_ms:.0f}ms")
                break
            res["stream_error"] = col.error or "未收到流式数据"
            L(f"  第 {attempt} 次流式未通过：{res['stream_error']}")
            if attempt == 1 and not (stop_event is not None and stop_event.is_set()):
                time.sleep(1.0)

    # 结论
    if res["ok"]:
        if res["stream_ok"]:
            res["verdict"] = (f"通道可用：认证方式 {res['auth_style']}，"
                              f"非流式 {res['latency_ms']:.0f}ms / 首字 {res['first_ms']:.0f}ms")
            res["verdict_level"] = "ok"
        else:
            res["verdict"] = (f"非流式可用（认证 {res['auth_style']}），但两次流式探测都没拿到数据"
                              f"（{res.get('stream_error') or '未知原因'}）"
                              "—— 可能是站点限流/瞬时抖动，也可能 SSE 未完整实现；"
                              "CLI 未必不能用，建议直接跑一次端到端验证")
            res["verdict_level"] = "warn"
    elif res["model_not_found"]:
        res["verdict"] = (f"认证通过，但模型「{res['model_used']}」在该网关不可用"
                          " → 需要做模型映射（这一步是 90% 失败的原因）")
        res["verdict_level"] = "warn"
    elif res["path_issue"]:
        res["verdict"] = ("该地址没有 /v1/messages：要么 ANTHROPIC_BASE_URL 写法不对，"
                          "要么这个站只提供 OpenAI 协议，Claude Code 无法直连")
        res["verdict_level"] = "error"
    else:
        statuses = [t.get("status") for t in res.get("tried", [])]
        if statuses and all(s in (401, 403) for s in statuses):
            # 返回 401/403 而不是 404 → 路由是存在的，协议多半支持，问题在 Key
            res["verdict"] = (f"端点存在（返回 {statuses[0]} 而非 404，说明 /v1/messages 是实现了的），"
                              f"但鉴权失败：{res['error']} —— 换一个有效的 Key 再测")
            res["verdict_level"] = "warn"
        else:
            res["verdict"] = f"通道不可用：{res['error'] or '未知错误'}"
            res["verdict_level"] = "error"
    return res


def evaluate_fluency(first_ms, stream_ok: bool) -> tuple[str, str]:
    """按首字延迟给个「流畅度」判断，返回 (level, 文案)。"""
    if not stream_ok or first_ms is None:
        return "warn", "未拿到流式首字，无法判断流畅度"
    if first_ms < 1000:
        return "ok", f"流畅（首字 {first_ms:.0f}ms）"
    if first_ms < 3000:
        return "ok", f"良好（首字 {first_ms:.0f}ms）"
    if first_ms < 8000:
        return "warn", f"偏慢（首字 {first_ms:.0f}ms）"
    return "warn", f"很慢（首字 {first_ms:.0f}ms），日常用会明显卡顿"


def suggest_claude_model_map(models: list[dict], fallback: str = "") -> dict:
    """从站点模型列表猜 sonnet/opus/haiku/fable 该映射到哪个真实 id。"""
    ids = [str(m.get("id") or "") for m in (models or []) if m.get("id")]
    out: dict = {}
    for alias in CLAUDE_ALIASES:
        cands = [i for i in ids if alias in i.lower()]
        if cands:
            cands.sort(key=lambda s: (len(s), s))     # 同族优先取最短（通常是没有日期后缀的主名）
            out[alias] = cands[0]
    default = out.get("sonnet") or fallback or (choose_default_model(models) if models else "")
    if default:
        out["default"] = default
    return out


def run_full_pipeline(base_input: str, key: str, model: str = "",
                      timeout: float = DEFAULT_TIMEOUT, verify: bool = True, deep: bool = True,
                      log=None, stop_event: threading.Event | None = None) -> dict:
    """一条龙走完用户的四步流程：

        ① 探测模型列表
        ② 生成测试（能不能真的跑、跑得流不流畅）
        ③ 检测两条 CLI 通道：Anthropic /v1/messages（Claude Code）与
           OpenAI /v1/responses、/v1/chat/completions（Codex）
        ④ 给出模型映射建议（claude 别名 + Codex 默认模型）

    返回 {"site","gen","channel","model_map","steps","fluency","ok","verdict",...}。
    不做任何写入动作 —— 写配置由 GUI 的「一键接入」显式触发。
    """
    def L(msg):
        if log:
            log(msg)

    out = {"site": {}, "gen": {}, "channel": {}, "channel_openai": {}, "model_map": {},
           "codex_model": "", "used_model": "", "fluency_level": "", "fluency": "",
           "ok": False, "stopped": False, "steps": [], "verdict": ""}

    def step(no, text, ok=None, detail=""):
        out["steps"].append({"no": no, "text": text, "ok": ok, "detail": detail})
        L(f"[{no}/4] {text}" + (f" → {detail}" if detail else ""))

    L("=" * 62)
    L(f"[1/4] 探测模型列表：{base_input}")
    site = probe_site(base_input, key, timeout=timeout, verify=verify, log=log,
                      stop_event=stop_event)
    out["site"] = site
    base = site.get("base") or ""
    if not base:
        out["verdict"] = site.get("verdict") or "地址无效"
        step(1, "探测模型列表", False, out["verdict"])
        return out
    if stop_event is not None and stop_event.is_set():
        out.update(stopped=True, verdict="已手动停止")
        return out
    step(1, "探测模型列表", bool(site.get("ok_site")),
         f"{site.get('protocol_label') or '-'}，{site.get('model_count', 0)} 个模型")

    used = (model or "").strip() or choose_default_model(site.get("models") or [])
    out["used_model"] = used
    if used:
        L(f"[2/4] 生成测试（模型 {used}）")
        gen = probe_generate(site, key, used, timeout=timeout, verify=verify, deep=deep,
                             log=log, stop_event=stop_event)
        out["gen"] = gen
        lvl, txt = evaluate_fluency(gen.get("first_token_ms"), bool(gen.get("stream_ok")))
        out["fluency_level"], out["fluency"] = lvl, txt
        step(2, f"生成测试（模型 {used}）", bool(gen.get("ok")), txt)
    else:
        step(2, "生成测试", False, "没有可用模型，请在「指定模型」里手写一个再试")

    if stop_event is not None and stop_event.is_set():
        out.update(stopped=True, verdict="已手动停止")
        return out

    L("[3/4] 检测 CLI 通道：Anthropic(/v1/messages) 与 OpenAI(/v1/responses、/v1/chat/completions)")
    ch = check_anthropic_channel(base, key, used, timeout=timeout, verify=verify, log=log,
                                 stop_event=stop_event)
    out["channel"] = ch
    och = check_openai_channel(base, key, used, timeout=timeout, verify=verify, log=log,
                               stop_event=stop_event)
    out["channel_openai"] = och
    a_txt = (f"Anthropic 认证 {ch['auth_style']}" if ch.get("ok") else
             ("Anthropic 认证通过(模型不可用)" if ch.get("model_not_found") else "Anthropic 不可用"))
    o_txt = (f"OpenAI wire_api={och['wire_api']}" if och.get("ok") else "OpenAI 不可用")
    step(3, "检测 CLI 通道（Anthropic + OpenAI）",
         bool(ch.get("ok") or ch.get("model_not_found") or och.get("ok")),
         f"{a_txt}；{o_txt}")

    mm = suggest_claude_model_map(site.get("models") or [], fallback=used)
    out["model_map"] = mm
    out["codex_model"] = suggest_codex_model(site.get("models") or [], fallback=used)
    step(4, "生成模型映射建议", bool(mm) or bool(out["codex_model"]),
         "、".join(f"{k}→{v}" for k, v in mm.items()) or "没匹配到 claude 系模型")

    gen_ok = bool(out["gen"].get("ok"))
    a_ok = bool(ch.get("ok")) or bool(ch.get("model_not_found"))
    o_ok = bool(och.get("ok"))
    out["ok"] = gen_ok and (a_ok or o_ok)
    bits = ["模型可用" if gen_ok else "生成测试未通过"]
    bits.append("Claude Code 通道可用" if ch.get("ok")
                else ("Claude Code 通道认证通过（映射模型后可用）" if ch.get("model_not_found")
                      else "Claude Code 通道不可用"))
    bits.append(f"Codex 通道可用（wire_api={och['wire_api']}）" if o_ok else "Codex 通道不可用")
    out["verdict"] = ("全流程" + ("通过：" if out["ok"] else "未完全通过：") + "；".join(bits)
                      + (f"；{out['fluency']}" if out["fluency"] else ""))
    L(f"流程结论：{out['verdict']}")
    return out


def build_claude_env(base: str, key: str, auth_style: str, model_map: dict | None = None,
                     model_default: str = "") -> dict:
    """生成要写进 CLI 的 ANTHROPIC_* 环境变量（key 只出其一，避免两个头打架）。"""
    env = {"ANTHROPIC_BASE_URL": claude_base_for_gateway(base)}
    if key:
        if auth_style == "x-api-key":
            env["ANTHROPIC_API_KEY"] = key
        else:
            env["ANTHROPIC_AUTH_TOKEN"] = key
    mm = dict(model_map or {})
    default = (model_default or mm.pop("default", "") or mm.get("sonnet") or "").strip()
    if default:
        env["ANTHROPIC_MODEL"] = default
    for alias in CLAUDE_ALIASES:
        v = str(mm.get(alias) or "").strip()
        if v:
            env[f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL"] = v
    return env


def merge_claude_settings(existing, env: dict) -> dict:
    """把 env 合并进 settings.json：只替换本工具管理的键，其它内容原样保留。"""
    out = dict(existing) if isinstance(existing, dict) else {}
    block = dict(out.get("env") or {}) if isinstance(out.get("env"), dict) else {}
    for k in CLAUDE_MANAGED_ENV_KEYS:
        block.pop(k, None)                    # 先清掉旧的，保证不会 AUTH_TOKEN/API_KEY 并存
    block.update({k: v for k, v in (env or {}).items()
                  if k in CLAUDE_MANAGED_ENV_KEYS and v})
    out["env"] = block
    return out


def read_claude_settings(path: str | Path | None = None) -> dict:
    p = Path(path) if path else CLAUDE_SETTINGS_PATH
    try:
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def install_claude_settings(env: dict, path: str | Path | None = None,
                            backup: bool = True) -> dict:
    """合并写入 settings.json。会先备份；现有文件解析不了就中止，绝不覆盖。"""
    p = Path(path) if path else CLAUDE_SETTINGS_PATH
    out = {"ok": False, "path": str(p), "backup": "", "error": "", "settings": {}}
    existing: dict = {}
    if p.exists():
        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                raise ValueError("顶层不是 JSON 对象")
            existing = data
        except Exception as e:  # noqa: BLE001
            out["error"] = f"现有配置无法解析（{e}），已中止以免覆盖：{p}"
            return out
        if backup:
            ts = time.strftime("%Y%m%d-%H%M%S")
            bak = p.with_name(p.name + f".bak-apiprobe-{ts}")
            try:
                shutil.copy2(p, bak)
                out["backup"] = str(bak)
            except Exception as e:  # noqa: BLE001
                out["error"] = f"备份失败，已中止：{e}"
                return out
    merged = merge_claude_settings(existing, env)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        out["error"] = f"写入失败：{e}"
        return out
    out["ok"] = True
    out["settings"] = merged
    return out


def slugify(name: str, maxlen: int = 24) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip()).strip("-").lower()
    return s[:maxlen] or "site"


def non_ascii_env_key(env: dict) -> str:
    """返回第一个含非 ASCII 的键名（.bat 只能装 ASCII，否则 cmd 会读乱）。"""
    for k, v in (env or {}).items():
        if not str(v).isascii():
            return str(k)
    return ""


def render_launcher_bat(env: dict, config_dir: str = "") -> str:
    """生成启动脚本内容（CRLF + 纯 ASCII，理由见 README 的 .bat 说明）。"""
    lines = ["@echo off",
             "rem Claude Code launcher generated by api_probe - keep this file ASCII-only,",
             "rem because cmd.exe parses .bat with the OEM code page."]
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
              "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
              "ANTHROPIC_DEFAULT_FABLE_MODEL"):
        if env.get(k):
            lines.append(f'set "{k}={env[k]}"')
    if config_dir:
        lines.append(f'set "CLAUDE_CONFIG_DIR={config_dir}"')
    lines.append("claude %*")
    return "\r\n".join(lines) + "\r\n"


def write_claude_launcher(env: dict, path: str | Path, config_dir: str = "") -> dict:
    out = {"ok": False, "path": str(path), "error": ""}
    bad = non_ascii_env_key(env)
    if bad:
        out["error"] = (f"{bad} 的值含非 ASCII 字符，无法安全写进 .bat"
                        "（cmd 按 OEM 码页读批处理）——请改用写入 settings.json 的方式")
        return out
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render_launcher_bat(env, config_dir), encoding="ascii", newline="")
    except Exception as e:  # noqa: BLE001
        out["error"] = f"写入失败：{e}"
        return out
    out["ok"] = True
    return out


def write_isolated_claude_config(env: dict, config_dir: str | Path) -> dict:
    """生成一个独立配置目录（配合 CLAUDE_CONFIG_DIR 用），与正式 ~/.claude 完全隔离。"""
    d = Path(config_dir)
    out = {"ok": False, "dir": str(d), "path": "", "error": ""}
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / "settings.json"
        existing = {}
        if p.exists():
            try:
                existing = json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:
                existing = {}
        p.write_text(json.dumps(merge_claude_settings(existing, env),
                                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out.update(ok=True, path=str(p))
    except Exception as e:  # noqa: BLE001
        out["error"] = f"生成失败：{e}"
    return out


def find_claude_cli() -> str:
    for c in CLAUDE_CLI_CANDIDATES:
        try:
            if c and str(c) not in ("", ".") and Path(c).exists():
                return str(c)
        except Exception:
            continue
    try:
        return shutil.which("claude") or ""
    except Exception:
        return ""


def verify_claude_cli(env: dict, cli_path: str = "", timeout: float = 240.0,
                      log=None) -> dict:
    """端到端验证：在一个空的隔离配置目录里跑 `claude -p PONG`，证明整条链路真的通。"""
    def L(msg):
        if log:
            log(msg)

    res = {"ok": False, "cli": "", "cmd": "", "result": "", "error": "", "status": "",
           "is_error": None, "terminal_reason": "", "model_usage": {}, "config_dir": "",
           "raw": ""}
    cli = cli_path or find_claude_cli()
    if cli and not Path(cli).exists():
        try:
            if not shutil.which(cli):
                cli = ""
        except Exception:
            cli = ""
    res["cli"] = cli
    if not cli:
        res["error"] = "没找到 claude 命令：先安装 @anthropic-ai/claude-code，或确认它在 PATH 里"
        return res
    tmp = Path(tempfile.mkdtemp(prefix="api_probe_cc_"))
    res["config_dir"] = str(tmp)
    run_env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
              "ANTHROPIC_MODEL"):
        run_env.pop(k, None)
    run_env.update({k: str(v) for k, v in (env or {}).items() if v})
    run_env["CLAUDE_CONFIG_DIR"] = str(tmp)      # 隔离：不读也不写你正式的 ~/.claude
    args = [cli, "-p", CLAUDE_PING_PROMPT, "--max-turns", "1", "--output-format", "json"]
    if cli.lower().endswith((".cmd", ".bat")):
        args = ["cmd", "/c"] + args
    res["cmd"] = " ".join(args)
    L(f"  执行：{res['cmd']}（CLAUDE_CONFIG_DIR={tmp}）")
    try:
        cp = subprocess.run(args, env=run_env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout,
                            cwd=str(tmp))
    except subprocess.TimeoutExpired:
        res["error"] = f"执行超时（>{timeout:.0f}s）"
    except Exception as e:  # noqa: BLE001
        res["error"] = f"执行失败：{type(e).__name__}: {e}"
    else:
        raw = (cp.stdout or "").strip()
        res["raw"] = raw[-2000:]
        obj = safe_json(raw)
        if isinstance(obj, dict):
            res["result"] = str(obj.get("result") or "")
            res["is_error"] = obj.get("is_error")
            res["terminal_reason"] = str(obj.get("terminal_reason") or "")
            res["status"] = str(obj.get("api_error_status") or "")
            if isinstance(obj.get("modelUsage"), dict):
                res["model_usage"] = obj["modelUsage"]
            res["ok"] = (not obj.get("is_error")) and "PONG" in res["result"]
        if not res["ok"] and not res["error"]:
            res["error"] = (res["result"] or (cp.stderr or "").strip()[:400]
                            or "输出无法解析为 JSON")
    shutil.rmtree(tmp, ignore_errors=True)
    return res


def find_claude_backups(path: str | Path | None = None) -> list[Path]:
    """列出 settings.json 旁边的备份（本工具的 .bak-apiprobe-* 与其它 .bak-* 都算）。"""
    p = Path(path) if path else CLAUDE_SETTINGS_PATH
    try:
        items = [q for q in p.parent.glob(p.name + ".bak*") if q.is_file()]
    except Exception:
        return []
    return sorted(items, key=lambda q: q.stat().st_mtime, reverse=True)


# =====================================================================
# 八、Codex CLI 接入（OpenAI 格式）
# =====================================================================
# Codex CLI 读 ~/.codex/config.toml，第三方网关靠 [model_providers.*] 描述：
#     [model_providers.<id>]
#     name = "..."            # 随便起，给人看
#     base_url = "https://gw.example.com/v1"   # 注意：与 Anthropic 相反，这里要带 /v1
#     env_key = "XXX_API_KEY" # Key 不放配置文件，而是从这个环境变量读（Codex 会加载 ~/.codex/.env）
#     wire_api = "responses"  # 或 "chat"：决定打 /v1/responses 还是 /v1/chat/completions
# 顶层再写 model / model_provider 指过去。
# 所以本节的活是两个判断：网关支持哪种 wire_api、这个 Key 能用哪个模型。

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
CODEX_CONFIG_PATH = CODEX_HOME / "config.toml"
CODEX_ENV_PATH = CODEX_HOME / ".env"
CODEX_PROVIDER_ID = "api_probe"
CODEX_MANAGED_BEGIN = "# >>> api-probe managed provider (auto-generated) >>>"
CODEX_MANAGED_END = "# <<< api-probe managed provider <<<"
CODEX_TOP_BEGIN = "# >>> api-probe managed top-level (auto-generated) >>>"
CODEX_TOP_END = "# <<< api-probe managed top-level <<<"
CODEX_PING_PROMPT = "Reply with exactly: PONG"


def find_codex_cli() -> str:
    """找 codex：先 PATH，再桌面版自带运行时（路径带版本号会变，按修改时间取最新）。"""
    try:
        w = shutil.which("codex")
        if w:
            return w
    except Exception:
        pass
    hits: list[Path] = []
    for root in (Path(os.environ.get("LOCALAPPDATA") or "") / "OpenAI" / "Codex" / "bin",
                 Path(os.environ.get("LOCALAPPDATA") or "") / "Programs" / "codex"):
        try:
            if root.is_dir():
                hits.extend(p for p in root.rglob("codex.exe") if p.is_file())
        except Exception:
            continue
    for extra in (Path.home() / ".local" / "bin" / "codex.exe",
                  Path(os.environ.get("APPDATA") or "") / "npm" / "codex.cmd"):
        try:
            if extra.exists():
                hits.append(extra)
        except Exception:
            continue
    if not hits:
        return ""
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(hits[0])


def codex_base_url_for_gateway(base: str) -> str:
    """Codex 的 base_url 要带 /v1（与 ANTHROPIC_BASE_URL 正好相反）。"""
    root = normalize_base(base)
    return f"{root}/v1" if root else ""


def codex_ping_payload(model: str, wire_api: str) -> dict:
    if wire_api == "responses":
        return {"model": model, "input": CODEX_PING_PROMPT, "max_output_tokens": 16}
    return {"model": model, "max_tokens": 16, "stream": False,
            "messages": [{"role": "user", "content": CODEX_PING_PROMPT}]}


def responses_reply_text(obj) -> str:
    """从 Responses API 响应里取纯文本。"""
    if not isinstance(obj, dict):
        return ""
    if isinstance(obj.get("output_text"), str) and obj["output_text"]:
        return obj["output_text"].strip()
    out = []
    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        for blk in item.get("content") or []:
            if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                out.append(blk["text"])
            elif isinstance(blk, str):
                out.append(blk)
    return "".join(out).strip()


def chat_reply_text(obj) -> str:
    if not isinstance(obj, dict):
        return ""
    for ch in obj.get("choices") or []:
        if isinstance(ch, dict):
            msg = ch.get("message") or {}
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return msg["content"].strip()
    return ""


def check_openai_channel(base: str, key: str, model: str = "",
                         timeout: float = DEFAULT_TIMEOUT, verify: bool = True,
                         log=None, stop_event: threading.Event | None = None) -> dict:
    """检测 OpenAI 格式通道：/v1/responses 与 /v1/chat/completions 哪个可用。

    返回里的 wire_api 就是要写进 Codex config.toml 的值（responses / chat）。
    """
    def L(msg):
        if log:
            log(msg)

    res = {"ok": False, "wire_api": "", "auth_style": "bearer", "status": 0, "error": "",
           "model_used": model or "", "reply": "", "latency_ms": None, "tried": [],
           "base_url": codex_base_url_for_gateway(base), "verdict": "", "verdict_level": "error"}
    gw = codex_base_url_for_gateway(base)
    if not gw:
        res["verdict"] = "地址为空"
        return res
    probe_timeout = max(5.0, min(timeout, 20.0))

    for wire, path in (("responses", "/responses"), ("chat", "/chat/completions")):
        if stop_event is not None and stop_event.is_set():
            res["verdict"] = "已手动停止"
            return res
        url = gw + path
        r = http_request("POST", url, headers=claude_auth_headers("bearer", key),
                         payload=codex_ping_payload(res["model_used"], wire),
                         timeout=probe_timeout, verify=verify)
        msg = "" if r.ok else (r.error or extract_error_message(r.text, r.status))
        info = {"wire_api": wire, "url": url, "status": r.status, "ms": r.latency_ms, "error": msg}
        L(f"  [OpenAI/{wire}] POST {url} → {r.status or r.error} {r.latency_ms}ms")
        res["tried"].append(info)
        if r.ok:
            text = (responses_reply_text(r.json) if wire == "responses"
                    else chat_reply_text(r.json))
            res.update(ok=True, wire_api=wire, status=r.status, latency_ms=r.latency_ms,
                       reply=text[:240], error="")
            info["ok"] = True
            break
        low = ((msg or "") + " " + (r.text or "")[:600]).lower()
        if r.status == 404 and ("model" in low or "not supported" in low):
            res.update(status=404, error=msg, latency_ms=r.latency_ms)
            info["model_not_found"] = True
            continue          # 路径是通的，只是模型不行 → 再看下一条通道
        res.update(status=r.status, error=msg, latency_ms=r.latency_ms)

    model_404 = bool(res["tried"]) and all(t.get("model_not_found") for t in res["tried"])
    if res["ok"]:
        res["verdict"] = (f"OpenAI 通道可用：wire_api = {res['wire_api']}，"
                          f"非流式 {res['latency_ms']:.0f}ms，返回 {res['reply'][:40]!r}")
        res["verdict_level"] = "ok"
    elif model_404:
        res["verdict"] = (f"两条通道路径都在（404 但提到模型），只是模型「{res['model_used']}」"
                          "不可用 → 换个模型再测")
        res["verdict_level"] = "warn"
    else:
        statuses = [t.get("status") for t in res["tried"]]
        if statuses and all(s in (401, 403) for s in statuses):
            res["verdict"] = (f"端点存在（返回 {statuses[0]} 而非 404），但鉴权失败：{res['error']}"
                              " —— 换一个有效 Key 再测")
            res["verdict_level"] = "warn"
        else:
            res["verdict"] = f"OpenAI 通道不可用：{res['error'] or '未知错误'}"
            res["verdict_level"] = "error"
    return res


def suggest_codex_model(models: list[dict], fallback: str = "") -> str:
    """从模型清单里挑一个适合 Codex 的默认模型。"""
    ids = [str(m.get("id") or "") for m in (models or []) if m.get("id")]
    if not ids:
        return fallback or ""
    if fallback and fallback in ids:
        return fallback
    cands = [i for i in ids
             if not any(b in i.lower() for b in NON_CHAT_MARKERS)
             and not any(v in i.lower() for v in VARIANT_MARKERS)]
    for want in ("gpt-6", "gpt-5", "gpt-4.1", "o4", "o3", "gpt-4o", "codex", "gpt-4"):
        hit = [i for i in cands if want in i.lower()]
        if hit:
            hit.sort(key=lambda s: (len(s), s))
            return hit[0]
    if fallback:
        return fallback
    return choose_default_model(models) if models else ""


def render_codex_top_block(model: str) -> str:
    """顶层键（TOML 要求裸键写在任何 [table] 之前）。"""
    return (f"{CODEX_TOP_BEGIN}\n"
            f'model = "{model}"\n'
            f'model_provider = "{CODEX_PROVIDER_ID}"\n'
            f"{CODEX_TOP_END}\n")


def render_codex_provider_block(base_url: str, env_key: str, wire_api: str = "responses",
                                provider_name: str = "API Probe") -> str:
    return (f"{CODEX_MANAGED_BEGIN}\n"
            f"[model_providers.{CODEX_PROVIDER_ID}]\n"
            f'name = "{provider_name}"\n'
            f'base_url = "{base_url}"\n'
            f'env_key = "{env_key}"\n'
            f'wire_api = "{wire_api}"\n'
            f"{CODEX_MANAGED_END}\n")


def _strip_marker_block(text: str, begin: str, end: str) -> str:
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        if line.strip() == begin:
            skipping = True
            continue
        if line.strip() == end:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "".join(out)


def merge_codex_config(existing: str, model: str, base_url: str, env_key: str,
                       provider_name: str = "API Probe", wire_api: str = "responses") -> str:
    """把 provider 表与顶层 model/model_provider 合并进现有 config.toml。

    - 先删掉上次由本工具写入的标记块，可反复执行
    - 顶层原有的 model / model_provider 会被替换（否则 TOML 重复键直接报错），旧值在备份里
    - 顶层块放文件开头（TOML 要求裸键在表之前），provider 表追加到末尾
    """
    text = _strip_marker_block(existing or "", CODEX_MANAGED_BEGIN, CODEX_MANAGED_END)
    text = _strip_marker_block(text, CODEX_TOP_BEGIN, CODEX_TOP_END)
    kept: list[str] = []
    in_table = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and not s.startswith("[["):
            in_table = True
        if not in_table and re.match(r"^\s*(model|model_provider)\s*=", line):
            continue
        kept.append(line)
    body = "\n".join(kept).strip("\n")
    top = render_codex_top_block(model)
    table = render_codex_provider_block(base_url, env_key, wire_api, provider_name)
    return f"{top}\n{body}\n\n{table}" if body else f"{top}\n{table}"


def install_codex_config(model: str, base_url: str, env_key: str,
                         provider_name: str = "API Probe", wire_api: str = "responses",
                         path: str | Path | None = None) -> dict:
    """写入 ~/.codex/config.toml（合并 + 备份；读不出来就中止，不覆盖）。"""
    p = Path(path) if path else CODEX_CONFIG_PATH
    out = {"ok": False, "path": str(p), "backup": "", "error": "", "old_model": "",
           "old_provider": "", "text": ""}
    existing = ""
    if p.exists():
        try:
            existing = p.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            out["error"] = f"读取失败，已中止：{e}"
            return out
        # 现有文件必须是合法 TOML，否则拒绝写入（有备份也不该冒险破坏用户配置）
        try:
            import tomllib
            tomllib.loads(existing or "")
        except ModuleNotFoundError:
            pass                      # Python < 3.11 没得校验，靠备份兜底
        except Exception as e:  # noqa: BLE001
            out["error"] = f"现有 config.toml 不是合法 TOML（{e}），已中止以免覆盖：{p}"
            return out
        for line in existing.splitlines():
            s = line.strip()
            if s.startswith("["):
                break
            m = re.match(r'^\s*model\s*=\s*"?([^"\n]*)"?\s*$', line)
            if m and not out["old_model"]:
                out["old_model"] = m.group(1).strip()
            m2 = re.match(r'^\s*model_provider\s*=\s*"?([^"\n]*)"?\s*$', line)
            if m2 and not out["old_provider"]:
                out["old_provider"] = m2.group(1).strip()
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = p.with_name(p.name + f".bak-apiprobe-{ts}")
        try:
            shutil.copy2(p, bak)
            out["backup"] = str(bak)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"备份失败，已中止：{e}"
            return out
    merged = merge_codex_config(existing, model, base_url, env_key, provider_name, wire_api)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(merged, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        out["error"] = f"写入失败：{e}"
        return out
    out.update(ok=True, text=merged)
    return out


def install_codex_env_key(env_key: str, value: str, path: str | Path | None = None) -> dict:
    """把 Key 写进 ~/.codex/.env —— Codex 会加载它，config 里的 env_key 就从这里取值。"""
    p = Path(path) if path else CODEX_ENV_PATH
    out = {"ok": False, "path": str(p), "error": "", "backup": ""}
    if not env_key or not value:
        out["error"] = "env_key 或 Key 为空"
        return out
    lines: list[str] = []
    if p.exists():
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception as e:  # noqa: BLE001
            out["error"] = f"读取失败：{e}"
            return out
        try:
            ts = time.strftime("%Y%m%d-%H%M%S")
            bak = p.with_name(p.name + f".bak-apiprobe-{ts}")
            shutil.copy2(p, bak)
            out["backup"] = str(bak)
        except Exception:
            pass
    kept = [ln for ln in lines if not ln.strip().startswith(f"{env_key}=")]
    kept.append(f"{env_key}={value}")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(kept).strip("\n") + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        out["error"] = f"写入失败：{e}"
        return out
    out["ok"] = True
    return out


def find_codex_backups(path: str | Path | None = None) -> list[Path]:
    p = Path(path) if path else CODEX_CONFIG_PATH
    try:
        items = [q for q in p.parent.glob(p.name + ".bak*") if q.is_file()]
    except Exception:
        return []
    return sorted(items, key=lambda q: q.stat().st_mtime, reverse=True)


def verify_codex_cli(model: str, base_url: str, env_key: str, key: str,
                     cli_path: str = "", timeout: float = 300.0, log=None,
                     wire_api: str = "responses") -> dict:
    """端到端验证：在隔离的 CODEX_HOME 里跑 `codex exec "Reply with exactly: PONG"`。"""
    def L(msg):
        if log:
            log(msg)

    res = {"ok": False, "cli": "", "cmd": "", "result": "", "error": "", "config_home": "",
           "raw": "", "wire_api": wire_api}
    cli = cli_path or find_codex_cli()
    if cli and not Path(cli).exists():
        try:
            if not shutil.which(cli):
                cli = ""
        except Exception:
            cli = ""
    res["cli"] = cli
    if not cli:
        res["error"] = "没找到 codex 命令（装 Codex CLI，或用桌面版自带的 codex.exe）"
        return res

    home = Path(tempfile.mkdtemp(prefix="api_probe_codex_"))
    work = Path(tempfile.mkdtemp(prefix="api_probe_codexwork_"))
    res["config_home"] = str(home)
    try:
        (home / "config.toml").write_text(
            merge_codex_config("", model, base_url, env_key, "API Probe", wire_api),
            encoding="utf-8")
        (home / ".env").write_text(f"{env_key}={key}\n", encoding="utf-8")
        last = work / "last.txt"
        args = [cli, "exec", CODEX_PING_PROMPT, "-C", str(work), "--skip-git-repo-check",
                "--ephemeral", "-s", "read-only", "-o", str(last)]
        if cli.lower().endswith((".cmd", ".bat")):
            args = ["cmd", "/c"] + args
        res["cmd"] = " ".join(args)
        run_env = dict(os.environ)
        run_env["CODEX_HOME"] = str(home)
        run_env[env_key] = key
        L(f"  执行：{res['cmd']}（CODEX_HOME={home}）")
        try:
            cp = subprocess.run(args, env=run_env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                cwd=str(work))
        except subprocess.TimeoutExpired:
            res["error"] = f"执行超时（>{timeout:.0f}s）"
            return res
        except Exception as e:  # noqa: BLE001
            res["error"] = f"执行失败：{type(e).__name__}: {e}"
            return res
        text = ""
        if last.exists():
            try:
                text = last.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                text = ""
        if not text:
            text = (cp.stdout or "").strip()
        res["result"] = text[-600:]
        res["raw"] = ((cp.stdout or "") + "\n" + (cp.stderr or "")).strip()[-2000:]
        res["ok"] = "PONG" in text
        if not res["ok"]:
            tail = (cp.stderr or "").strip() or (cp.stdout or "").strip()
            res["error"] = tail[-400:] or "输出里没有 PONG"
    finally:
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    return res


def render_codex_launcher(env_key: str, key: str, codex_exe: str = "",
                          codex_home: str = "") -> str:
    """启动脚本：临时设好环境变量再拉起 codex（CRLF + 纯 ASCII）。"""
    lines = ["@echo off",
             "rem Codex launcher generated by api_probe - keep this file ASCII-only,",
             "rem because cmd.exe parses .bat with the OEM code page."]
    if codex_home:
        lines.append(f'set "CODEX_HOME={codex_home}"')
    lines.append(f'set "{env_key}={key}"')
    lines.append(f'"{codex_exe}" %*' if codex_exe else "codex %*")
    return "\r\n".join(lines) + "\r\n"


def write_codex_launcher(env_key: str, key: str, path: str | Path,
                         codex_exe: str = "", codex_home: str = "") -> dict:
    out = {"ok": False, "path": str(path), "error": ""}
    for name, val in ((env_key, key), ("CODEX_HOME", codex_home), ("codex", codex_exe)):
        if val and not str(val).isascii():
            out["error"] = (f"{name} 的值含非 ASCII 字符，没法安全写进 .bat"
                            "（cmd 按 OEM 码页读批处理）——请改用写 config.toml + .env 的方式")
            return out
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render_codex_launcher(env_key, key, codex_exe, codex_home),
                     encoding="ascii", newline="")
    except Exception as e:  # noqa: BLE001
        out["error"] = f"写入失败：{e}"
        return out
    out["ok"] = True
    return out


# =====================================================================
# 九、图形界面
# =====================================================================

# 「站点」下拉里那个不落盘的临时项
ADHOC_SITE = "✎ 自定义（临时）"

# 添加/编辑站点对话框里的「常用预设」快速填充
QUICK_PRESETS = [
    ("自定义 / 中转站", ""),
    ("OpenAI 官方", "https://api.openai.com"),
    ("Anthropic 官方", "https://api.anthropic.com"),
    ("Gemini 官方", "https://generativelanguage.googleapis.com"),
    ("DeepSeek", "https://api.deepseek.com"),
    ("硅基流动 SiliconFlow", "https://api.siliconflow.cn"),
    ("OpenRouter", "https://openrouter.ai/api/v1"),
    ("阿里云百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4"),
    ("月之暗面 Kimi", "https://api.moonshot.cn"),
]

COLORS = {
    "ok": "#0a7d32",
    "warn": "#b45309",
    "risk": "#b91c1c",
    "error": "#b91c1c",
    "info": "#1f4e79",
    "muted": "#6b7280",
}

LEVEL_LABEL = {
    "ok": "✔ 校验",
    "info": "ℹ 提示",
    "warn": "⚠ 警告",
    "risk": "⛔ 风险",
    "error": "✘ 错误",
}


class ProbeApp:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = False
        self.last_result: dict | None = None      # 第 2 步之后的结果（含两段信息）
        self.site_result: dict | None = None      # 第 1 步的结果，供反复换模型重测
        self.batch_rows: list[dict] = []
        self._site_signature = ""                 # 记录第 1 步对应的「地址+Key」，变了就作废
        self._save_job = None                     # 延迟写盘的 after 句柄
        # 站点管理：sites 是用户添加的多个中转站，adhoc 是「自定义（临时）」那一格
        self.sites: list[dict] = []
        self.adhoc: dict = {"name": ADHOC_SITE, "url": "", "key": "", "model": ""}
        self._active_site = ADHOC_SITE            # 当前已装载进输入框的那一格
        # Claude Code 接入
        self.cc_state: dict = {}                  # 最近一次 Anthropic 通道检测结果
        self.codex_state: dict = {}               # 最近一次 OpenAI 通道检测结果
        self.cc_model_vars: dict = {}             # 别名 -> (StringVar, Combobox)
        self.tab_claude = None
        self.tab_codex = None

        root.title(f"{APP_NAME} v{APP_VERSION}")
        root.geometry("1180x760")
        root.minsize(980, 640)
        self._setup_style()
        self._build_ui()
        self._load_config()
        self._refresh_cc_gate()                   # 启动时接入按钮是锁着的
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._pump)

    # ---------------- 样式 ----------------
    def _setup_style(self):
        ttk = self.ttk
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except Exception:
                    pass
        base_font = ("Microsoft YaHei UI", 9)
        mono = ("Consolas", 9)
        style.configure(".", font=base_font)
        style.configure("Treeview", font=base_font, rowheight=22)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Hint.TLabel", foreground=COLORS["muted"])
        self.mono_font = mono

    # ---------------- 界面 ----------------
    def _build_ui(self):
        tk, ttk = self.tk, self.ttk

        # 顶部：输入区
        top = ttk.LabelFrame(self.root, text=" 连接信息 ")
        top.pack(fill="x", padx=10, pady=(10, 6))

        row1 = ttk.Frame(top)
        row1.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(row1, text="站点").pack(side="left")
        self.var_site = tk.StringVar(value=ADHOC_SITE)
        self.cmb_site = ttk.Combobox(row1, textvariable=self.var_site, width=26,
                                     values=[ADHOC_SITE], state="readonly")
        self.cmb_site.pack(side="left", padx=(6, 6))
        self.cmb_site.bind("<<ComboboxSelected>>", self._on_site_change)
        ttk.Button(row1, text="＋ 添加站点", command=self.add_site).pack(side="left")
        self.btn_edit_site = ttk.Button(row1, text="编辑", command=self.edit_site, state="disabled")
        self.btn_edit_site.pack(side="left", padx=4)
        self.btn_del_site = ttk.Button(row1, text="删除", command=self.delete_site, state="disabled")
        self.btn_del_site.pack(side="left")

        self.var_timeout = tk.StringVar(value=str(int(DEFAULT_TIMEOUT)))
        ttk.Spinbox(row1, from_=3, to=180, increment=1, width=5,
                    textvariable=self.var_timeout).pack(side="right")
        ttk.Label(row1, text="超时(秒)").pack(side="right", padx=(0, 6))

        row_url = ttk.Frame(top)
        row_url.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(row_url, text="API 地址").pack(side="left")
        self.var_url = tk.StringVar()
        self.ent_url = ttk.Entry(row_url, textvariable=self.var_url, font=self.mono_font)
        self.ent_url.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.ent_url.bind("<Return>", lambda e: self.fetch_models())
        self.ent_url.bind("<KeyRelease>", self._on_conn_changed)

        row2 = ttk.Frame(top)
        row2.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(row2, text="API Key").pack(side="left")
        self.var_key = tk.StringVar()
        self.ent_key = ttk.Entry(row2, textvariable=self.var_key, show="*", font=self.mono_font)
        self.ent_key.pack(side="left", fill="x", expand=True, padx=(6, 8))
        self.ent_key.bind("<Return>", lambda e: self.fetch_models())
        self.ent_key.bind("<KeyRelease>", self._on_conn_changed)
        self.var_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="显示", variable=self.var_show,
                        command=self._toggle_key).pack(side="left")

        ttk.Label(row2, text="指定模型").pack(side="left", padx=(14, 0))
        self.var_model = tk.StringVar()
        ent_model = ttk.Entry(row2, textvariable=self.var_model, width=26, font=self.mono_font)
        ent_model.pack(side="left", padx=(6, 0))
        ent_model.bind("<Return>", lambda e: self.run_generation())

        row3 = ttk.Frame(top)
        row3.pack(fill="x", padx=8, pady=(0, 8))
        self.var_verify = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text="校验 TLS 证书（自签站可取消）",
                        variable=self.var_verify).pack(side="left")
        self.var_deep = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text="深度检测（补测 usage）",
                        variable=self.var_deep).pack(side="left", padx=(12, 0))
        self.var_remember = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="记住 Key（本机加密保存，含批量列表）",
                        variable=self.var_remember,
                        command=self._on_remember_toggle).pack(side="left", padx=(12, 0))
        ttk.Button(row3, text="清除已存 Key",
                   command=self.clear_saved_key).pack(side="left", padx=(8, 0))
        ttk.Label(row3, text=f"配置：{CONFIG_PATH}", style="Hint.TLabel").pack(side="right")

        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        self.btn_list = ttk.Button(btns, text="①  获取模型列表", command=self.fetch_models)
        self.btn_list.pack(side="left")
        self.btn_gen = ttk.Button(btns, text="②  对选中模型发测试", command=self.run_generation,
                                  state="disabled")
        self.btn_gen.pack(side="left", padx=6)
        ttk.Button(btns, text="③  接入 Claude Code",
                   command=self.goto_claude_tab).pack(side="left", padx=6)
        ttk.Button(btns, text="④  接入 Codex",
                   command=self.goto_codex_tab).pack(side="left", padx=6)
        self.btn_flow = ttk.Button(btns, text="▶  全流程（探测 → 测速 → 接入）",
                                   command=self.run_full_flow)
        self.btn_flow.pack(side="left", padx=(14, 4))
        self.btn_stop = ttk.Button(btns, text="■  停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(btns, text="复制结论", command=self.copy_verdict).pack(side="left", padx=(18, 4))
        ttk.Button(btns, text="导出 JSON", command=lambda: self.export_last("json")).pack(side="left", padx=4)
        ttk.Button(btns, text="导出 CSV", command=lambda: self.export_last("csv")).pack(side="left", padx=4)
        self.lbl_status = ttk.Label(btns, text="就绪", style="Hint.TLabel")
        self.lbl_status.pack(side="right")

        # 中部：标签页
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        self._build_tab_overview()
        self._build_tab_models()
        self._build_tab_batch()
        self._build_tab_claude()
        self._build_tab_codex()
        self._build_tab_log()

        # 底部状态
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=10, pady=(0, 8))
        self.lbl_summary = ttk.Label(bottom, text="第 ① 步拉取模型列表 → 在「模型列表」里挑一个模型 → 第 ② 步发测试。"
                                                 "地址支持直接粘贴 https://xxx.com/v1/chat/completions 这种完整写法。",
                                     style="Hint.TLabel")
        self.lbl_summary.pack(side="left")

    def _build_tab_overview(self):
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="  概览  ")

        self.tv_ov = ttk.Treeview(frame, columns=("item", "value"), show="headings", height=18)
        self.tv_ov.heading("item", text="项目")
        self.tv_ov.heading("value", text="结果")
        self.tv_ov.column("item", width=150, anchor="w", stretch=False)
        self.tv_ov.column("value", width=900, anchor="w")
        vs = ttk.Scrollbar(frame, orient="vertical", command=self.tv_ov.yview)
        self.tv_ov.configure(yscrollcommand=vs.set)
        self.tv_ov.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vs.pack(side="right", fill="y", pady=6, padx=(0, 6))
        for lvl, color in COLORS.items():
            self.tv_ov.tag_configure(lvl, foreground=color)
        self.tv_ov.tag_configure("head", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_tab_models(self):
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="  模型列表  ")

        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(bar, text="过滤").pack(side="left")
        self.var_filter = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.var_filter, width=32, font=self.mono_font)
        ent.pack(side="left", padx=6)
        ent.bind("<KeyRelease>", lambda e: self._refresh_models())
        self.var_only_main = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="只看主流模型", variable=self.var_only_main,
                        command=self._refresh_models).pack(side="left", padx=8)
        ttk.Button(bar, text="复制全部模型名", command=self.copy_models).pack(side="left", padx=6)
        ttk.Button(bar, text="导出模型列表", command=self.export_models).pack(side="left")
        self.lbl_models = ttk.Label(bar, text="（尚未测试）", style="Hint.TLabel")
        self.lbl_models.pack(side="right", padx=6)

        bar2 = ttk.Frame(frame)
        bar2.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(bar2, text="当前选中：").pack(side="left")
        self.lbl_picked = ttk.Label(bar2, text="（未选择）", font=self.mono_font,
                                    foreground=COLORS["info"])
        self.lbl_picked.pack(side="left")
        ttk.Button(bar2, text="②  用选中模型发测试",
                   command=self.run_generation).pack(side="left", padx=10)
        ttk.Label(bar2, text="单击选中 / 双击直接测试；列表里没有的模型，可直接在上方「指定模型」手写。",
                  style="Hint.TLabel").pack(side="left", padx=4)

        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True, padx=6, pady=6)
        self.tv_models = ttk.Treeview(body, columns=("idx", "id", "tag", "owner"),
                                      show="headings", selectmode="browse")
        for col, txt, w, anchor in (("idx", "#", 60, "e"), ("id", "模型 ID", 520, "w"),
                                    ("tag", "标记", 120, "w"), ("owner", "归属/备注", 220, "w")):
            self.tv_models.heading(col, text=txt)
            self.tv_models.column(col, width=w, anchor=anchor, stretch=(col == "id"))
        vs = ttk.Scrollbar(body, orient="vertical", command=self.tv_models.yview)
        self.tv_models.configure(yscrollcommand=vs.set)
        self.tv_models.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tv_models.bind("<<TreeviewSelect>>", self._on_model_select)
        self.tv_models.bind("<Double-1>", self._on_model_dclick)

    def _build_tab_batch(self):
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="  批量测试  ")

        left = ttk.LabelFrame(frame, text=" 每行一个站点：地址 | Key | [模型]（批量模式自动挑模型；想指定就写第三列） ")
        left.pack(side="left", fill="both", expand=False, padx=(6, 4), pady=6)
        self.txt_batch = tk.Text(left, width=46, height=20, font=self.mono_font, wrap="none")
        self.txt_batch.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt_batch.insert("1.0",
                              "# 示例（以 # 开头的行会被忽略）\n"
                              "https://api.deepseek.com | sk-xxxxxxxx | deepseek-chat\n"
                              "https://your-relay.com/v1 | sk-yyyyyyyy\n")
        self.txt_batch.bind("<KeyRelease>", lambda e: self._schedule_save())

        right = ttk.Frame(frame)
        right.pack(side="left", fill="both", expand=True, padx=(4, 6), pady=6)

        bar = ttk.Frame(right)
        bar.pack(fill="x")
        self.btn_batch = ttk.Button(bar, text="▶  开始批量测试", command=self.start_batch)
        self.btn_batch.pack(side="left")
        ttk.Label(bar, text="并发").pack(side="left", padx=(10, 2))
        self.var_workers = tk.StringVar(value="2")
        ttk.Spinbox(bar, from_=1, to=8, width=4, textvariable=self.var_workers).pack(side="left")
        ttk.Button(bar, text="导出 CSV", command=lambda: self.export_batch("csv")).pack(side="left", padx=8)
        ttk.Button(bar, text="导出 JSON", command=lambda: self.export_batch("json")).pack(side="left")
        ttk.Button(bar, text="导入站点列表", command=self.import_sites_to_batch).pack(side="left", padx=8)
        ttk.Button(bar, text="清空结果", command=self._clear_batch).pack(side="left", padx=8)

        cols = ("url", "proto", "status", "ms", "first", "n", "models", "panel", "verdict")
        heads = (("url", "地址", 240), ("proto", "协议", 110), ("status", "HTTP", 60),
                 ("ms", "列表ms", 70), ("first", "首字ms", 70), ("n", "模型数", 70),
                 ("models", "请求→返回模型", 260), ("panel", "指纹", 150), ("verdict", "结论", 340))
        self.tv_batch = ttk.Treeview(right, columns=cols, show="headings")
        for c, txt, w in heads:
            self.tv_batch.heading(c, text=txt)
            self.tv_batch.column(c, width=w, anchor="w", stretch=(c in ("verdict", "url")))
        vs = ttk.Scrollbar(right, orient="vertical", command=self.tv_batch.yview)
        self.tv_batch.configure(yscrollcommand=vs.set)
        self.tv_batch.pack(side="left", fill="both", expand=True, pady=(6, 0))
        vs.pack(side="right", fill="y", pady=(6, 0))
        for lvl, color in COLORS.items():
            self.tv_batch.tag_configure(lvl, foreground=color)
        self.tv_batch.bind("<Double-1>", self._on_batch_dclick)

    def _build_tab_claude(self):
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="  Claude Code  ")
        self.tab_claude = frame

        # ---- 通道检测 ----
        head = ttk.LabelFrame(
            frame, text=" ③ 接入 Claude Code —— 前提：网关实现 Anthropic Messages 协议（POST <BASE>/v1/messages）；CLI 需 2.x ")
        head.pack(fill="x", padx=6, pady=(6, 4))
        r1 = ttk.Frame(head)
        r1.pack(fill="x", padx=8, pady=(6, 2))
        self.btn_cc_check = ttk.Button(r1, text="检测通道 /v1/messages", command=self.cc_check)
        self.btn_cc_check.pack(side="left")
        ttk.Label(r1, text="（用当前选中站点的地址与 Key；先过这一步再写配置）",
                  style="Hint.TLabel").pack(side="left", padx=6)
        self.lbl_cc_cli = ttk.Label(r1, text="", style="Hint.TLabel")
        self.lbl_cc_cli.pack(side="right")

        self.tv_cc = ttk.Treeview(head, columns=("k", "v"), show="headings", height=6)
        self.tv_cc.heading("k", text="项目")
        self.tv_cc.heading("v", text="结果")
        self.tv_cc.column("k", width=110, anchor="w", stretch=False)
        self.tv_cc.column("v", width=880, anchor="w")
        self.tv_cc.pack(fill="x", padx=8, pady=(2, 8))
        for lvl, color in COLORS.items():
            self.tv_cc.tag_configure(lvl, foreground=color)

        # ---- 模型映射 ----
        mapf = ttk.LabelFrame(
            frame, text=" 模型映射：CLI 别名 → 网关真实模型 id（不做映射 = 拿官方 id 去打你的网关 = 404 model_not_found） ")
        mapf.pack(fill="x", padx=6, pady=4)
        grid = ttk.Frame(mapf)
        grid.pack(fill="x", padx=8, pady=(6, 2))
        pairs = [("默认模型", "default"), ("sonnet", "sonnet"), ("opus", "opus"),
                 ("haiku", "haiku"), ("fable", "fable")]
        for i, (label, key) in enumerate(pairs):
            row, col = divmod(i, 2)
            ttk.Label(grid, text=label).grid(row=row, column=col * 3, sticky="w",
                                             padx=(0, 4), pady=3)
            var = tk.StringVar()
            cmb = ttk.Combobox(grid, textvariable=var, width=32, font=self.mono_font)
            cmb.grid(row=row, column=col * 3 + 1, sticky="we", padx=(0, 16), pady=3)
            self.cc_model_vars[key] = (var, cmb)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(4, weight=1)
        r2 = ttk.Frame(mapf)
        r2.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(r2, text="从当前站点模型列表自动匹配",
                   command=self.cc_autofill).pack(side="left")
        ttk.Label(r2, text="（也可直接手打 id；列表为空就先做上面的第 ① 步）",
                  style="Hint.TLabel").pack(side="left", padx=6)

        # ---- 接入方式 ----
        act = ttk.LabelFrame(frame, text=" 接入方式（可多选） ")
        act.pack(fill="x", padx=6, pady=4)
        self.cc_write_settings = tk.BooleanVar(value=True)
        self.cc_write_launcher = tk.BooleanVar(value=False)
        self.cc_write_isolated = tk.BooleanVar(value=False)
        self.cc_run_verify = tk.BooleanVar(value=True)
        ttk.Checkbutton(act, text="写入用户配置 ~/.claude/settings.json（合并 + 自动备份）",
                        variable=self.cc_write_settings).pack(anchor="w", padx=8, pady=(6, 2))
        r3 = ttk.Frame(act)
        r3.pack(fill="x", padx=8, pady=2)
        ttk.Checkbutton(r3, text="生成启动脚本（设环境变量，不落盘 Key）",
                        variable=self.cc_write_launcher).pack(side="left")
        ttk.Checkbutton(r3, text="生成隔离配置目录（CLAUDE_CONFIG_DIR，与正式配置互不干扰）",
                        variable=self.cc_write_isolated).pack(side="left", padx=(16, 0))
        r4 = ttk.Frame(act)
        r4.pack(fill="x", padx=8, pady=(2, 4))
        ttk.Checkbutton(r4, text="运行端到端验证（claude -p PONG，隔离目录，不碰你的正式配置）",
                        variable=self.cc_run_verify).pack(side="left")
        self.lbl_cc_target = ttk.Label(r4, text=f"配置文件：{CLAUDE_SETTINGS_PATH}",
                                       style="Hint.TLabel")
        self.lbl_cc_target.pack(side="right")
        r5 = ttk.Frame(act)
        r5.pack(fill="x", padx=8, pady=(0, 8))
        self.btn_cc_install = ttk.Button(r5, text="▶  一键接入", command=self.cc_install,
                                         state="disabled")
        self.btn_cc_install.pack(side="left")
        self.lbl_cc_gate = ttk.Label(r5, text="", style="Hint.TLabel")
        self.lbl_cc_gate.pack(side="left", padx=8)
        ttk.Button(r5, text="仅预览配置", command=self.cc_preview).pack(side="left", padx=4)
        ttk.Button(r5, text="复制临时命令", command=self.cc_copy_oneliner).pack(side="left", padx=4)
        ttk.Button(r5, text="恢复上次备份", command=self.cc_restore).pack(side="left", padx=(14, 4))
        ttk.Button(r5, text="打开 ~/.claude", command=self.cc_open_dir).pack(side="left")
        ttk.Button(r5, text="打开输出目录",
                   command=lambda: self._open_path(CLAUDE_OUTPUT_DIR)).pack(side="left", padx=4)

        # ---- 日志 ----
        logf = ttk.LabelFrame(frame, text=" 执行日志 ")
        logf.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.txt_cc = tk.Text(logf, font=self.mono_font, wrap="word", height=8,
                              background="#111827", foreground="#e5e7eb",
                              insertbackground="#e5e7eb")
        vs = ttk.Scrollbar(logf, orient="vertical", command=self.txt_cc.yview)
        self.txt_cc.configure(yscrollcommand=vs.set)
        self.txt_cc.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vs.pack(side="right", fill="y", pady=6, padx=(0, 6))

    def _build_tab_codex(self):
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="  Codex (OpenAI)  ")
        self.tab_codex = frame

        head = ttk.LabelFrame(
            frame, text=" ④ 接入 Codex —— 前提：网关提供 /v1/responses 或 /v1/chat/completions；本机需有 codex CLI ")
        head.pack(fill="x", padx=6, pady=(6, 4))
        r1 = ttk.Frame(head)
        r1.pack(fill="x", padx=8, pady=(6, 2))
        self.btn_codex_check = ttk.Button(r1, text="检测通道（responses / chat）",
                                          command=self.codex_check)
        self.btn_codex_check.pack(side="left")
        ttk.Label(r1, text="（用当前站点的地址与 Key；结果里的 wire_api 会写进配置）",
                  style="Hint.TLabel").pack(side="left", padx=6)
        self.lbl_codex_cli = ttk.Label(r1, text="", style="Hint.TLabel")
        self.lbl_codex_cli.pack(side="right")

        self.tv_codex = ttk.Treeview(head, columns=("k", "v"), show="headings", height=6)
        self.tv_codex.heading("k", text="项目")
        self.tv_codex.heading("v", text="结果")
        self.tv_codex.column("k", width=110, anchor="w", stretch=False)
        self.tv_codex.column("v", width=880, anchor="w")
        self.tv_codex.pack(fill="x", padx=8, pady=(2, 8))
        for lvl, color in COLORS.items():
            self.tv_codex.tag_configure(lvl, foreground=color)

        mapf = ttk.LabelFrame(frame, text=" 模型与 Provider（写入 ~/.codex/config.toml） ")
        mapf.pack(fill="x", padx=6, pady=4)
        grid = ttk.Frame(mapf)
        grid.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(grid, text="默认模型").grid(row=0, column=0, sticky="w", padx=(0, 4), pady=3)
        self.var_codex_model = tk.StringVar()
        self.cmb_codex_model = ttk.Combobox(grid, textvariable=self.var_codex_model, width=32,
                                            font=self.mono_font)
        self.cmb_codex_model.grid(row=0, column=1, sticky="we", padx=(0, 16), pady=3)
        ttk.Label(grid, text="base_url").grid(row=0, column=2, sticky="w", padx=(0, 4), pady=3)
        self.var_codex_base = tk.StringVar()
        ttk.Entry(grid, textvariable=self.var_codex_base, width=34,
                  font=self.mono_font).grid(row=0, column=3, sticky="we", pady=3)
        ttk.Label(grid, text="Key 环境变量").grid(row=1, column=0, sticky="w", padx=(0, 4), pady=3)
        self.var_codex_envkey = tk.StringVar(value="CODEX_API_KEY")
        ttk.Entry(grid, textvariable=self.var_codex_envkey, width=32,
                  font=self.mono_font).grid(row=1, column=1, sticky="we", padx=(0, 16), pady=3)
        ttk.Label(grid, text="wire_api").grid(row=1, column=2, sticky="w", padx=(0, 4), pady=3)
        self.var_codex_wire = tk.StringVar(value="（检测后自动填）")
        c = ttk.Combobox(grid, textvariable=self.var_codex_wire, width=14, state="readonly",
                         values=["responses", "chat"])
        c.grid(row=1, column=3, sticky="w", pady=3)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)
        r2 = ttk.Frame(mapf)
        r2.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(r2, text="从当前站点模型列表挑一个",
                   command=self.codex_autofill).pack(side="left")
        ttk.Label(r2, text="（优先 gpt-6 / gpt-5 / gpt-4.1 / o 系列；也可手打）",
                  style="Hint.TLabel").pack(side="left", padx=6)

        act = ttk.LabelFrame(frame, text=" 接入方式（可多选） ")
        act.pack(fill="x", padx=6, pady=4)
        self.codex_write_config = tk.BooleanVar(value=True)
        self.codex_write_env = tk.BooleanVar(value=True)
        self.codex_write_launcher = tk.BooleanVar(value=False)
        self.codex_run_verify = tk.BooleanVar(value=True)
        ttk.Checkbutton(act, text="写入 ~/.codex/config.toml（只合并 provider 与 model，改前备份）",
                        variable=self.codex_write_config).pack(anchor="w", padx=8, pady=(6, 2))
        ttk.Checkbutton(act, text="把 Key 写进 ~/.codex/.env（Codex 的 env_key 机制；明文，改前备份）",
                        variable=self.codex_write_env).pack(anchor="w", padx=8, pady=2)
        r3 = ttk.Frame(act)
        r3.pack(fill="x", padx=8, pady=2)
        ttk.Checkbutton(r3, text="生成启动脚本（临时设环境变量再启动 codex）",
                        variable=self.codex_write_launcher).pack(side="left")
        ttk.Checkbutton(r3, text="运行端到端验证（codex exec PONG，隔离 CODEX_HOME）",
                        variable=self.codex_run_verify).pack(side="left", padx=(16, 0))
        r4 = ttk.Frame(act)
        r4.pack(fill="x", padx=8, pady=(2, 8))
        self.btn_codex_install = ttk.Button(r4, text="▶  一键接入 Codex", command=self.codex_install,
                                            state="disabled")
        self.btn_codex_install.pack(side="left")
        self.lbl_codex_gate = ttk.Label(r4, text="", style="Hint.TLabel")
        self.lbl_codex_gate.pack(side="left", padx=8)
        ttk.Button(r4, text="仅预览配置", command=self.codex_preview).pack(side="left", padx=4)
        ttk.Button(r4, text="复制临时命令", command=self.codex_copy_oneliner).pack(side="left", padx=4)
        ttk.Button(r4, text="恢复上次备份", command=self.codex_restore).pack(side="left", padx=(14, 4))
        ttk.Button(r4, text="打开 ~/.codex", command=self.codex_open_dir).pack(side="left")

        logf = ttk.LabelFrame(frame, text=" 执行日志 ")
        logf.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.txt_codex = tk.Text(logf, font=self.mono_font, wrap="word", height=8,
                                 background="#111827", foreground="#e5e7eb",
                                 insertbackground="#e5e7eb")
        vs = ttk.Scrollbar(logf, orient="vertical", command=self.txt_codex.yview)
        self.txt_codex.configure(yscrollcommand=vs.set)
        self.txt_codex.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vs.pack(side="right", fill="y", pady=6, padx=(0, 6))

    def _build_tab_log(self):
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="  原始日志  ")
        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Button(bar, text="清空日志", command=lambda: self._log_clear()).pack(side="left")
        ttk.Button(bar, text="复制日志", command=self.copy_log).pack(side="left", padx=6)
        ttk.Label(bar, text="每个 HTTP 请求的地址、状态码、耗时；失败时含响应片段。",
                  style="Hint.TLabel").pack(side="left", padx=10)
        self.txt_log = tk.Text(frame, font=self.mono_font, wrap="word", state="disabled",
                               background="#111827", foreground="#e5e7eb", insertbackground="#e5e7eb")
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=6)

    # ---------------- 全流程（探测 → 测速 → 接入） ----------------
    def run_full_flow(self):
        """按用户要的顺序一条龙：输入地址+Key → 探测模型 → 测是否跑得动/流不流畅 → 准备接入 Claude Code。"""
        if self.busy:
            self._set_status("正在测试中，请稍候…")
            return
        base = self.var_url.get().strip()
        if not base:
            self._set_status("请先填写 API 地址")
            return
        key = self.var_key.get().strip()
        model = self.var_model.get().strip()
        if not model and self.cc_model_vars:
            model = self.cc_model_vars["default"][0].get().strip()
        self.stop_event.clear()
        self._save_config()
        self._log_clear()
        self.busy = True
        self.btn_list.configure(state="disabled")
        self.btn_gen.configure(state="disabled")
        self.btn_batch.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._set_action_busy(True)
        self.tv_ov.delete(*self.tv_ov.get_children())
        self.tv_models.delete(*self.tv_models.get_children())
        self.tv_cc.delete(*self.tv_cc.get_children())
        self.site_result = None
        self.last_result = None
        self.cc_state = {}
        self._set_status("全流程进行中…")
        self.lbl_summary.configure(text=f"全流程：{base}")
        self._cc_log_append("=" * 52)
        self._cc_log_append(f"全流程开始：{base}（模型 {model or '自动挑一个'}）")
        self._log_append(f"=== 全流程开始 {base} ===")
        timeout = self._timeout()
        verify = bool(self.var_verify.get())
        deep = bool(self.var_deep.get())

        def work():
            try:
                res = run_full_pipeline(base, key, model, timeout=timeout, verify=verify,
                                        deep=deep, log=self._log, stop_event=self.stop_event)
                self.q.put(("flow_done", res))
            except Exception as e:  # noqa: BLE001
                self.q.put(("fatal", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _render_flow(self, res: dict):
        site = res.get("site") or {}
        gen = res.get("gen") or site
        ch = res.get("channel") or {}
        if site:
            self.site_result = site
            self.last_result = site
            self._site_signature = self._signature()
            self._refresh_models()
            self.lbl_models.configure(text=f"共 {len(site.get('models') or [])} 个模型")
        self._render_overview(gen if gen else site)
        self.cc_state = ch
        if ch:
            self._render_cc_check(ch)
        och = res.get("channel_openai") or {}
        self.codex_state = och
        if och:
            self._render_codex_check(och)
        ids = [str(m.get("id") or "") for m in (site.get("models") or []) if m.get("id")]
        mm = res.get("model_map") or {}
        for k, (var, cmb) in self.cc_model_vars.items():
            cmb.configure(values=ids)
            if mm.get(k):
                var.set(mm[k])
        cmb_cx = self.cmb_codex_model
        cmb_cx.configure(values=ids)
        if res.get("codex_model"):
            self.var_codex_model.set(res["codex_model"])
        self.var_codex_base.set(codex_base_url_for_gateway(self.var_url.get().strip()))
        cli = find_claude_cli()
        self.lbl_cc_cli.configure(text=("CLI：" + cli) if cli
                                  else "没找到 claude 命令（端到端验证会跳过）")
        cx_cli = find_codex_cli()
        self.lbl_codex_cli.configure(text=("CLI：" + cx_cli) if cx_cli
                                     else "没找到 codex（端到端验证会跳过）")
        for s in res.get("steps", []):
            mark = {True: "✔", False: "✘"}.get(s.get("ok"), "·")
            line = (f"{mark} [{s['no']}/4] {s['text']}"
                    + (f" → {s['detail']}" if s.get("detail") else ""))
            self._cc_log_append(line)
            self._codex_log_append(line)
        self._cc_log_append(f"结论：{res.get('verdict', '')}")
        self._codex_log_append(f"结论：{res.get('verdict', '')}")
        ready_claude = self.cc_ready()[0]
        ready_codex = self.codex_ready()[0]
        self.nb.select(self.tab_claude if ready_claude else
                       (self.tab_codex if ready_codex else self.tab_claude))
        self._set_status("全流程完成" if res.get("ok") else "全流程完成（有未通过项，见日志）")
        self.lbl_summary.configure(text=res.get("verdict", ""))
        self._log_append(f"=== 全流程完成：{res.get('verdict', '')} ===")
        self._finish_busy()
        if res.get("stopped"):
            return
        from tkinter import messagebox
        if ready_claude and self.cc_write_settings.get():
            extra = "　Codex 通道也可用，可在 Codex 页点「一键接入 Codex」。" if ready_codex else ""
            if messagebox.askyesno("全流程通过",
                                   "模型能跑、Claude Code 通道也通了。\n\n"
                                   "现在把这套配置写进 Claude Code 吗？\n"
                                   "（合并进 ~/.claude/settings.json，改前自动备份）" + extra):
                self.cc_install()
        elif ready_codex and self.codex_write_config.get():
            if messagebox.askyesno("全流程通过",
                                   "模型能跑、OpenAI 通道也通了。\n\n"
                                   "现在把这套配置写进 Codex 吗？\n"
                                   "（合并进 ~/.codex/config.toml，改前自动备份）"):
                self.codex_install()

    # ---------------- Claude Code 接入 ----------------
    def _open_path(self, path) -> None:
        """在文件管理器里打开目录（跨平台；非 Windows 用 open / xdg-open）。"""
        p = Path(path)
        if not p.exists():
            self._set_status(f"路径不存在：{p}")
            return
        target = str(p) if p.is_dir() else str(p.parent)
        try:
            if sys.platform == "win32":
                if p.is_dir():
                    os.startfile(str(p))
                else:
                    subprocess.Popen(["explorer", "/select,", str(p)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as e:  # noqa: BLE001
            self._set_status(f"打开失败：{e}")

    def cc_log_msg(self, msg: str) -> None:
        self.q.put(("cc_log", msg))

    def _cc_log_append(self, msg: str) -> None:
        self.txt_cc.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
        self.txt_cc.see("end")

    def cc_ready(self) -> tuple[bool, str]:
        """门禁：通道探测通过即可接入 —— 它本身就是一次真实的最小生成（拿到 PONG）。

        第 ② 步生成测试只作为提示：它走的是工具自己挑的协议路径，
        而 CLI 走的是 /v1/messages，两者不一定同时可用（例如只代理 Anthropic 的站）。
        返回 (是否可以接入, 说明或不可接入的原因)。
        """
        ch = self.cc_state or {}
        gen_ok = bool((self.site_result or {}).get("ok"))
        if ch.get("ok"):
            return True, ("" if gen_ok else
                          "注意：第 ② 步生成测试未通过，但 Claude Code 用的 /v1/messages 已实测可用")
        if ch.get("model_not_found"):
            return False, "通道认证通过但模型不可用：先在下面做好模型映射，再点「检测通道」复测"
        if ch:
            return False, f"Anthropic 通道未通过：{ch.get('verdict') or '未知原因'}"
        return False, "还没检测 Anthropic 通道：先点「检测通道 /v1/messages」或「▶ 全流程」"

    def _refresh_cc_gate(self) -> None:
        ok, why = self.cc_ready()
        try:
            self.btn_cc_install.configure(state="normal" if ok else "disabled")
            self.lbl_cc_gate.configure(
                text=(("已解锁，可以接入" + (f"（{why}）" if why else "")) if ok
                      else f"未解锁：{why}"),
                foreground=COLORS["ok"] if ok else COLORS["muted"])
        except Exception:
            pass

    def _set_action_busy(self, flag: bool) -> None:
        """忙碌时禁用「检测 / 全流程」，并按各自门禁决定两个「一键接入」是否可点。"""
        state = "disabled" if flag else "normal"
        for btn in (getattr(self, "btn_cc_check", None), getattr(self, "btn_flow", None),
                    getattr(self, "btn_codex_check", None)):
            try:
                btn.configure(state=state)
            except Exception:
                pass
        if flag:
            for btn in (getattr(self, "btn_cc_install", None),
                        getattr(self, "btn_codex_install", None)):
                try:
                    btn.configure(state="disabled")
                except Exception:
                    pass
        else:
            self._refresh_cc_gate()
            self._refresh_codex_gate()

    def goto_claude_tab(self):
        self.nb.select(self.tab_claude)
        self.cc_refresh_context()
        self._set_status("先点「① 检测通道 /v1/messages」，通过后再「一键接入」")

    def cc_refresh_context(self):
        """刷新 CLI 探测结果、候选模型下拉、配置路径。"""
        models = (self.site_result or {}).get("models") or []
        ids = [str(m.get("id") or "") for m in models if m.get("id")]
        for key, (var, cmb) in self.cc_model_vars.items():
            cmb.configure(values=ids)
            if key == "default" and not var.get() and self.var_model.get().strip():
                var.set(self.var_model.get().strip())
        cli = find_claude_cli()
        self.lbl_cc_cli.configure(text=("CLI：" + cli) if cli
                                  else "没找到 claude 命令（端到端验证会跳过）")
        self.lbl_cc_target.configure(text=f"配置文件：{CLAUDE_SETTINGS_PATH}")
        if ids:
            self.cc_autofill()

    def cc_autofill(self):
        models = (self.site_result or {}).get("models") or []
        if not models:
            self._set_status("还没有模型列表：先在主界面点「① 获取模型列表」")
            return
        m = suggest_claude_model_map(models, fallback=self.var_model.get().strip())
        n = 0
        for k, v in m.items():
            if k in self.cc_model_vars and v:
                self.cc_model_vars[k][0].set(v)
                n += 1
        self._set_status(f"已按模型名自动匹配 {n} 项，可手工改成你要的 id")

    def cc_check(self):
        if self.busy:
            self._set_status("正在测试中，请稍候…")
            return
        base = self.var_url.get().strip()
        if not base:
            self._set_status("请先填写 API 地址")
            return
        key = self.var_key.get().strip()
        model = self.cc_model_vars["default"][0].get().strip() or self.var_model.get().strip()
        self.stop_event.clear()
        self.busy = True
        self._set_action_busy(True)
        self._set_status("正在检测 Anthropic Messages 通道…")
        self._cc_log_append(f"=== 检测通道：{base.rstrip('/')}/v1/messages"
                            f"（模型 {model or '未指定'}）===")
        timeout, verify = self._timeout(), bool(self.var_verify.get())

        def work():
            try:
                res = check_anthropic_channel(base, key, model, timeout=timeout, verify=verify,
                                              log=self.cc_log_msg, stop_event=self.stop_event)
                self.q.put(("cc_check_done", res))
            except Exception as e:  # noqa: BLE001
                self.q.put(("fatal", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _render_cc_check(self, res: dict):
        self.cc_state = res
        tv = self.tv_cc
        tv.delete(*tv.get_children())

        def add(k, v, level="info"):
            tv.insert("", "end", values=(k, v), tags=(level,))

        add("结论", res.get("verdict", ""), res.get("verdict_level", "info"))
        add("端点", res.get("url", ""), "muted")
        add("认证方式", res.get("auth_style") or "-")
        add("HTTP 状态", str(res.get("status") or "-"))
        add("非流式耗时", f"{res['latency_ms']:.0f} ms" if res.get("latency_ms") else "-")
        add("流式首字", f"{res['first_ms']:.0f} ms" if res.get("first_ms") else "-",
            "ok" if res.get("stream_ok") else "warn")
        add("返回内容", (res.get("reply") or "-").replace("\n", " ")[:180])
        for t in res.get("tried", []):
            add(f"尝试 {t.get('auth_style')}",
                f"HTTP {t.get('status') or '-'}　{t.get('error') or ''}"[:220],
                "ok" if t.get("ok") else "warn")
        self._cc_log_append(f"检测结论：{res.get('verdict', '')}")
        self._set_status("通道检测完成")
        self._finish_busy()

    def cc_build_env(self):
        mm = {k: v[0].get().strip() for k, v in self.cc_model_vars.items()}
        default = mm.pop("default", "")
        style = (self.cc_state or {}).get("auth_style") or "bearer"
        env = build_claude_env(self.var_url.get().strip(), self.var_key.get().strip(),
                               style, mm, default)
        return env, style

    @staticmethod
    def _mask_env(env: dict) -> dict:
        return {k: (mask_key(v) if ("TOKEN" in k or "KEY" in k) else v)
                for k, v in (env or {}).items()}

    def cc_preview(self):
        env, style = self.cc_build_env()
        if not env.get("ANTHROPIC_BASE_URL"):
            self._set_status("请先填写 API 地址")
            return
        merged = merge_claude_settings(read_claude_settings(), env)
        self._cc_log_append("=== 预览：即将合并进 settings.json 的内容（Key 已打码）===")
        self._cc_log_append("认证头：" + ("x-api-key" if style == "x-api-key"
                                          else "Authorization: Bearer"))
        preview = dict(merged)
        if isinstance(preview.get("env"), dict):
            preview["env"] = self._mask_env(preview["env"])
        self._cc_log_append(json.dumps(preview, ensure_ascii=False, indent=2))
        self._set_status("预览已打印到下方日志（Key 已打码）")

    def cc_copy_oneliner(self):
        env, _ = self.cc_build_env()
        if not env.get("ANTHROPIC_BASE_URL") or not self.var_key.get().strip():
            self._set_status("需要地址与 Key 才能生成临时命令")
            return
        payload = json.dumps({"env": env}, ensure_ascii=False, separators=(",", ":"))
        cmd = f"claude --settings '{payload}' -p \"hi\""
        self.root.clipboard_clear()
        self.root.clipboard_append(cmd)
        self._cc_log_append("已复制临时命令（含真实 Key，别外发；适用于 Git Bash / PowerShell）：")
        self._cc_log_append(cmd)
        self._set_status("临时命令已复制到剪贴板")

    def cc_install(self):
        from tkinter import messagebox
        if self.busy:
            self._set_status("正在测试中，请稍候…")
            return
        # 硬门禁：必须先生成测试 + 通道检测都通过
        ready, why = self.cc_ready()
        if not ready:
            self._set_status(why)
            try:
                messagebox.showwarning("还不能接入 Claude Code", why)
            except Exception:
                pass
            return
        env, _style = self.cc_build_env()
        if not env.get("ANTHROPIC_BASE_URL"):
            self._set_status("请先填写 API 地址")
            return
        if not any(k in env for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")):
            if not messagebox.askyesno("没有 Key", "当前没填 Key，写进配置后 CLI 无法认证。仍要继续？"):
                return
        do_settings = bool(self.cc_write_settings.get())
        do_launcher = bool(self.cc_write_launcher.get())
        do_isolated = bool(self.cc_write_isolated.get())
        do_verify = bool(self.cc_run_verify.get())
        if not any((do_settings, do_launcher, do_isolated, do_verify)):
            self._set_status("没有勾选任何接入方式")
            return
        site = self.var_site.get()
        slug = slugify(site if site != ADHOC_SITE else "custom")
        outdir = CLAUDE_OUTPUT_DIR / f"claude-code-{slug}"
        timeout = max(60.0, self._timeout() * 6)
        self.stop_event.clear()
        self.busy = True
        self._set_action_busy(True)
        self._set_status("正在接入 Claude Code…")
        self._cc_log_append(f"=== 一键接入开始（站点：{site}）===")

        def work():
            summary = {"ok": True, "lines": []}

            def note(msg, ok=True):
                summary["lines"].append(msg)
                summary["ok"] = summary["ok"] and ok
                self.cc_log_msg(msg)

            try:
                if do_settings:
                    r = install_claude_settings(env, CLAUDE_SETTINGS_PATH)
                    if r["ok"]:
                        note(f"✔ 已写入 {r['path']}"
                             + (f"（备份：{r['backup']}）" if r["backup"] else "（原本没有文件）"))
                    else:
                        note(f"✘ 写配置失败：{r['error']}", ok=False)
                if do_isolated:
                    d = outdir / "config"
                    r = write_isolated_claude_config(env, d)
                    if r["ok"]:
                        note(f"✔ 隔离配置：{r['path']}")
                        lr = write_claude_launcher(env, d / "start-claude.bat", config_dir=str(d))
                        note(f"✔ 启动脚本：{lr['path']}" if lr["ok"]
                             else f"✘ 启动脚本失败：{lr['error']}", ok=lr["ok"])
                    else:
                        note(f"✘ 隔离配置失败：{r['error']}", ok=False)
                if do_launcher:
                    lp = outdir / f"start-claude-{slug}.bat"
                    r = write_claude_launcher(env, lp)
                    note(f"✔ 启动脚本：{r['path']}" if r["ok"]
                         else f"✘ 启动脚本失败：{r['error']}", ok=r["ok"])
                if do_verify:
                    note("… 正在跑端到端验证（claude -p PONG），首次可能要十几秒")
                    vr = verify_claude_cli(env, timeout=timeout, log=self.cc_log_msg)
                    if vr["ok"]:
                        note(f"✔ 端到端验证通过：result={vr['result']!r} "
                             f"terminal_reason={vr['terminal_reason'] or '-'} "
                             f"模型={list(vr['model_usage'].keys()) or '-'}")
                    else:
                        hint = ""
                        low = (vr["error"] or "").lower()
                        if vr["status"] == "404" or "model" in low or "not exist" in low:
                            hint = "　→ 大概率是模型映射不对：点「从当前站点模型列表自动匹配」再重试"
                        elif vr["status"] in ("401", "403"):
                            hint = "　→ 鉴权失败：确认这个 Key 对当前站点有效"
                        note(f"✘ 端到端验证未通过：{vr['error']}"
                             + (f"（api_error_status={vr['status']}）" if vr["status"] else "")
                             + hint, ok=False)
                self.q.put(("cc_install_done", summary))
            except Exception as e:  # noqa: BLE001
                self.q.put(("fatal", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _finish_cc_install(self, summary: dict):
        ok = bool(summary.get("ok"))
        self._cc_log_append("=== 一键接入完成 ===" if ok else "=== 一键接入完成（有失败项，见上）===")
        self._set_status("接入完成" if ok else "接入完成，但有失败项（见日志）")
        self._finish_busy()

    def cc_restore(self):
        from tkinter import messagebox
        baks = find_claude_backups()
        if not baks:
            self._set_status("没有找到备份文件")
            return
        newest = baks[0]
        if not messagebox.askyesno(
                "恢复备份",
                f"用这个备份覆盖当前配置？\n\n{newest}\n\n（当前文件会先另存一份再覆盖）"):
            return
        try:
            cur = CLAUDE_SETTINGS_PATH
            if cur.exists():
                ts = time.strftime("%Y%m%d-%H%M%S")
                shutil.copy2(cur, cur.with_name(cur.name + f".bak-apiprobe-{ts}"))
            shutil.copy2(newest, cur)
            self._cc_log_append(f"✔ 已从备份恢复：{newest}")
            self._set_status("已恢复备份")
        except Exception as e:  # noqa: BLE001
            self._set_status(f"恢复失败：{e}")

    def cc_open_dir(self):
        self._open_path(CLAUDE_SETTINGS_PATH.parent)

    # ---------------- Codex（OpenAI 格式）接入 ----------------
    def codex_log_msg(self, msg: str) -> None:
        self.q.put(("codex_log", msg))

    def _codex_log_append(self, msg: str) -> None:
        self.txt_codex.insert("end", f"{time.strftime('%H:%M:%S')}  {msg}\n")
        self.txt_codex.see("end")

    def goto_codex_tab(self):
        self.nb.select(self.tab_codex)
        self.codex_refresh_context()
        self._set_status("先点「检测通道（responses / chat）」，通过后再「一键接入 Codex」")

    def codex_refresh_context(self):
        """刷新可选模型、base_url、codex 路径。"""
        models = (self.site_result or {}).get("models") or []
        ids = [str(m.get("id") or "") for m in models if m.get("id")]
        self.cmb_codex_model.configure(values=ids)
        if not self.var_codex_model.get():
            self.var_codex_model.set(
                suggest_codex_model(models, fallback=self.var_model.get().strip()))
        self.var_codex_base.set(codex_base_url_for_gateway(self.var_url.get().strip()))
        cli = find_codex_cli()
        self.lbl_codex_cli.configure(text=("CLI：" + cli) if cli
                                     else "没找到 codex（端到端验证会跳过）")

    def codex_autofill(self):
        models = (self.site_result or {}).get("models") or []
        if not models:
            self._set_status("还没有模型列表：先在主界面点「① 获取模型列表」")
            return
        pick = suggest_codex_model(models, fallback=self.var_model.get().strip())
        if pick:
            self.var_codex_model.set(pick)
            self._set_status(f"已选默认模型：{pick}")

    def codex_ready(self) -> tuple[bool, str]:
        """门禁：OpenAI 通道探测通过即可接入 —— 它本身就是一次真实的生成（拿到 PONG）。

        第 ② 步生成测试只作提示：它走工具自己挑的协议路径，Codex 走的是
        /v1/responses 或 /v1/chat/completions，某些只实现 responses 的网关两者并不同时可用。
        """
        ch = self.codex_state or {}
        gen_ok = bool((self.site_result or {}).get("ok"))
        if ch.get("ok"):
            return True, ("" if gen_ok else
                          "注意：第 ② 步生成测试未通过，但 Codex 用的通道已实测可用")
        if any(t.get("model_not_found") for t in ch.get("tried") or []):
            return False, "通道路径通了但模型不可用：换一个模型后再点「检测通道」"
        if ch:
            return False, f"OpenAI 通道未通过：{ch.get('verdict') or '未知原因'}"
        return False, "还没检测 OpenAI 通道：先点「检测通道（responses / chat）」或「▶ 全流程」"

    def _refresh_codex_gate(self) -> None:
        ok, why = self.codex_ready()
        try:
            self.btn_codex_install.configure(state="normal" if ok else "disabled")
            self.lbl_codex_gate.configure(
                text=(("已解锁，可以接入" + (f"（{why}）" if why else "")) if ok
                      else f"未解锁：{why}"),
                foreground=COLORS["ok"] if ok else COLORS["muted"])
        except Exception:
            pass

    def codex_check(self):
        if self.busy:
            self._set_status("正在测试中，请稍候…")
            return
        base = self.var_url.get().strip()
        if not base:
            self._set_status("请先填写 API 地址")
            return
        key = self.var_key.get().strip()
        model = self.var_codex_model.get().strip() or self.var_model.get().strip()
        self.stop_event.clear()
        self.busy = True
        self._set_action_busy(True)
        self._set_status("正在检测 OpenAI 通道…")
        self._codex_log_append(f"=== 检测 OpenAI 通道：{codex_base_url_for_gateway(base)}"
                               f"（模型 {model or '未指定'}）===")
        timeout, verify = self._timeout(), bool(self.var_verify.get())

        def work():
            try:
                res = check_openai_channel(base, key, model, timeout=timeout, verify=verify,
                                           log=self.codex_log_msg, stop_event=self.stop_event)
                self.q.put(("codex_check_done", res))
            except Exception as e:  # noqa: BLE001
                self.q.put(("fatal", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _render_codex_check(self, res: dict):
        self.codex_state = res
        tv = self.tv_codex
        tv.delete(*tv.get_children())

        def add(k, v, level="info"):
            tv.insert("", "end", values=(k, v), tags=(level,))

        add("结论", res.get("verdict", ""), res.get("verdict_level", "info"))
        add("wire_api（要写进配置）", res.get("wire_api") or "-",
            "ok" if res.get("wire_api") else "warn")
        add("base_url（Codex 用）", res.get("base_url", ""), "muted")
        add("HTTP 状态", str(res.get("status") or "-"))
        add("耗时", f"{res['latency_ms']:.0f} ms" if res.get("latency_ms") else "-")
        add("返回内容", (res.get("reply") or "-").replace("\n", " ")[:180])
        for t in res.get("tried", []):
            add(f"尝试 {t.get('wire_api')}",
                f"HTTP {t.get('status') or '-'}　{t.get('error') or ''}"[:220],
                "ok" if t.get("ok") else "warn")
        if res.get("wire_api"):
            self.var_codex_wire.set(res["wire_api"])
        self._codex_log_append(f"检测结论：{res.get('verdict', '')}")
        self._set_status("OpenAI 通道检测完成")
        self._finish_busy()

    def codex_params(self):
        model = self.var_codex_model.get().strip() or self.var_model.get().strip()
        base_url = self.var_codex_base.get().strip() or \
            codex_base_url_for_gateway(self.var_url.get().strip())
        env_key = self.var_codex_envkey.get().strip() or "CODEX_API_KEY"
        wire = self.var_codex_wire.get().strip()
        if wire not in ("responses", "chat"):
            wire = (self.codex_state or {}).get("wire_api") or "responses"
        return model, base_url, env_key, wire

    def codex_preview(self):
        model, base_url, env_key, wire = self.codex_params()
        if not base_url or not model:
            self._set_status("需要地址与模型才能预览")
            return
        existing = ""
        try:
            if CODEX_CONFIG_PATH.exists():
                existing = CODEX_CONFIG_PATH.read_text(encoding="utf-8")
        except Exception:
            pass
        merged = merge_codex_config(existing, model, base_url, env_key,
                                    self.var_site.get() or "API Probe", wire)
        self._codex_log_append("=== 预览：合并后的 ~/.codex/config.toml（只显示前 30 行）===")
        for line in merged.splitlines()[:30]:
            self._codex_log_append(line)
        self._set_status("预览已打印到下方日志")

    def codex_copy_oneliner(self):
        model, base_url, env_key, wire = self.codex_params()
        if not base_url or not model or not self.var_key.get().strip():
            self._set_status("需要地址、模型与 Key 才能生成临时命令")
            return
        cmd = (f'codex exec -c model="{model}" '
               f'-c model_provider="{CODEX_PROVIDER_ID}" '
               f'-c model_providers.{CODEX_PROVIDER_ID}.base_url="{base_url}" '
               f'-c model_providers.{CODEX_PROVIDER_ID}.wire_api="{wire}" '
               f'-c model_providers.{CODEX_PROVIDER_ID}.env_key="{env_key}" '
               f'"hi"')
        self.root.clipboard_clear()
        self.root.clipboard_append(cmd)
        self._codex_log_append("已复制临时命令（不含 Key；运行前先设好 "
                               f"{env_key} 环境变量）：")
        self._codex_log_append(cmd)
        self._set_status("临时命令已复制到剪贴板")

    def codex_install(self):
        from tkinter import messagebox
        if self.busy:
            self._set_status("正在测试中，请稍候…")
            return
        ready, why = self.codex_ready()
        if not ready:
            self._set_status(why)
            try:
                messagebox.showwarning("还不能接入 Codex", why)
            except Exception:
                pass
            return
        model, base_url, env_key, wire = self.codex_params()
        if not model or not base_url:
            self._set_status("缺少模型或地址")
            return
        key = self.var_key.get().strip()
        do_config = bool(self.codex_write_config.get())
        do_env = bool(self.codex_write_env.get())
        do_launcher = bool(self.codex_write_launcher.get())
        do_verify = bool(self.codex_run_verify.get())
        if not any((do_config, do_env, do_launcher, do_verify)):
            self._set_status("没有勾选任何接入方式")
            return
        if (do_env or do_launcher) and not key:
            if not messagebox.askyesno("没有 Key", "要写 .env / 生成启动脚本，但没有填 Key。继续？"):
                return
        site = self.var_site.get()
        slug = slugify(site if site != ADHOC_SITE else "custom")
        outdir = CLAUDE_OUTPUT_DIR / f"codex-{slug}"
        cli = find_codex_cli()
        timeout = max(60.0, self._timeout() * 6)
        self.stop_event.clear()
        self.busy = True
        self._set_action_busy(True)
        self._set_status("正在接入 Codex…")
        self._codex_log_append(f"=== 一键接入 Codex 开始（站点：{site}，模型：{model}）===")

        def work():
            summary = {"ok": True, "lines": []}

            def note(msg, ok=True):
                summary["lines"].append(msg)
                summary["ok"] = summary["ok"] and ok
                self.codex_log_msg(msg)

            try:
                if do_config:
                    r = install_codex_config(model, base_url, env_key,
                                             site or "API Probe", wire)
                    if r["ok"]:
                        old = (f"（原 model={r['old_model']} / provider={r['old_provider']}，"
                               f"已备份）" if r["old_model"] or r["old_provider"] else
                               "（原本没有配置文件）")
                        note(f"✔ 已写入 {r['path']}{old}")
                    else:
                        note(f"✘ 写 config.toml 失败：{r['error']}", ok=False)
                if do_env:
                    r = install_codex_env_key(env_key, key)
                    note(f"✔ 已把 Key 写进 {r['path']}（变量名 {env_key}）"
                         + (f"，备份 {r['backup']}" if r["backup"] else "")
                         if r["ok"] else f"✘ 写 .env 失败：{r['error']}", ok=r["ok"])
                if do_launcher:
                    lp = outdir / f"start-codex-{slug}.bat"
                    r = write_codex_launcher(env_key, key, lp, cli)
                    note(f"✔ 启动脚本：{r['path']}" if r["ok"]
                         else f"✘ 启动脚本失败：{r['error']}", ok=r["ok"])
                if do_verify:
                    note("… 正在跑端到端验证（codex exec PONG），首次可能要几十秒")
                    vr = verify_codex_cli(model, base_url, env_key, key, cli_path=cli,
                                          timeout=timeout, log=self.codex_log_msg, wire_api=wire)
                    if vr["ok"]:
                        note(f"✔ 端到端验证通过：{vr['result'].strip()[:120]!r}")
                    else:
                        note(f"✘ 端到端验证未通过：{vr['error']}", ok=False)
                self.q.put(("codex_install_done", summary))
            except Exception as e:  # noqa: BLE001
                self.q.put(("fatal", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _finish_codex_install(self, summary: dict):
        ok = bool(summary.get("ok"))
        self._codex_log_append("=== 一键接入 Codex 完成 ===" if ok
                               else "=== 一键接入 Codex 完成（有失败项，见上）===")
        self._set_status("Codex 接入完成" if ok else "Codex 接入完成，但有失败项（见日志）")
        self._finish_busy()

    def codex_restore(self):
        from tkinter import messagebox
        baks = find_codex_backups()
        if not baks:
            self._set_status("没有找到 config.toml 的备份")
            return
        newest = baks[0]
        if not messagebox.askyesno("恢复备份",
                                   f"用这个备份覆盖 ~/.codex/config.toml？\n\n{newest}\n\n"
                                   "（当前文件会先另存一份再覆盖）"):
            return
        try:
            if CODEX_CONFIG_PATH.exists():
                ts = time.strftime("%Y%m%d-%H%M%S")
                shutil.copy2(CODEX_CONFIG_PATH,
                             CODEX_CONFIG_PATH.with_name(CODEX_CONFIG_PATH.name
                                                         + f".bak-apiprobe-{ts}"))
            shutil.copy2(newest, CODEX_CONFIG_PATH)
            self._codex_log_append(f"✔ 已从备份恢复：{newest}")
            self._set_status("已恢复备份")
        except Exception as e:  # noqa: BLE001
            self._set_status(f"恢复失败：{e}")

    def codex_open_dir(self):
        self._open_path(CODEX_HOME)

    # ---------------- 小工具 ----------------
    def _toggle_key(self):
        self.ent_key.configure(show="" if self.var_show.get() else "*")

    # ---------------- 站点管理（多个中转站） ----------------
    def _site_index(self, name: str) -> int:
        for i, s in enumerate(self.sites):
            if s.get("name") == name:
                return i
        return -1

    def _record_for(self, name: str):
        """按名字取站点记录；「自定义」返回 adhoc 那一格。"""
        if name == ADHOC_SITE:
            return self.adhoc
        i = self._site_index(name)
        return self.sites[i] if i >= 0 else None

    def _current_record(self):
        return self._record_for(self.var_site.get())

    def _stash_current_fields(self) -> None:
        """把输入框里当前的 地址/Key/模型 写回「当前装载的那一格」。

        必须是 _active_site（上一次真正装载的），不能用下拉框的当前值 —— 否则
        切换站点的瞬间会把旧站点的内容覆盖到新站点上。
        """
        rec = self._record_for(self._active_site)
        if rec is None:
            return
        rec["url"] = self.var_url.get().strip()
        rec["key"] = self.var_key.get().strip()
        rec["model"] = self.var_model.get().strip()

    def _apply_record(self, rec: dict) -> None:
        self.var_url.set(rec.get("url", "") or "")
        self.var_key.set(rec.get("key", "") or "")
        self.var_model.set(rec.get("model", "") or "")

    def _refresh_site_combo(self) -> None:
        self.cmb_site.configure(values=[ADHOC_SITE] + [s["name"] for s in self.sites])
        is_real = self._current_record() is not None
        self.btn_edit_site.configure(state="normal" if is_real else "disabled")
        self.btn_del_site.configure(state="normal" if is_real else "disabled")

    def _invalidate_results(self) -> None:
        """站点/Key 变了：上一次拉到的模型列表与测试结果作废。"""
        self.site_result = None
        self.last_result = None
        self.cc_state = {}
        self.codex_state = {}
        self._site_signature = ""
        self.btn_gen.configure(state="disabled")
        self.lbl_picked.configure(text="（未选择）")
        self.tv_ov.delete(*self.tv_ov.get_children())
        self.tv_models.delete(*self.tv_models.get_children())
        self.lbl_models.configure(text="（尚未获取列表）")
        self._refresh_cc_gate()               # 站点一变，两个接入门禁立刻关上
        self._refresh_codex_gate()
        try:
            self.nb.select(0)
        except Exception:
            pass

    def _select_site(self, name: str) -> None:
        self.var_site.set(name)
        self._on_site_change()

    def _on_site_change(self, _evt=None) -> None:
        """切换站点：先把旧站点输入框内容存回去，再装载新站点的地址/Key/模型。"""
        self._stash_current_fields()
        name = self.var_site.get()
        rec = self._record_for(name)
        if rec is None:
            self.var_site.set(ADHOC_SITE)
            name, rec = ADHOC_SITE, self.adhoc
        self._active_site = name
        self._apply_record(rec)
        self._invalidate_results()
        self._refresh_site_combo()
        self._save_config()
        if name == ADHOC_SITE:
            self._set_status("已切到「自定义（临时）」：这一格的地址/Key 也会各自记住")
        elif not self.var_remember.get() and not rec.get("key"):
            self._set_status(f"已切换到「{name}」：未勾选「记住 Key」，请粘贴 Key 后再测试")
        else:
            self._set_status(f"已切换到「{name}」")

    def _add_site_record(self, name: str, url: str, key: str = "", model: str = "",
                         select: bool = True) -> bool:
        name = (name or "").strip()
        if not name or name == ADHOC_SITE or self._site_index(name) >= 0:
            return False
        self.sites.append({"name": name, "url": (url or "").strip(),
                           "key": (key or "").strip(), "model": (model or "").strip()})
        self._refresh_site_combo()
        if select:
            self._select_site(name)
        return True

    def _update_site_record(self, old_name: str, name: str, url: str, key: str,
                            model: str) -> bool:
        i = self._site_index(old_name)
        if i < 0:
            return False
        name = (name or "").strip()
        if not name or name == ADHOC_SITE:
            return False
        if name != old_name and self._site_index(name) >= 0:
            return False
        was_active = self._active_site == old_name
        self.sites[i] = {"name": name, "url": (url or "").strip(),
                         "key": (key or "").strip(), "model": (model or "").strip()}
        self._refresh_site_combo()
        if was_active:
            self._active_site = name
            self.var_site.set(name)
            self._apply_record(self.sites[i])
            self._invalidate_results()
        return True

    def _delete_site_record(self, name: str) -> bool:
        i = self._site_index(name)
        if i < 0:
            return False
        was_active = self._active_site == name
        self.sites.pop(i)
        self._refresh_site_combo()
        if was_active:
            self._select_site(ADHOC_SITE)
        return True

    def _site_dialog(self, title: str, initial: dict):
        """弹出添加/编辑站点对话框，返回 dict 或 None。"""
        tk, ttk = self.tk, self.ttk
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()
        out = {"value": None}

        body = ttk.Frame(dlg)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        v_name = tk.StringVar(value=initial.get("name", "") or "")
        v_url = tk.StringVar(value=initial.get("url", "") or "")
        v_key = tk.StringVar(value=initial.get("key", "") or "")
        v_model = tk.StringVar(value=initial.get("model", "") or "")

        def add_row(r, label, var, width=54, show=None):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="w", pady=4)
            ent = ttk.Entry(body, textvariable=var, width=width, font=self.mono_font, show=show)
            ent.grid(row=r, column=1, sticky="we", padx=(8, 0), pady=4)
            return ent

        e_name = add_row(0, "站点名称", v_name, 30)
        ttk.Label(body, text="常用预设").grid(row=1, column=0, sticky="w", pady=4)
        v_preset = tk.StringVar(value=QUICK_PRESETS[0][0])
        cmb = ttk.Combobox(body, textvariable=v_preset, values=[n for n, _ in QUICK_PRESETS],
                           state="readonly", width=30)
        cmb.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=4)

        def apply_preset(_e=None):
            for n, u in QUICK_PRESETS:
                if n == v_preset.get() and u:
                    v_url.set(u)
                    if not v_name.get().strip():
                        v_name.set(n)
                    break
        cmb.bind("<<ComboboxSelected>>", apply_preset)

        add_row(2, "API 地址", v_url)
        add_row(3, "API Key", v_key, show="*")
        add_row(4, "默认模型", v_model, 30)
        ttk.Label(body, text="（默认模型可留空，之后在模型列表里挑也行）",
                  style="Hint.TLabel").grid(row=4, column=2, sticky="w", padx=(6, 0))
        hint = ("已勾选「记住 Key」：Key 会加密保存到本机，切换站点自动带出。"
                if self.var_remember.get() else
                "注意：当前未勾选「记住 Key」，这里填的 Key 不会写盘（只在本次运行内有效）。")
        ttk.Label(body, text=hint, style="Hint.TLabel", wraplength=430).grid(
            row=5, column=1, sticky="w", pady=(2, 0))
        err = ttk.Label(body, text="", foreground=COLORS["risk"])
        err.grid(row=6, column=1, sticky="w")

        def ok(_e=None):
            name = v_name.get().strip()
            url = v_url.get().strip()
            if not name:
                err.configure(text="请填写站点名称")
                return
            if name == ADHOC_SITE:
                err.configure(text="这个名称被内置项占用了，换一个")
                return
            if not url:
                err.configure(text="请填写 API 地址")
                return
            if name != initial.get("name") and self._site_index(name) >= 0:
                err.configure(text=f"站点名「{name}」已存在")
                return
            out["value"] = {"name": name, "url": url, "key": v_key.get().strip(),
                            "model": v_model.get().strip()}
            dlg.destroy()

        bar = ttk.Frame(body)
        bar.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(bar, text="确定", command=ok).pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="left")
        dlg.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        e_name.focus_set()
        self.root.wait_window(dlg)
        return out["value"]

    def add_site(self):
        init = {"name": "", "url": self.var_url.get().strip(),
                "key": self.var_key.get().strip(), "model": self.var_model.get().strip()}
        data = self._site_dialog("添加站点", init)
        if not data:
            return
        if self._add_site_record(data["name"], data["url"], data["key"], data["model"]):
            self._save_config()
            self._set_status(f"已添加站点「{data['name']}」，可用它做第 ① 步")

    def edit_site(self):
        rec = self._current_record()
        if rec is None:
            self._set_status("请先在下拉里选择一个已保存的站点（「自定义」不能编辑）")
            return
        self._stash_current_fields()
        old_name = rec["name"]
        data = self._site_dialog(f"编辑站点 - {old_name}", dict(rec))
        if not data:
            return
        if self._update_site_record(old_name, data["name"], data["url"], data["key"],
                                    data["model"]):
            self._save_config()
            self._set_status(f"已更新站点「{data['name']}」")

    def delete_site(self):
        from tkinter import messagebox
        rec = self._current_record()
        if rec is None:
            self._set_status("「自定义（临时）」不能删除，请先选中一个已保存的站点")
            return
        if not messagebox.askyesno("删除站点", f"确定删除站点「{rec['name']}」？"):
            return
        name = rec["name"]
        if self._delete_site_record(name):
            self._save_config()
            self._set_status(f"已删除站点「{name}」")

    def import_sites_to_batch(self):
        """把所有已保存的站点导入批量测试框，一键巡检多个中转站。"""
        self._stash_current_fields()
        lines = []
        for s in self.sites:
            parts = [s.get("url", ""), s.get("key", ""), s.get("model", "")]
            while parts and not parts[-1]:
                parts.pop()
            if parts and parts[0]:
                lines.append(" | ".join(parts))
        if self.var_url.get().strip() and self._active_site == ADHOC_SITE:
            parts = [self.var_url.get().strip(), self.var_key.get().strip(),
                     self.var_model.get().strip()]
            while parts and not parts[-1]:
                parts.pop()
            lines.insert(0, " | ".join(parts))
        if not lines:
            self._set_status("站点列表是空的，先点「＋ 添加站点」")
            return
        self.txt_batch.delete("1.0", "end")
        self.txt_batch.insert("1.0", "\n".join(lines) + "\n")
        self._save_config()
        self._set_status(f"已把 {len(lines)} 个站点导入批量列表")

    def _log(self, msg: str):
        self.q.put(("log", msg))

    def _log_clear(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def _log_append(self, msg: str):
        self.txt_log.configure(state="normal")
        ts = time.strftime("%H:%M:%S")
        self.txt_log.insert("end", f"{ts}  {msg}\n")
        if int(self.txt_log.index("end-1c").split(".")[0]) > 4000:
            self.txt_log.delete("1.0", "500.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _set_status(self, text: str):
        self.lbl_status.configure(text=text)

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.txt_log.get("1.0", "end-1c"))
        self._set_status("日志已复制")

    def copy_verdict(self):
        if not self.last_result:
            self._set_status("还没有测试结果")
            return
        res = self.last_result
        lines = [f"地址: {res.get('base')}", f"结论: {res.get('verdict')}",
                 f"协议: {res.get('protocol_label')}", f"模型数: {res.get('model_count', 0)}",
                 f"请求模型: {res.get('used_model')}", f"返回模型: {res.get('returned_model')}",
                 f"首字延迟: {res.get('first_token_ms')} ms", f"指纹: {res.get('fingerprint')}"]
        lines += [f"[{lvl}] {t}" for lvl, t in
                  (res.get("warnings", []) + res.get("gen_warnings", []))]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        self._set_status("结论已复制到剪贴板")

    def copy_models(self):
        ids = [self.tv_models.set(i, "id") for i in self.tv_models.get_children()]
        if not ids:
            self._set_status("模型列表为空")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(ids))
        self._set_status(f"已复制 {len(ids)} 个模型名")

    # ---------------- 配置持久化 ----------------
    # 保存位置：~/.api_probe.json（config_version 3）
    #   明文：各站点 名称/地址/默认模型、超时、各开关、窗口大小
    #   加密：各站点的 Key 与批量列表 —— 只有勾选「记住 Key」才写盘，用 Windows DPAPI 加密
    def _load_config(self):
        try:
            if not CONFIG_PATH.exists():
                return
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                return
        except Exception:
            return
        try:
            self.var_timeout.set(str(cfg.get("timeout", int(DEFAULT_TIMEOUT))))
            self.var_verify.set(bool(cfg.get("verify_tls", True)))
            self.var_deep.set(bool(cfg.get("deep_check", True)))
            self.var_only_main.set(bool(cfg.get("only_mainstream", False)))
            self.var_workers.set(str(cfg.get("workers", 2)))
            geo = str(cfg.get("geometry") or "")
            if re.fullmatch(r"\d+x\d+([+-]\d+[+-]\d+)?", geo):
                self.root.geometry(geo)
        except Exception:
            pass
        remember = bool(cfg.get("remember_key", False))
        self.var_remember.set(remember)

        # 站点列表
        self.sites = []
        for s in cfg.get("sites") or []:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            if not name or name == ADHOC_SITE:
                continue
            key = ""
            if remember and s.get("key"):
                key = decode_secret(str(s["key"]), bool(s.get("key_encrypted", False)))
            self.sites.append({"name": name, "url": str(s.get("url") or ""),
                               "model": str(s.get("model") or ""), "key": key})
        # 「自定义（临时）」那一格
        ad = cfg.get("adhoc") if isinstance(cfg.get("adhoc"), dict) else {}
        adhoc_key = ""
        if remember:
            blob = str(ad.get("key") or "") or str(cfg.get("key") or "")
            enc = bool(ad.get("key_encrypted", cfg.get("key_encrypted", False)))
            adhoc_key = decode_secret(blob, enc)
        self.adhoc = {"name": ADHOC_SITE,
                      "url": str(ad.get("url") or cfg.get("url") or ""),
                      "model": str(ad.get("model") or cfg.get("model") or ""),
                      "key": adhoc_key}
        # v1/v2 -> v3 迁移：老配置只有一个 url，把它变成一个站点，免得用户以为丢了
        if not self.sites and int(cfg.get("config_version") or 1) < 3 and self.adhoc["url"]:
            self.sites.append({"name": "默认站点", "url": self.adhoc["url"],
                               "model": self.adhoc["model"], "key": self.adhoc["key"]})
        # 当前选中的站点
        names = [ADHOC_SITE] + [s["name"] for s in self.sites]
        cur = str(cfg.get("current_site") or "")
        if cur not in names:
            cur = self.sites[0]["name"] if self.sites else ADHOC_SITE
        self._active_site = cur
        self.var_site.set(cur)
        self._refresh_site_combo()
        self._apply_record(self._record_for(cur) or self.adhoc)

        if remember:
            batch = decode_secret(str(cfg.get("batch_text") or ""),
                                  bool(cfg.get("batch_encrypted", False)))
            if batch:
                self.txt_batch.delete("1.0", "end")
                self.txt_batch.insert("1.0", batch)

    def _save_config(self):
        try:
            self._stash_current_fields()
            remember = bool(self.var_remember.get())
            sites_out = []
            for s in self.sites:
                rec = {"name": s.get("name", ""), "url": s.get("url", ""),
                       "model": s.get("model", "")}
                if remember and s.get("key"):
                    rec["key"], rec["key_encrypted"] = encode_secret(s["key"])
                sites_out.append(rec)
            adhoc_out = {"url": self.adhoc.get("url", ""), "model": self.adhoc.get("model", "")}
            if remember and self.adhoc.get("key"):
                adhoc_out["key"], adhoc_out["key_encrypted"] = encode_secret(self.adhoc["key"])
            batch_blob, batch_enc = ("", True)
            if remember:
                batch_blob, batch_enc = encode_secret(
                    self.txt_batch.get("1.0", "end-1c").strip())
            cfg = {
                "config_version": 3,
                "current_site": self.var_site.get(),
                "sites": sites_out,
                "adhoc": adhoc_out,
                "timeout": self._timeout(),
                "verify_tls": bool(self.var_verify.get()),
                "deep_check": bool(self.var_deep.get()),
                "only_mainstream": bool(self.var_only_main.get()),
                "workers": self._workers(),
                "remember_key": remember,
                "batch_text": batch_blob,
                "batch_encrypted": batch_enc,
            }
            # 顶层再冗余一份「当前站点」，方便人肉查看，也让旧版本仍能读到地址
            cfg["url"] = self.var_url.get().strip()
            cfg["model"] = self.var_model.get().strip()
            if remember:
                cfg["key"], cfg["key_encrypted"] = encode_secret(self.var_key.get().strip())
            else:
                cfg["key"], cfg["key_encrypted"] = "", True
            try:
                if self.root.winfo_viewable():
                    cfg["geometry"] = self.root.geometry()
            except Exception:
                pass
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except Exception:
            pass

    def _schedule_save(self, delay: int = 1200):
        """改动后延迟写盘，避免每敲一个键就写一次文件。"""
        try:
            if self._save_job:
                self.root.after_cancel(self._save_job)
            self._save_job = self.root.after(delay, self._save_now)
        except Exception:
            self._save_job = None

    def _save_now(self):
        self._save_job = None
        self._save_config()

    def _on_remember_toggle(self):
        self._save_config()          # 立即生效：取消勾选会同时清掉文件里的 Key
        if self.var_remember.get():
            extra = "" if dpapi_available() else "（本机不支持加密，将以明文保存）"
            self._set_status(f"已记住 Key，保存在 {CONFIG_PATH}{extra}")
        else:
            self._set_status(f"已取消记住，配置文件里的 Key 已清空（{CONFIG_PATH}）")

    def _clear_all_saved_keys(self):
        """清空所有站点（含自定义格）已保存的 Key，并把文件里的 Key 抹掉。"""
        for s in self.sites:
            s["key"] = ""
        self.adhoc["key"] = ""
        self.var_key.set("")
        self._save_config()

    def clear_saved_key(self):
        from tkinter import messagebox
        n = sum(1 for s in self.sites if s.get("key")) + (1 if self.adhoc.get("key") else 0)
        if not messagebox.askyesno("清除已存 Key",
                                   f"将清空 {n} 处已保存的 Key（站点列表与当前输入框），确定？"):
            return
        self._clear_all_saved_keys()
        self._set_status(f"已清空保存的 Key：{CONFIG_PATH}")

    def _on_close(self):
        self.stop_event.set()
        self._save_config()
        self.root.destroy()

    def _timeout(self) -> float:
        try:
            return max(3.0, float(self.var_timeout.get()))
        except Exception:
            return DEFAULT_TIMEOUT

    def _workers(self) -> int:
        try:
            return max(1, min(8, int(self.var_workers.get())))
        except Exception:
            return 2

    # ---------------- 第 1 步：拉取模型列表 ----------------
    def fetch_models(self):
        if self.busy:
            self._set_status("正在测试中，请稍候…")
            return
        url = self.var_url.get().strip()
        if not url:
            self._set_status("请先填写 API 地址")
            return
        key = self.var_key.get().strip()
        self.stop_event.clear()
        self._save_config()
        self._log_clear()
        self._set_status("第 1 步：正在拉取模型列表…")
        self.busy = True
        self.btn_list.configure(state="disabled")
        self.btn_gen.configure(state="disabled")
        self.btn_batch.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.tv_ov.delete(*self.tv_ov.get_children())
        self.tv_models.delete(*self.tv_models.get_children())
        self.site_result = None
        self.last_result = None
        self.lbl_picked.configure(text="（未选择）")
        self.lbl_summary.configure(text=f"正在拉取 {url} 的模型列表 …")
        self._log_append(f"=== 第 1 步：获取模型列表 {url} （Key: {mask_key(key)}） ===")
        # 注意：Tk 变量只能在主线程读，先取好值再交给 worker 线程
        timeout, verify = self._timeout(), bool(self.var_verify.get())

        def work():
            try:
                res = probe_site(url, key, timeout=timeout, verify=verify, log=self._log,
                                 stop_event=self.stop_event)
                self.q.put(("site_done", res))
            except Exception as e:  # noqa: BLE001
                self.q.put(("fatal", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 第 2 步：用人工选中的模型发测试 ----------------
    def run_generation(self):
        if self.busy:
            self._set_status("正在测试中，请稍候…")
            return
        if not self.site_result:
            self._set_status("请先点「① 获取模型列表」")
            return
        model = self.var_model.get().strip()
        if not model:
            self._set_status("请先在「模型列表」里选一个模型（或在上方「指定模型」里手写一个）")
            self.nb.select(1)
            return
        site = self.site_result
        key = self.var_key.get().strip()
        self.stop_event.clear()
        self.busy = True
        self.btn_list.configure(state="disabled")
        self.btn_gen.configure(state="disabled")
        self.btn_batch.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._set_status(f"第 2 步：正在用 {model} 发测试…")
        self._log_append(f"=== 第 2 步：生成测试，模型 = {model} ===")
        # Tk 变量只在主线程读
        timeout = self._timeout()
        verify = bool(self.var_verify.get())
        deep = bool(self.var_deep.get())

        def work():
            try:
                res = probe_generate(site, key, model, timeout=timeout, verify=verify,
                                     deep=deep, log=self._log, stop_event=self.stop_event)
                self.q.put(("gen_done", res))
            except Exception as e:  # noqa: BLE001
                self.q.put(("fatal", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self._set_status("正在停止…")
        self._log_append("用户请求停止")

    # ---------------- 批量测试 ----------------
    def start_batch(self):
        if self.busy:
            self._set_status("正在测试中，请稍候…")
            return
        items = parse_batch_lines(self.txt_batch.get("1.0", "end"))
        if not items:
            self._set_status("批量输入为空")
            return
        self.stop_event.clear()
        self._clear_batch()
        self.busy = True
        self.btn_batch.configure(state="disabled")
        self.btn_list.configure(state="disabled")
        self.btn_gen.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._log_append(f"=== 批量测试开始，共 {len(items)} 个站点 ===")
        workers = self._workers()
        self._save_config()                       # 顺手把批量列表存下来
        # Tk 变量只在主线程读
        timeout = self._timeout()
        verify = bool(self.var_verify.get())
        deep = bool(self.var_deep.get())

        def work():
            done = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(probe, it["url"], it["key"], it["model"], timeout,
                                  verify, deep, self._log, self.stop_event): it for it in items}
                for fut in concurrent.futures.as_completed(futs):
                    it = futs[fut]
                    try:
                        res = fut.result()
                    except Exception as e:  # noqa: BLE001
                        res = {"input": it["url"], "base": it["url"], "verdict":
                               f"异常: {type(e).__name__}: {e}", "verdict_level": "error",
                               "ok": False, "models": [], "warnings": []}
                    done += 1
                    self.q.put(("batch_row", (res, done, len(items))))

        threading.Thread(target=work, daemon=True).start()

    def _clear_batch(self):
        self.tv_batch.delete(*self.tv_batch.get_children())
        self.batch_rows = []

    # ---------------- 事件泵 ----------------
    def _pump(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._log_append(str(data))
                elif kind == "site_done":
                    self._render_site(data)
                elif kind == "gen_done":
                    self._render_generation(data)
                elif kind == "cc_log":
                    self._cc_log_append(str(data))
                elif kind == "cc_check_done":
                    self._render_cc_check(data)
                elif kind == "cc_install_done":
                    self._finish_cc_install(data)
                elif kind == "codex_log":
                    self._codex_log_append(str(data))
                elif kind == "codex_check_done":
                    self._render_codex_check(data)
                elif kind == "codex_install_done":
                    self._finish_codex_install(data)
                elif kind == "flow_done":
                    self._render_flow(data)
                elif kind == "batch_row":
                    res, done, total = data
                    self._append_batch_row(res, done, total)
                elif kind == "batch_done":
                    self._finish_batch()
                elif kind == "fatal":
                    self._log_append(f"致命错误: {data}")
                    self._set_status("出错，详见日志")
                    self._finish_busy()
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def _finish_busy(self):
        self.busy = False
        self.btn_list.configure(state="normal")
        self.btn_batch.configure(state="normal")
        self.btn_gen.configure(state="normal" if self.site_result else "disabled")
        self.btn_stop.configure(state="disabled")
        self._set_action_busy(False)

    # ---------------- 渲染 ----------------
    def _render_site(self, res: dict):
        """第 1 步完成：把站点信息铺开，并把推荐模型高亮出来，等人工确认。"""
        self.site_result = res
        self.last_result = res
        self._site_signature = self._signature()
        self._render_overview(res)
        self._refresh_models()
        n = len(res.get("models") or [])
        self.lbl_models.configure(text=f"共 {n} 个模型")
        auto = choose_default_model(res.get("models") or [], self.var_model.get().strip())
        if auto:
            self._select_model_in_tree(auto)
        if res.get("ok_site"):
            self.nb.select(1)
            self._set_status("第 1 步完成 —— 请选择模型")
            self.lbl_summary.configure(
                text=f"第 1 步完成：{n} 个模型（已定位到推荐模型 {auto}）。"
                     "点「② 对选中模型发测试」或双击某行直接测。")
        else:
            self.nb.select(0)
            self._set_status("第 1 步结束，详见概览")
            self.lbl_summary.configure(
                text=f"{res.get('verdict', '')}（可手工在上方「指定模型」填模型名后做第 2 步）")
        self._log_append(f"=== 第 1 步完成：{res.get('verdict', '')} ===")
        self._finish_busy()
        try:
            self.cc_refresh_context()          # 顺手把候选模型喂给两个 CLI 接入页
            self.codex_refresh_context()
        except Exception:
            pass

    def _render_generation(self, res: dict):
        """第 2 步完成：刷新概览并切回概览页看结论。"""
        self.site_result = res
        self.last_result = res
        self._render_overview(res)
        self.nb.select(0)
        self._set_status("第 2 步完成")
        self.lbl_summary.configure(text=f"{res.get('base', '')} → {res.get('verdict', '')}")
        self._log_append(f"=== 第 2 步完成：{res.get('verdict', '')} ===")
        self._finish_busy()

    def _render_overview(self, res: dict):
        tv = self.tv_ov
        tv.delete(*tv.get_children())

        def add(item, value, level="info", head=False):
            tv.insert("", "end", values=(item, value), tags=("head",) if head else (level,))

        add("结论", res.get("verdict", ""), res.get("verdict_level", "info"))

        add("── 第 1 步：站点与模型列表 ──", "", head=True)
        add("地址（归一化）", res.get("base", ""), "muted")
        add("协议族", res.get("protocol_label", "未识别"))
        add("站点指纹", res.get("fingerprint") or "未识别（非 New API/one-api 系或已隐藏）")
        add("模型数量", str(res.get("model_count", len(res.get("models") or []))))
        add("模型列表耗时", f"{res.get('models_ms')} ms" if res.get("models_ms") else "-")
        for lvl, text in res.get("warnings", []):
            add(LEVEL_LABEL.get(lvl, "·"), text, lvl)

        add("── 第 2 步：生成测试 ──", "", head=True)
        if not res.get("used_model") and not res.get("gen_error"):
            add("状态", "尚未执行 —— 在「模型列表」里选一个模型，再点「② 对选中模型发测试」", "muted")
        else:
            add("请求模型", res.get("used_model") or "-")
            got = res.get("returned_model") or "-"
            same = (res.get("used_model") and res.get("returned_model")
                    and norm_model(res["used_model"]) == norm_model(res["returned_model"]))
            add("返回模型", got, "ok" if same else ("risk" if res.get("returned_model") else "muted"))
            add("首字延迟", f"{res['first_token_ms']:.0f} ms"
                if res.get("first_token_ms") is not None else "-")
            lvl_f, txt_f = evaluate_fluency(res.get("first_token_ms"),
                                            bool(res.get("stream_ok")))
            add("流畅度", txt_f, lvl_f)
            add("生成总耗时", f"{res.get('gen_ms')} ms" if res.get("gen_ms") else "-")
            usage = res.get("usage")
            if isinstance(usage, dict):
                add("usage（token）", json.dumps(usage, ensure_ascii=False)[:200])
            else:
                add("usage（token）", "未返回", "warn")
            add("HTTP 状态", str(res.get("http_status") or "-"))
            if res.get("sample_text"):
                add("返回内容片段", res["sample_text"].replace("\n", " ")[:160])
            if res.get("gen_error"):
                add("生成错误", res["gen_error"], "error")
            for lvl, text in res.get("gen_warnings", []):
                add(LEVEL_LABEL.get(lvl, "·"), text, lvl)

    def _refresh_models(self):
        models = (self.site_result or self.last_result or {}).get("models") or []
        kw = self.var_filter.get().strip().lower()
        only_main = bool(self.var_only_main.get())
        main_set = set(MAINSTREAM_MODELS)
        tv = self.tv_models
        tv.delete(*tv.get_children())
        n = 0
        for i, m in enumerate(models, 1):
            mid = m.get("id", "")
            nm = norm_model(mid)
            marks = []
            if nm in main_set:
                marks.append("主流")
            low = mid.lower()
            if any(x in low for x in ("embed", "rerank")):
                marks.append("向量/重排")
            elif any(x in low for x in ("tts", "whisper", "audio", "speech")):
                marks.append("语音")
            elif any(x in low for x in ("image", "dall-e", "flux", "sd", "video", "sora", "mj")):
                marks.append("图像/视频")
            elif any(x in low for x in ("moderation",)):
                marks.append("审核")
            if only_main and "主流" not in marks:
                continue
            if kw and kw not in mid.lower() and not any(kw in mk.lower() for mk in marks):
                continue
            n += 1
            m["_mark"] = "、".join(marks)
            tv.insert("", "end", values=(n, mid, m["_mark"], m.get("owner", "")
                                         or m.get("display", "")))
        self.lbl_models.configure(text=f"显示 {n} / 共 {len(models)} 个模型")

    def _select_model_in_tree(self, model_id: str) -> None:
        """在模型列表里定位并选中某个模型（仅高亮，不会自动发测试）。"""
        for iid in self.tv_models.get_children():
            if self.tv_models.set(iid, "id") == model_id:
                self.tv_models.selection_set(iid)
                self.tv_models.focus(iid)
                self.tv_models.see(iid)
                self.var_model.set(model_id)
                self.lbl_picked.configure(text=model_id)
                return

    def _on_model_select(self, _evt=None):
        sel = self.tv_models.selection()
        if not sel:
            return
        mid = self.tv_models.set(sel[0], "id")
        self.var_model.set(mid)
        self.lbl_picked.configure(text=mid)
        if self.site_result and not self.busy:
            self._set_status(f"已选中 {mid} —— 点「② 对选中模型发测试」或双击该行直接测")

    def _signature(self) -> str:
        return f"{self.var_url.get().strip()}|{self.var_key.get().strip()}"

    def _on_conn_changed(self, _evt=None):
        """输入框里的地址/Key 被改动后：结果作废 + 择机落盘。"""
        self._schedule_save()
        if self.busy:
            return
        if self.site_result and self._signature() != self._site_signature:
            self._invalidate_results()
            self._set_status("地址/Key 已变更：请重新点「① 获取模型列表」")

    def _on_model_dclick(self, _evt):
        sel = self.tv_models.selection()
        if not sel:
            return
        self._on_model_select()
        self.run_generation()

    def _append_batch_row(self, res: dict, done: int, total: int):
        row = result_to_row(res)
        self.batch_rows.append(row)
        req, got = res.get("used_model", ""), res.get("returned_model", "")
        arrow = f"{req} → {got}" if (req or got) else ""
        marks = " ; ".join(x for lvl, x in res.get("warnings", []) if lvl in ("risk", "warn", "error"))
        self.tv_batch.insert("", "end", tags=(res.get("verdict_level", "info"),), values=(
            res.get("base") or res.get("input", ""),
            res.get("protocol_label", ""),
            res.get("http_status", ""),
            res.get("models_ms", ""),
            round(res["first_token_ms"], 1) if res.get("first_token_ms") is not None else "",
            res.get("model_count", len(res.get("models") or [])),
            arrow,
            res.get("fingerprint", ""),
            res.get("verdict", "") + (f" ｜ {marks}" if marks else ""),
        ))
        self._set_status(f"批量进度 {done}/{total}")
        self.lbl_summary.configure(text=f"批量测试中… {done}/{total}")
        if done >= total:
            self._log_append(f"=== 批量测试完成，共 {total} 个 ===")
            self._set_status(f"批量完成：{total} 个")
            self._finish_busy()

    def _finish_batch(self):
        self._finish_busy()

    def _on_batch_dclick(self, _evt):
        sel = self.tv_batch.selection()
        if not sel:
            return
        vals = self.tv_batch.item(sel[0], "values")
        if vals:
            self.var_url.set(vals[0])
            self.nb.select(0)
            self._set_status("已把该地址填入上方输入框，可点「开始测试」看详情")

    # ---------------- 导出 ----------------
    def export_last(self, kind: str):
        from tkinter import filedialog, messagebox
        if not self.last_result:
            self._set_status("还没有测试结果")
            return
        res = self.last_result
        default = f"api_probe_{time.strftime('%Y%m%d_%H%M%S')}.{kind}"
        path = filedialog.asksaveasfilename(title="导出测试结果", defaultextension=f".{kind}",
                                            initialfile=default)
        if not path:
            return
        try:
            if kind == "json":
                export_json(res, path)
            else:
                export_csv([result_to_row(res)], path)
            self._set_status(f"已导出：{path}")
            messagebox.showinfo("导出成功", f"已导出到：\n{path}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("导出失败", str(e))

    def export_batch(self, kind: str):
        from tkinter import filedialog, messagebox
        if not self.batch_rows:
            self._set_status("批量结果为空")
            return
        default = f"api_batch_{time.strftime('%Y%m%d_%H%M%S')}.{kind}"
        path = filedialog.asksaveasfilename(title="导出批量结果", defaultextension=f".{kind}",
                                            initialfile=default)
        if not path:
            return
        try:
            if kind == "json":
                export_json(self.batch_rows, path)
            else:
                export_csv(self.batch_rows, path)
            self._set_status(f"已导出：{path}")
            messagebox.showinfo("导出成功", f"已导出到：\n{path}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("导出失败", str(e))

    def export_models(self):
        from tkinter import filedialog, messagebox
        models = (self.last_result or {}).get("models") or []
        if not models:
            self._set_status("模型列表为空")
            return
        path = filedialog.asksaveasfilename(title="导出模型列表", defaultextension=".csv",
                                            initialfile=f"models_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        if not path:
            return
        try:
            export_csv([{"模型ID": m.get("id", ""), "标记": m.get("_mark", ""),
                         "归属": m.get("owner", "")} for m in models], path)
            messagebox.showinfo("导出成功", f"已导出 {len(models)} 个模型到：\n{path}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("导出失败", str(e))


def enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll
        try:
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def run_gui() -> int:
    import tkinter as tk
    enable_dpi_awareness()
    root = tk.Tk()
    ProbeApp(root)
    root.mainloop()
    return 0


# =====================================================================
# 十、自检（本地 mock 服务器，不访问外网）
# =====================================================================

def _mock_server(mode: str):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):  # 静音
            pass

        def _send(self, code, obj=None, raw=None, ctype="application/json", extra=None):
            body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _path(self):
            return urllib.parse.urlsplit(self.path)

        def do_GET(self):  # noqa: N802
            u = self._path()
            path, q = u.path, urllib.parse.parse_qs(u.query)
            mode_ = self.server.mode  # type: ignore[attr-defined]
            auth = self.headers.get("Authorization") or ""
            xkey = self.headers.get("x-goog-api-key") or ""
            if path == "/api/status":
                return self._send(200, {"success": True, "data": {
                    "version": "v0.8.12.3", "system_name": "MockRelay",
                    "quota_per_unit": 500000, "display_in_currency": True}})
            if path == "/":
                return self._send(200, raw=b"<!doctype html><title>MockRelay - New API</title>",
                                  ctype="text/html; charset=utf-8")
            if mode_ == "badkey":
                return self._send(401, {"error": {"message": "无效的令牌 (request id: mock-1)",
                                                  "type": "new_api_error"}})
            if path in ("/v1/models", "/models"):
                if mode_ == "gemini":
                    # 真 Gemini 原生接口没有 /v1/models，这里如实 404，
                    # 用来验证「OpenAI 探测失败后会自动切到 Gemini 协议」
                    return self._send(404, {"error": {"message": "not found"}})
                if not auth.startswith("Bearer "):
                    return self._send(401, {"error": {"message": "未提供令牌", "type": "one_api_error"}})
                if mode_ == "anthropic":
                    # 模拟一个「同时支持 OpenAI 协议与 Anthropic Messages 协议」的中转站
                    return self._send(200, {"object": "list", "data": [
                        {"id": "claude-fable-5-1", "object": "model", "owned_by": "anthropic"},
                        {"id": "claude-haiku-4-5-20251001", "object": "model", "owned_by": "anthropic"},
                        {"id": "claude-opus-5-5", "object": "model", "owned_by": "anthropic"},
                        {"id": "claude-sonnet-5-5", "object": "model", "owned_by": "anthropic"}]})
                return self._send(200, {"object": "list", "data": [
                    {"id": "gpt-4o", "object": "model", "owned_by": "openai"},
                    {"id": "gpt-4o-mini-2024-07-18", "object": "model", "owned_by": "openai"},
                    {"id": "claude-sonnet-4-5-20250929", "object": "model", "owned_by": "anthropic"},
                    {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
                    {"id": "text-embedding-3-small", "object": "model", "owned_by": "openai"}]})
            if path == "/v1beta/models":
                if xkey != "sk-mock" and q.get("key", [""])[0] != "sk-mock":
                    return self._send(403, {"error": {"message": "API key not valid"}})
                return self._send(200, {"models": [
                    {"name": "models/gemini-2.5-pro", "displayName": "Gemini 2.5 Pro"},
                    {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash"}]})
            return self._send(404, {"error": {"message": "not found"}})

        def do_POST(self):  # noqa: N802
            u = self._path()
            path = u.path
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                payload = {}
            if path == "/v1/responses":
                # 模拟 Responses API（Codex 的首选 wire_api）
                mode_ = getattr(self.server, "mode", "")
                if mode_ == "chatonly":
                    return self._send(404, {"error": {"message": "not found"}})
                auth_hdr = self.headers.get("Authorization") or ""
                if mode_ == "badkey" or not auth_hdr.lower().startswith("bearer sk-mock"):
                    return self._send(401, {"error": {"message": "无效的令牌 (request id: mock-1)",
                                                      "type": "new_api_error"}})
                model = str((payload or {}).get("model") or "gpt-4o")
                return self._send(200, {
                    "id": "resp_mock", "object": "response", "status": "completed",
                    "model": model,
                    "output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "PONG"}]}],
                    "usage": {"input_tokens": 9, "output_tokens": 1, "total_tokens": 10}})
            if path == "/v1/chat/completions":
                mode_ = getattr(self.server, "mode", "")
                auth_hdr = self.headers.get("Authorization") or ""
                if mode_ == "badkey" or not auth_hdr.lower().startswith("bearer sk-mock"):
                    return self._send(401, {"error": {"message": "无效的令牌 (request id: mock-1)",
                                                      "type": "new_api_error"}})
                model = (payload or {}).get("model", "gpt-4o")
                # 故意返回一个不同的模型名，用于验证「掺水/映射」告警
                fake = "gpt-4o-mini" if model != "gpt-4o-mini" else "gpt-4o"
                if payload.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunks = [
                        {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": fake,
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "OK"},
                                      "finish_reason": None}]},
                        {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": fake,
                         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10}},
                    ]
                    for c in chunks:
                        self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                return self._send(200, {"id": "chatcmpl-mock", "object": "chat.completion",
                                        "model": fake,
                                        "choices": [{"index": 0, "finish_reason": "stop",
                                                     "message": {"role": "assistant", "content": "OK"}}],
                                        "usage": {"prompt_tokens": 9, "completion_tokens": 1,
                                                  "total_tokens": 10}})
            if path == "/v1/messages":
                mode_ = self.server.mode  # type: ignore[attr-defined]
                if mode_ == "openaionly":
                    # 只提供 OpenAI 协议的中转站：路径级 404，不带模型信息
                    return self._send(404, {"error": {"message": "not found"}})
                auth_hdr = self.headers.get("Authorization") or ""
                xkey = self.headers.get("x-api-key") or ""
                if mode_ == "xapikey":
                    ok_auth = (xkey == "sk-mock")           # 该站只认 x-api-key
                else:
                    ok_auth = (xkey == "sk-mock"
                               or auth_hdr.lower().startswith("bearer sk-mock"))
                if not ok_auth:
                    return self._send(401, {"type": "error", "error": {
                        "type": "authentication_error", "message": "invalid x-api-key"}})
                model = str((payload or {}).get("model") or "")
                if model == "bad-model" or (mode_ == "modelmissing" and not model.startswith("claude-")):
                    # 认证已通过，只是模型 id 不在该 key 的可用集合里（指南 §4 的经典 404）
                    return self._send(404, {"type": "error", "error": {
                        "type": "not_found_error",
                        "message": f'Model "{model}" is not supported by any configured '
                                   f'account in this group'}})
                if payload.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    evs = [
                        ("message_start", {"type": "message_start", "message": {
                            "id": "msg_mock", "model": "claude-sonnet-4-5", "usage": {"input_tokens": 8}}}),
                        ("content_block_delta", {"type": "content_block_delta",
                                                 "delta": {"type": "text_delta", "text": "PONG"}}),
                        ("message_delta", {"type": "message_delta",
                                           "usage": {"output_tokens": 1}}),
                    ]
                    for name, obj in evs:
                        self.wfile.write(f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode())
                    return
                return self._send(200, {"id": "msg_mock", "type": "message", "role": "assistant",
                                        "model": "claude-sonnet-4-5",
                                        "content": [{"type": "text", "text": "PONG"}],
                                        "usage": {"input_tokens": 8, "output_tokens": 1}})
            return self._send(404, {"error": {"message": "not found"}})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.mode = mode  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def selftest() -> int:
    ok_count, fail_count = 0, 0

    def check(name, cond, extra=""):
        nonlocal ok_count, fail_count
        if cond:
            ok_count += 1
            print(f"  [PASS] {name}")
        else:
            fail_count += 1
            print(f"  [FAIL] {name} {extra}")

    print(f"{APP_NAME} v{APP_VERSION} 自检\n")

    print("[1] URL 归一化")
    cases = [
        ("api.openai.com", "https://api.openai.com"),
        ("https://x.com/v1/chat/completions", "https://x.com"),
        ("https://x.com/v1/", "https://x.com"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api"),
        ("https://x.com/v1beta/models", "https://x.com"),
        ("https://x.com/proxy/v1/messages", "https://x.com/proxy"),
        ("http://127.0.0.1:3000/v1", "http://127.0.0.1:3000"),
    ]
    for raw, want in cases:
        got = normalize_base(raw)
        check(f"normalize_base({raw!r}) == {want!r}", got == want, f"-> {got!r}")

    print("\n[2] 模型名归一化")
    mcases = [("gpt-4o-2024-08-06", "gpt-4o"), ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
              ("openai/gpt-4o", "gpt-4o"), ("models/gemini-2.5-pro", "gemini-2-5-pro"),
              ("deepseek-chat", "deepseek-chat")]
    for raw, want in mcases:
        got = norm_model(raw)
        check(f"norm_model({raw!r}) == {want!r}", got == want, f"-> {got!r}")

    print("\n[2b] 默认模型挑选（回归：不要选 :batch / embedding）")
    fake = [{"id": x} for x in ["openai/gpt-4o:batch", "openai/gpt-4o",
                                "text-embedding-3-large", "openai/gpt-4o-mini"]]
    picked = choose_default_model(fake)
    check("避开 :batch 与 embedding", picked == "openai/gpt-4o", f"-> {picked}")
    picked2 = choose_default_model(fake, "gpt-4o-mini")
    check("用户指定模型优先命中", picked2 == "openai/gpt-4o-mini", f"-> {picked2}")
    check("用户指定的模型不在列表时原样保留",
          choose_default_model(fake, "my-custom-model") == "my-custom-model")

    print("\n[3] 批量行解析")
    items = parse_batch_lines("https://a.com | sk-1 | gpt-4o\n#注释\nhttps://b.com,sk-2\n\nhttps://c.com")
    check("解析出 3 条", len(items) == 3, f"-> {items}")
    check("第一条含模型", items and items[0]["model"] == "gpt-4o", f"-> {items[0] if items else None}")
    check("第三条只有 URL", items and items[2]["key"] == "")

    print("\n[4] mock 站点：OpenAI 兼容 + 掺水告警")
    srv, base = _mock_server("openai")
    try:
        res = probe(base, "sk-mock", timeout=10, deep=True)
        check("识别为 OpenAI 兼容", res["protocol"] == "openai", f"-> {res['protocol']}")
        check("模型数 == 5", res["model_count"] == 5, f"-> {res['model_count']}")
        check("识别为可用", res["ok"] is True, f"-> {res['verdict']}")
        check("流式拿到首字", res["first_token_ms"] is not None)
        check("返回模型被解析", res["returned_model"] == "gpt-4o-mini", f"-> {res['returned_model']}")
        check("触发模型不一致告警", any(lvl == "risk" for lvl, _ in res["gen_warnings"]),
              f"-> {res['gen_warnings']}")
        check("指纹识别 MockRelay", "MockRelay" in (res["fingerprint"] or ""), f"-> {res['fingerprint']}")
        check("usage 被解析", isinstance(res["usage"], dict) and res["usage"].get("total_tokens") == 10,
              f"-> {res['usage']}")
    finally:
        srv.shutdown()

    print("\n[4b] 两段式：先拉列表 → 人工选模型 → 再生成测试")
    srv, base = _mock_server("openai")
    try:
        site = probe_site(base, "sk-mock", timeout=10)
        check("第 1 步识别协议", site["protocol"] == "openai", f"-> {site['protocol']}")
        check("第 1 步拿到 5 个模型", site["model_count"] == 5, f"-> {site['model_count']}")
        check("第 1 步判定站点可达", site["ok_site"] is True)
        check("第 1 步不发生成请求", site["ok"] is False and site["first_token_ms"] is None)
        pick = "claude-sonnet-4-5-20250929"          # 模拟人工从列表里挑一个
        gen = probe_generate(site, "sk-mock", pick, timeout=10, deep=True)
        check("第 2 步用人工选定的模型", gen["used_model"] == pick, f"-> {gen['used_model']}")
        check("第 2 步拿到首字延迟", gen["first_token_ms"] is not None)
        check("第 2 步判定可用", gen["ok"] is True, f"-> {gen['verdict']}")
        gen2 = probe_generate(site, "sk-mock", "gpt-4o", timeout=10, deep=False)
        check("换模型重测仍是可用", gen2["used_model"] == "gpt-4o" and gen2["ok"] is True)
        check("换模型重测不累积旧结论",
              len([1 for lvl, _ in gen2["gen_warnings"] if lvl == "risk"]) == 1,
              f"-> {gen2['gen_warnings']}")
        check("第 1 步结论仍在（未被覆盖）",
              "第 1 步完成" in (gen2.get("verdict") or "") or bool(gen2.get("models")),
              f"-> {gen2.get('verdict')}")
    finally:
        srv.shutdown()

    print("\n[5] mock 站点：Gemini 协议")
    srv, base = _mock_server("gemini")
    try:
        res = probe(base, "sk-mock", timeout=10, deep=False)
        check("识别为 Gemini", res["protocol"] == "gemini", f"-> {res['protocol']}")
        check("模型数 == 2", res["model_count"] == 2, f"-> {res['model_count']}")
    finally:
        srv.shutdown()

    print("\n[6] mock 站点：Key 无效")
    srv, base = _mock_server("badkey")
    try:
        res = probe(base, "sk-bad", timeout=10)
        check("判定为不可用", res["ok"] is False)
        check("错误信息包含『无效的令牌』",
              "无效的令牌" in json.dumps(res["warnings"], ensure_ascii=False),
              f"-> {res['warnings']}")
    finally:
        srv.shutdown()

    print("\n[7] 不可达地址（应优雅失败，不抛异常）")
    res = probe("http://127.0.0.1:9/v1", "sk-x", timeout=3)
    check("返回结论而非异常", bool(res["verdict"]), f"-> {res['verdict']}")
    check("ok 为 False", res["ok"] is False)

    print("\n[8] 导出 CSV / JSON")
    import tempfile
    srv, base = _mock_server("openai")
    try:
        res = probe(base, "sk-mock", timeout=10, deep=False)
        with tempfile.TemporaryDirectory() as td:
            pj = os.path.join(td, "r.json")
            pc = os.path.join(td, "r.csv")
            export_json(res, pj)
            export_csv([result_to_row(res)], pc)
            data = json.loads(Path(pj).read_text(encoding="utf-8"))
            check("JSON 可回读且结论一致", data.get("verdict") == res["verdict"],
                  f"-> {data.get('verdict')!r}")
            txt = Path(pc).read_text(encoding="utf-8-sig")
            head, _, rest = txt.partition("\n")
            check("CSV 表头含关键列", "结论" in head and "首字延迟ms" in head, f"-> {head[:80]}")
            check("CSV 含站点地址", "127.0.0.1" in rest)
    finally:
        srv.shutdown()

    print("\n[9] 敏感信息保存（本机加密）")
    avail = dpapi_available()
    print(f"  本机加密能力（Windows DPAPI）: {'可用' if avail else '不可用'}")
    secret = "sk-test-abcdef1234567890"
    blob, enc = encode_secret(secret)
    check("编码结果非空", bool(blob))
    check("加密标记与实际能力一致", enc is avail, f"-> enc={enc} avail={avail}")
    check("能原样解回来", decode_secret(blob, enc) == secret,
          f"-> {decode_secret(blob, enc)!r}")
    if avail:
        check("存盘内容里没有明文 Key", secret not in blob, f"-> {blob[:36]}...")
    check("空值不炸", encode_secret("")[0] == "" and decode_secret("", True) == "")
    check("坏数据不炸（返回空串）", decode_secret("!!!not-base64!!!", True) == "")
    check("向后兼容旧的明文配置",
          decode_secret("sk-plain-old", False) == "sk-plain-old")

    print("\n[10] Claude Code 接入")
    check("ANTHROPIC_BASE_URL 不带 /v1 后缀",
          claude_base_for_gateway("https://gw.example.com/v1/") == "https://gw.example.com")
    hb = claude_auth_headers("bearer", "sk-x")
    hx = claude_auth_headers("x-api-key", "sk-x")
    check("bearer 只出 Authorization",
          hb.get("Authorization") == "Bearer sk-x" and "x-api-key" not in hb)
    check("x-api-key 只出 x-api-key",
          hx.get("x-api-key") == "sk-x" and "Authorization" not in hx)
    check("两种都带 anthropic-version", hb.get("anthropic-version") == "2023-06-01")

    srv, base = _mock_server("anthropic")
    try:
        chk = check_anthropic_channel(base, "sk-mock", "claude-sonnet-5-5", timeout=10)
        check("支持 Anthropic 协议的站检测通过", chk["ok"] is True, f"-> {chk['verdict']}")
        check("认证方式识别为 bearer", chk["auth_style"] == "bearer")
        check("非流式拿到 PONG", "PONG" in chk["reply"], f"-> {chk['reply']!r}")
        check("流式也通过", chk["stream_ok"] is True, f"-> {chk.get('stream_error')}")
        check("端点拼的是 /v1/messages", chk["url"].endswith("/v1/messages"))
        bad = check_anthropic_channel(base, "sk-mock", "bad-model", timeout=10)
        check("模型不可用时判为 model_not_found", bad["model_not_found"] is True,
              f"-> {bad['verdict']}")
        check("模型不可用时是警告级（认证其实是通的）", bad["verdict_level"] == "warn")
    finally:
        srv.shutdown()

    srv, base = _mock_server("xapikey")
    try:
        chk2 = check_anthropic_channel(base, "sk-mock", "claude-sonnet-5-5", timeout=10)
        check("只认 x-api-key 的站会自动改用该头",
              chk2["ok"] is True and chk2["auth_style"] == "x-api-key",
              f"-> {chk2['auth_style']} {chk2['verdict']}")
    finally:
        srv.shutdown()

    srv, base = _mock_server("openaionly")
    try:
        chk3 = check_anthropic_channel(base, "sk-mock", "gpt-4o", timeout=10)
        check("纯 OpenAI 站被识别为无法直连",
              chk3["ok"] is False and chk3["path_issue"] is True, f"-> {chk3['verdict']}")
    finally:
        srv.shutdown()

    srv, base = _mock_server("badkey")
    try:
        chk4 = check_anthropic_channel(base, "sk-bad", "claude-sonnet-5-5", timeout=10)
        check("Key 无效时报「鉴权失败」而不是「协议不支持」",
              chk4["ok"] is False and chk4["verdict_level"] == "warn"
              and "鉴权失败" in chk4["verdict"], f"-> {chk4['verdict']}")
    finally:
        srv.shutdown()

    sample = [{"id": x} for x in ["claude-fable-5-1", "claude-haiku-4-5-20251001",
                                  "claude-opus-5-5", "claude-sonnet-5-5", "gpt-4o"]]

    lv1, tx1 = evaluate_fluency(620, True)
    lv2, tx2 = evaluate_fluency(4200, True)
    lv3, tx3 = evaluate_fluency(None, False)
    check("流畅度：快 → ok", lv1 == "ok" and "流畅" in tx1, f"-> {tx1}")
    check("流畅度：慢 → warn", lv2 == "warn" and "偏慢" in tx2, f"-> {tx2}")
    check("流畅度：没拿到首字 → warn", lv3 == "warn", f"-> {tx3}")

    srv, base = _mock_server("anthropic")
    try:
        pipe = run_full_pipeline(base, "sk-mock", timeout=10, deep=False)
        check("全流程：拿到 4 个模型", (pipe["site"] or {}).get("model_count") == 4,
              f"-> {(pipe['site'] or {}).get('model_count')}")
        check("全流程：生成测试通过", (pipe["gen"] or {}).get("ok") is True,
              f"-> {(pipe['gen'] or {}).get('verdict')}")
        check("全流程：Anthropic 通道通过", (pipe["channel"] or {}).get("ok") is True,
              f"-> {(pipe['channel'] or {}).get('verdict')}")
        check("全流程：给出模型映射", pipe["model_map"].get("sonnet") == "claude-sonnet-5-5",
              f"-> {pipe['model_map']}")
        check("全流程：四步都记录了", len(pipe["steps"]) == 4, f"-> {pipe['steps']}")
        check("全流程：结论为通过", pipe["ok"] is True, f"-> {pipe['verdict']}")
        check("全流程：带上了流畅度", bool(pipe["fluency"]), f"-> {pipe['fluency']}")
    finally:
        srv.shutdown()

    srv, base = _mock_server("openaionly")
    try:
        pipe2 = run_full_pipeline(base, "sk-mock", timeout=10, deep=False)
        check("纯 OpenAI 协议时 Claude Code 通道判为不可用",
              (pipe2["channel"] or {}).get("ok") is False, f"-> {pipe2['channel'].get('verdict')}")
        check("但 Codex 通道可用，全流程仍通过", pipe2["ok"] is True, f"-> {pipe2['verdict']}")
        check("给出了正确的 wire_api",
              (pipe2["channel_openai"] or {}).get("wire_api") == "responses",
              f"-> {(pipe2['channel_openai'] or {}).get('wire_api')}")
        check("此时模型测试本身是通过的", (pipe2["gen"] or {}).get("ok") is True)
    finally:
        srv.shutdown()

    mm = suggest_claude_model_map(sample)
    check("sonnet 映射到 claude-sonnet-5-5", mm.get("sonnet") == "claude-sonnet-5-5", f"-> {mm}")
    check("opus 映射到 claude-opus-5-5", mm.get("opus") == "claude-opus-5-5")
    check("haiku 映射到 claude-haiku-4-5-20251001",
          mm.get("haiku") == "claude-haiku-4-5-20251001")
    check("fable 映射到 claude-fable-5-1", mm.get("fable") == "claude-fable-5-1")
    check("默认模型回落到 sonnet", mm.get("default") == "claude-sonnet-5-5")

    e1 = build_claude_env("https://gw.example.com/v1", "sk-x", "bearer", mm)
    e2 = build_claude_env("https://gw.example.com", "sk-x", "x-api-key", mm)
    check("bearer → 只有 AUTH_TOKEN",
          "ANTHROPIC_AUTH_TOKEN" in e1 and "ANTHROPIC_API_KEY" not in e1)
    check("x-api-key → 只有 API_KEY",
          "ANTHROPIC_API_KEY" in e2 and "ANTHROPIC_AUTH_TOKEN" not in e2)
    check("BASE_URL 已去掉 /v1", e1["ANTHROPIC_BASE_URL"] == "https://gw.example.com")
    check("别名映射都写进去了",
          e1.get("ANTHROPIC_DEFAULT_OPUS_MODEL") == "claude-opus-5-5"
          and e1.get("ANTHROPIC_MODEL") == "claude-sonnet-5-5")

    old_settings = {"env": {"ANTHROPIC_AUTH_TOKEN": "old-token", "FOO": "bar"},
                    "permissions": {"allow": ["Bash"]}}
    merged = merge_claude_settings(old_settings, dict(e2))
    check("合并时清掉旧 AUTH_TOKEN（不会两个 key 变量并存）",
          "ANTHROPIC_AUTH_TOKEN" not in merged["env"])
    check("合并后写入 API_KEY", merged["env"].get("ANTHROPIC_API_KEY") == "sk-x")
    check("保留无关的 env 键", merged["env"].get("FOO") == "bar")
    check("保留非 env 的顶层配置", merged.get("permissions") == {"allow": ["Bash"]})

    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "settings.json"
        r1 = install_claude_settings(dict(e1), sp)
        check("首次写配置成功", r1["ok"] and sp.exists(), f"-> {r1.get('error')}")
        check("原本没有文件时不产生备份", r1["backup"] == "")
        r2 = install_claude_settings(dict(e2), sp)
        check("二次写入会先备份", r2["ok"] and Path(r2["backup"]).exists(), f"-> {r2['backup']}")
        saved = json.loads(sp.read_text(encoding="utf-8"))
        check("落盘后只有一个 key 变量",
              ("ANTHROPIC_AUTH_TOKEN" in saved["env"]) != ("ANTHROPIC_API_KEY" in saved["env"]))
        sp.write_text("{ 这不是 json", encoding="utf-8")
        r3 = install_claude_settings(dict(e1), sp)
        check("坏配置拒绝覆盖", r3["ok"] is False and "无法解析" in r3["error"], f"-> {r3['error']}")
        check("坏配置内容原样保留", sp.read_text(encoding="utf-8") == "{ 这不是 json")

        lp = Path(td) / "start-claude.bat"
        lr = write_claude_launcher(dict(e1), lp)
        body = lp.read_bytes() if lp.exists() else b""
        check("启动脚本生成成功", lr["ok"] and bool(body), f"-> {lr.get('error')}")
        check("启动脚本是纯 ASCII", all(b < 128 for b in body))
        check("启动脚本用 CRLF", b"\r\n" in body and b"\n\n" not in body.replace(b"\r\n", b"\n"))
        check("启动脚本含环境变量与 claude 调用",
              b"ANTHROPIC_BASE_URL=https://gw.example.com" in body and b"claude %*" in body)
        badlaunch = write_claude_launcher({"ANTHROPIC_MODEL": "中文模型名"},
                                          Path(td) / "x.bat")
        check("非 ASCII 值拒绝写 .bat",
              badlaunch["ok"] is False and "非 ASCII" in badlaunch["error"])

        iso = write_isolated_claude_config(dict(e1), Path(td) / "iso")
        check("隔离配置目录生成成功", iso["ok"] and Path(iso["path"]).exists(),
              f"-> {iso.get('error')}")
        check("能找到备份文件", len(find_claude_backups(sp)) >= 1)

        stub_dir = Path(td) / "stub"
        stub_dir.mkdir()
        stub = stub_dir / "claude.cmd"
        stub.write_text('@echo off\r\necho {"is_error":false,"result":"PONG",'
                        '"terminal_reason":"completed","api_error_status":null,'
                        '"modelUsage":{"claude-sonnet-5-5":{}}}\r\n', encoding="ascii")
        vr = verify_claude_cli(dict(e1), cli_path=str(stub), timeout=30)
        check("能解析 CLI 的 JSON 并判定成功", vr["ok"] is True,
              f"-> {vr.get('error')} {vr.get('raw')!r}")
        check("拿到 result 与 terminal_reason",
              vr["result"] == "PONG" and vr["terminal_reason"] == "completed")
        check("验证跑在隔离配置目录里", "api_probe_cc_" in vr["config_dir"])
        stub.write_text('@echo off\r\necho {"is_error":true,"result":"","api_error_status":404}\r\n',
                        encoding="ascii")
        vr2 = verify_claude_cli(dict(e1), cli_path=str(stub), timeout=30)
        check("CLI 报错时判定为失败", vr2["ok"] is False and vr2["status"] == "404",
              f"-> {vr2}")
        missing = verify_claude_cli(dict(e1), cli_path=str(Path(td) / "nope.exe"), timeout=5)
        check("找不到 CLI 时优雅失败",
              missing["ok"] is False and "没找到" in missing["error"], f"-> {missing['error']}")

    print("\n[11] Codex（OpenAI 格式）接入")
    check("Codex base_url 自动补 /v1",
          codex_base_url_for_gateway("https://gw.example.com") == "https://gw.example.com/v1")
    check("Codex base_url 不会重复 /v1",
          codex_base_url_for_gateway("https://gw.example.com/v1/") == "https://gw.example.com/v1")
    check("Codex base_url 能从完整端点还原",
          codex_base_url_for_gateway("https://gw.example.com/v1/chat/completions")
          == "https://gw.example.com/v1")

    srv, base = _mock_server("openai")
    try:
        oc = check_openai_channel(base, "sk-mock", "gpt-4o", timeout=10)
        check("支持 Responses API 的站优先用 responses", oc["wire_api"] == "responses",
              f"-> {oc['wire_api']} {oc['verdict']}")
        check("OpenAI 通道检测通过", oc["ok"] is True, f"-> {oc['verdict']}")
        check("拿到了返回文本", "PONG" in oc["reply"], f"-> {oc['reply']!r}")
        check("base_url 带 /v1", oc["base_url"].endswith("/v1"))
    finally:
        srv.shutdown()

    srv, base = _mock_server("chatonly")
    try:
        oc2 = check_openai_channel(base, "sk-mock", "gpt-4o", timeout=10)
        check("只支持 chat 的站自动回落到 chat", oc2["wire_api"] == "chat",
              f"-> {oc2['wire_api']} {oc2['verdict']}")
    finally:
        srv.shutdown()

    srv, base = _mock_server("badkey")
    try:
        oc3 = check_openai_channel(base, "sk-bad", "gpt-4o", timeout=10)
        check("Key 无效时报鉴权失败而不是协议不支持",
              oc3["ok"] is False and "鉴权失败" in oc3["verdict"], f"-> {oc3['verdict']}")
    finally:
        srv.shutdown()

    check("Codex 默认模型优先挑 gpt-5 系",
          suggest_codex_model([{"id": x} for x in
                               ["gpt-4o-mini", "text-embedding-3-small", "gpt-5.1-codex",
                                "gpt-4o", "o3-mini"]]) == "gpt-5.1-codex")
    check("没有 gpt-5 时退到 gpt-4o 而不是 embedding",
          suggest_codex_model([{"id": "text-embedding-3-small"}, {"id": "gpt-4o"}]) == "gpt-4o")

    existing_toml = ('model = "old-model"\nmodel_provider = "old_prov"\n'
                     'approval_policy = "never"\n\n'
                     '[model_providers.hermes]\nbase_url = "https://x/v1"\n\n'
                     '[desktop]\ntheme = "dark"\n')
    merged = merge_codex_config(existing_toml, "gpt-5.1-codex", "https://gw.example.com/v1",
                                "CODEX_API_KEY", "我的站点", "responses")
    check("合并后旧的顶层 model 被替换", 'model = "old-model"' not in merged)
    check("合并后其它顶层键保留", 'approval_policy = "never"' in merged)
    check("合并后别的 provider 表保留", "[model_providers.hermes]" in merged)
    check("合并后其它表保留", "[desktop]" in merged)
    check("写入了自己的 provider 表", f"[model_providers.{CODEX_PROVIDER_ID}]" in merged)
    check("env_key 与 wire_api 写对",
          'env_key = "CODEX_API_KEY"' in merged and 'wire_api = "responses"' in merged)
    check("顶层键排在所有表之前（TOML 硬要求）", merged.index("model =") < merged.index("["))
    merged2 = merge_codex_config(merged, "gpt-5.1-codex", "https://gw.example.com/v1",
                                 "CODEX_API_KEY", "我的站点", "responses")
    check("重复执行不产生重复块",
          merged2.count(f"[model_providers.{CODEX_PROVIDER_ID}]") == 1)
    try:
        import tomllib
        parsed = tomllib.loads(merged2)
        check("合并结果是合法 TOML，且值正确",
              parsed.get("model") == "gpt-5.1-codex"
              and parsed["model_providers"][CODEX_PROVIDER_ID]["wire_api"] == "responses"
              and parsed.get("approval_policy") == "never")
    except ImportError:
        check("（Python<3.11 无 tomllib，跳过 TOML 解析校验）", True)

    with tempfile.TemporaryDirectory() as td:
        cp = Path(td) / "config.toml"
        cp.write_text(existing_toml, encoding="utf-8")
        r = install_codex_config("gpt-5.1-codex", "https://gw.example.com/v1", "CODEX_API_KEY",
                                 "我的站点", "responses", path=cp)
        check("写 config.toml 成功", r["ok"] and cp.exists(), f"-> {r.get('error')}")
        check("记录下旧的 model/provider",
              r["old_model"] == "old-model" and r["old_provider"] == "old_prov", f"-> {r}")
        check("产生备份", bool(r["backup"]) and Path(r["backup"]).exists())
        cp.write_text("这不是合法 toml [[[", encoding="utf-8")
        bad = install_codex_config("m", "https://gw/v1", "K", "n", "responses", path=cp)
        check("坏 TOML 拒绝覆盖", bad["ok"] is False and "合法 TOML" in bad["error"],
              f"-> {bad['error']}")
        check("坏文件内容原样保留", cp.read_text(encoding="utf-8") == "这不是合法 toml [[[")

        lp = Path(td) / "start-codex.bat"
        lr = write_codex_launcher("CODEX_API_KEY", "sk-x", lp, r"C:\tools\codex.exe")
        body = lp.read_bytes() if lp.exists() else b""
        check("Codex 启动脚本生成成功", lr["ok"] and bool(body), f"-> {lr.get('error')}")
        check("启动脚本纯 ASCII", all(b < 128 for b in body))
        check("启动脚本用 CRLF", b"\r\n" in body)
        check("启动脚本设了 Key 变量并调用 codex",
              b'set "CODEX_API_KEY=sk-x"' in body and b"codex.exe" in body)
        badl = write_codex_launcher("CODEX_API_KEY", "中文Key", Path(td) / "x.bat")
        check("非 ASCII Key 拒绝写 .bat", badl["ok"] is False and "非 ASCII" in badl["error"])

        ep = Path(td) / ".env"
        ep.write_text("OTHER=1\nCODEX_API_KEY=sk-old\n", encoding="utf-8")
        er = install_codex_env_key("CODEX_API_KEY", "sk-new", path=ep)
        envtxt = ep.read_text(encoding="utf-8")
        check("写 .env 成功", er["ok"], f"-> {er.get('error')}")
        check(".env 里同名变量被替换", "CODEX_API_KEY=sk-new" in envtxt
              and "sk-old" not in envtxt)
        check(".env 里其它变量保留", "OTHER=1" in envtxt)

        stub_dir = Path(td) / "cstub"
        stub_dir.mkdir()
        stub = stub_dir / "codex.cmd"
        stub.write_text("@echo off\r\necho PONG\r\n", encoding="ascii")
        vr = verify_codex_cli("gpt-5.1-codex", "https://gw.example.com/v1", "CODEX_API_KEY",
                              "sk-mock", cli_path=str(stub), timeout=60)
        check("Codex 端到端验证能识别 PONG", vr["ok"] is True,
              f"-> {vr.get('error')} {vr.get('result')!r}")
        check("验证跑在隔离 CODEX_HOME 里", "api_probe_codex_" in vr["config_home"])
        stub.write_text("@echo off\r\necho nothing useful\r\n", encoding="ascii")
        vr2 = verify_codex_cli("m", "https://x/v1", "K", "sk-mock", cli_path=str(stub), timeout=60)
        check("没有 PONG 时判失败", vr2["ok"] is False)
        missing = verify_codex_cli("m", "https://x/v1", "K", "sk", 
                                   cli_path=str(Path(td) / "nope.exe"), timeout=5)
        check("找不到 codex 时优雅失败",
              missing["ok"] is False and "没找到" in missing["error"], f"-> {missing['error']}")

    print(f"\n结果：{ok_count} 通过 / {fail_count} 失败")
    return 1 if fail_count else 0


def uitest() -> int:
    """界面自检：构建控件 + 用本地 mock 跑一遍「拉列表 → 人工挑模型 → 发测试」的完整流程，
    再验证配置持久化（URL 明文、Key 加密、重启恢复）。

    注意：整个过程把 CONFIG_PATH 指向临时目录，绝不碰用户真实的 ~/.api_probe.json。
    """
    import tkinter as tk
    import tempfile
    global CONFIG_PATH, CLAUDE_SETTINGS_PATH, CLAUDE_OUTPUT_DIR
    global CODEX_HOME, CODEX_CONFIG_PATH, CODEX_ENV_PATH

    real_config_path = CONFIG_PATH
    CONFIG_PATH = Path(tempfile.mkdtemp(prefix="api_probe_cfg_")) / "cfg.json"
    # Claude Code 相关的路径同样重定向到临时目录：测试绝不碰用户真实的 ~/.claude
    real_claude_settings = CLAUDE_SETTINGS_PATH
    real_claude_outdir = CLAUDE_OUTPUT_DIR
    CLAUDE_SETTINGS_PATH = (Path(tempfile.mkdtemp(prefix="api_probe_claude_"))
                            / ".claude" / "settings.json")
    CLAUDE_OUTPUT_DIR = Path(tempfile.mkdtemp(prefix="api_probe_ccout_"))
    # Codex 同理：绝不碰用户真实的 ~/.codex（那里有他的 provider 配置与 Key）
    real_codex_home = CODEX_HOME
    real_codex_config = CODEX_CONFIG_PATH
    real_codex_env = CODEX_ENV_PATH
    _codex_home_tmp = Path(tempfile.mkdtemp(prefix="api_probe_codexhome_"))
    CODEX_HOME = _codex_home_tmp
    CODEX_CONFIG_PATH = _codex_home_tmp / "config.toml"
    CODEX_ENV_PATH = _codex_home_tmp / ".env"

    enable_dpi_awareness()
    fails = []

    def check(name, cond, extra=""):
        if cond:
            print(f"  [PASS] {name}")
        else:
            fails.append(name)
            print(f"  [FAIL] {name} {extra}")

    def wait_until(pred, root, timeout=20.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            root.update()
            if pred():
                return True
            time.sleep(0.05)
        root.update()
        return pred()

    srv, base = _mock_server("openai")
    root = tk.Tk()
    root.withdraw()
    try:
        app = ProbeApp(root)
        root.update()
        check("界面构建成功", True)

        # ---- 第 1 步：拉模型列表 ----
        app.var_url.set(base)
        app.var_key.set("sk-mock")
        app.var_deep.set(False)
        app.fetch_models()
        check("第 1 步进入忙碌态", app.busy is True)
        ok = wait_until(lambda: app.site_result is not None and not app.busy, root)
        check("第 1 步完成且回到空闲", ok)
        check("第 1 步拿到 5 个模型",
              (app.site_result or {}).get("model_count") == 5,
              f"-> {(app.site_result or {}).get('model_count')}")
        check("第 1 步没有发生成请求",
              (app.site_result or {}).get("first_token_ms") is None)
        check("模型列表已填进表格", len(app.tv_models.get_children()) == 5,
              f"-> {len(app.tv_models.get_children())} 行")
        check("第 2 步按钮已启用", str(app.btn_gen["state"]) == "normal",
              f"-> {app.btn_gen['state']}")
        check("自动切到模型列表页", app.nb.index(app.nb.select()) == 1)

        # ---- 人工挑一个模型（模拟点击列表里的另一行）----
        pick = "deepseek-chat"
        app._select_model_in_tree(pick)
        root.update()
        check("选中后写入指定模型框", app.var_model.get() == pick, f"-> {app.var_model.get()}")
        check("界面显示当前选中", pick in app.lbl_picked.cget("text"))

        # ---- 第 2 步：发测试 ----
        app.run_generation()
        ok = wait_until(lambda: not app.busy, root)
        check("第 2 步完成", ok)
        res = app.last_result or {}
        check("测试用的正是人工选的那个模型", res.get("used_model") == pick,
              f"-> {res.get('used_model')}")
        check("第 2 步拿到首字延迟", res.get("first_token_ms") is not None)
        check("第 2 步判定可用", res.get("ok") is True, f"-> {res.get('verdict')}")
        check("第 1 步信息仍保留", res.get("model_count") == 5)

        items = [app.tv_ov.set(i, "item") for i in app.tv_ov.get_children()]
        check("概览含第 1 步分区", any("第 1 步" in i for i in items))
        check("概览含第 2 步分区", any("第 2 步" in i for i in items))
        check("概览记录了请求/返回模型",
              any(i == "请求模型" for i in items) and any(i == "返回模型" for i in items))
        check("测试完切回概览页", app.nb.index(app.nb.select()) == 0)

        # ---- 换一个模型重测：不应污染第 1 步信息 ----
        app.var_model.set("gpt-4o")
        app.run_generation()
        wait_until(lambda: not app.busy, root)
        res2 = app.last_result or {}
        check("换模型重测 ok", res2.get("used_model") == "gpt-4o" and res2.get("ok") is True)
        check("重测后模型列表仍在", res2.get("model_count") == 5)

        # ---- 改地址应让第 1 步结果作废 ----
        app.var_url.set(base + "/other")
        app._on_conn_changed()
        check("地址变更后第 2 步按钮被禁用", str(app.btn_gen["state"]) == "disabled",
              f"-> {app.btn_gen['state']}")
        check("地址变更后结果已清空", app.site_result is None)

        # ---- 配置持久化：URL 明文存、Key 加密存、重启后能恢复 ----
        try:
            app.var_url.set(base)
            app.var_key.set("sk-mock-secret-123456")
            app.var_model.set("gpt-4o")
            app.var_remember.set(True)
            app.txt_batch.delete("1.0", "end")
            app.txt_batch.insert("1.0", "https://a.example | sk-batch-key | gpt-4o\n")
            app._save_config()
            check("配置已写入磁盘", CONFIG_PATH.exists(), f"-> {CONFIG_PATH}")
            raw = CONFIG_PATH.read_text(encoding="utf-8")
            saved = json.loads(raw)
            check("URL 明文保存", saved.get("url") == base, f"-> {saved.get('url')}")
            check("配置文件里找不到明文 Key", "sk-mock-secret-123456" not in raw)
            check("配置文件里找不到批量明文 Key", "sk-batch-key" not in raw)
            check("加密标记与实际能力一致",
                  bool(saved.get("key_encrypted")) is dpapi_available())

            root2 = tk.Tk()
            root2.withdraw()
            app2 = ProbeApp(root2)
            check("重启后恢复 URL", app2.var_url.get() == base, f"-> {app2.var_url.get()}")
            check("重启后恢复 Key", app2.var_key.get() == "sk-mock-secret-123456",
                  f"-> {app2.var_key.get()!r}")
            check("重启后恢复勾选与指定模型",
                  bool(app2.var_remember.get()) and app2.var_model.get() == "gpt-4o")
            check("重启后恢复批量列表",
                  "sk-batch-key" in app2.txt_batch.get("1.0", "end-1c"))
            check("重启后恢复界面开关（TLS 校验）", bool(app2.var_verify.get()) is True)

            app2.var_remember.set(False)
            app2._on_remember_toggle()
            saved2 = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            check("取消「记住 Key」后文件中的 Key 被清空", saved2.get("key") == "",
                  f"-> {saved2.get('key')!r}")
            root2.destroy()
        finally:
            pass

        # ---- 站点管理：多个中转站互相切换（含用户反馈的「切回自定义不还原」）----
        app.var_remember.set(True)
        app.var_site.set(ADHOC_SITE)
        app._active_site = ADHOC_SITE
        app.var_url.set("https://my-own-relay.example/v1")   # 先在「自定义」格里放一个地址
        app.var_key.set("sk-adhoc-key")
        app.var_model.set("gpt-4o")
        app._stash_current_fields()

        ok_a = app._add_site_record("Kimi 官方", "https://api.moonshot.cn/v1",
                                    "sk-kimi-123", "kimi-k2")
        ok_b = app._add_site_record("中转站B", "https://relay.example.com",
                                    "sk-siteb-456", "claude-sonnet-5-5")
        check("能连续添加两个站点", ok_a and ok_b and len(app.sites) == 2,
              f"-> a={ok_a} b={ok_b} n={len(app.sites)}")
        check("重复名字不允许添加", app._add_site_record("Kimi 官方", "https://x.com") is False)
        check("添加后自动选中新站点", app.var_site.get() == "中转站B", f"-> {app.var_site.get()}")
        check("切换站点后地址跟着变", app.var_url.get() == "https://relay.example.com",
              f"-> {app.var_url.get()}")
        check("切换站点后 Key 跟着变", app.var_key.get() == "sk-siteb-456")
        check("切换站点后默认模型跟着变", app.var_model.get() == "claude-sonnet-5-5")
        check("下拉里是 自定义 + 2 个站点", len(list(app.cmb_site.cget("values"))) == 3,
              f"-> {app.cmb_site.cget('values')}")

        app._select_site("Kimi 官方")
        check("切到 Kimi 站地址正确", app.var_url.get() == "https://api.moonshot.cn/v1",
              f"-> {app.var_url.get()}")
        check("切到 Kimi 站 Key 正确", app.var_key.get() == "sk-kimi-123")

        # 用户报的 bug 场景：切到别的站点后，再切回「自定义」必须还原自定义那一格
        app._select_site(ADHOC_SITE)
        check("切回「自定义」还原自定义地址",
              app.var_url.get() == "https://my-own-relay.example/v1", f"-> {app.var_url.get()}")
        check("切回「自定义」还原自定义 Key", app.var_key.get() == "sk-adhoc-key")
        check("切回「自定义」还原自定义模型", app.var_model.get() == "gpt-4o")

        app._select_site("Kimi 官方")
        app._select_site(ADHOC_SITE)
        check("来回切换后自定义格还是原值",
              app.var_url.get() == "https://my-own-relay.example/v1"
              and app.var_key.get() == "sk-adhoc-key")

        # 在站点里改了内容，切走再切回要保住
        app._select_site("Kimi 官方")
        app.var_url.set("https://api.moonshot.cn/v1/")
        app._stash_current_fields()
        app._select_site("中转站B")
        app._select_site("Kimi 官方")
        check("站点内的编辑会被记住", app.var_url.get() == "https://api.moonshot.cn/v1/",
              f"-> {app.var_url.get()}")

        # 落盘 + 加密 + 重启恢复
        app._save_config()
        raw3 = CONFIG_PATH.read_text(encoding="utf-8")
        cfg3 = json.loads(raw3)
        check("配置文件里存了两个站点",
              [s["name"] for s in cfg3.get("sites", [])] == ["Kimi 官方", "中转站B"],
              f"-> {[s['name'] for s in cfg3.get('sites', [])]}")
        check("站点的默认模型也存了", cfg3["sites"][1].get("model") == "claude-sonnet-5-5")
        check("站点地址明文保存", cfg3["sites"][1].get("url") == "https://relay.example.com")
        check("站点 Key 不以明文出现",
              "sk-kimi-123" not in raw3 and "sk-siteb-456" not in raw3)

        root3 = tk.Tk()
        root3.withdraw()
        app3 = ProbeApp(root3)
        check("重启后站点列表还在",
              [s["name"] for s in app3.sites] == ["Kimi 官方", "中转站B"],
              f"-> {[s['name'] for s in app3.sites]}")
        check("重启后仍停在原来的站点", app3.var_site.get() == "Kimi 官方",
              f"-> {app3.var_site.get()}")
        check("重启后站点 Key 自动带出", app3.var_key.get() == "sk-kimi-123",
              f"-> {app3.var_key.get()!r}")
        app3._select_site(ADHOC_SITE)
        check("重启后「自定义」格也还原",
              app3.var_url.get() == "https://my-own-relay.example/v1",
              f"-> {app3.var_url.get()}")
        app3._select_site("中转站B")
        check("重启后切站点 Key 正确", app3.var_key.get() == "sk-siteb-456")

        app3.import_sites_to_batch()
        bt = app3.txt_batch.get("1.0", "end-1c")
        check("批量列表能一键导入所有站点",
              "relay.example.com" in bt and "api.moonshot.cn" in bt, f"-> {bt!r}")
        check("导入的批量行带上了 Key", "sk-siteb-456" in bt)

        check("重命名站点",
              app3._update_site_record("中转站B", "B-改名", "https://relay.example.com",
                                       "sk-siteb-456", "claude-sonnet-5-5") is True)
        check("重命名后下拉同步", "B-改名" in list(app3.cmb_site.cget("values")),
              f"-> {app3.cmb_site.cget('values')}")

        app3._select_site("Kimi 官方")
        check("删除站点成功", app3._delete_site_record("Kimi 官方") is True)
        check("删掉当前站点后回落到「自定义」", app3.var_site.get() == ADHOC_SITE,
              f"-> {app3.var_site.get()}")
        check("删除后只剩一个站点", len(app3.sites) == 1, f"-> {len(app3.sites)}")
        root3.destroy()

        # ---- Claude Code 接入页 ----
        check("Claude Code 标签页已创建", app.tab_claude is not None)
        check("Codex 标签页已创建", app.tab_codex is not None)
        check("标签页总数为 6", len(app.nb.tabs()) == 6, f"-> {len(app.nb.tabs())}")
        check("模型映射有五项", len(app.cc_model_vars) == 5, f"-> {list(app.cc_model_vars)}")

        srv_cc, base_cc = _mock_server("anthropic")
        try:
            app.var_url.set(base_cc)
            app.var_key.set("sk-mock")
            app.fetch_models()
            wait_until(lambda: app.site_result is not None and not app.busy, root)
            check("Anthropic 站拿到 4 个模型", (app.site_result or {}).get("model_count") == 4,
                  f"-> {(app.site_result or {}).get('model_count')}")
            app.cc_refresh_context()
            check("自动匹配到 sonnet",
                  app.cc_model_vars["sonnet"][0].get().startswith("claude-sonnet"),
                  f"-> {app.cc_model_vars['sonnet'][0].get()}")
            check("候选下拉里带上了模型 id",
                  "claude-opus-5-5" in list(app.cc_model_vars["opus"][1].cget("values")))

            # 门禁：生成测试没过之前，接入按钮必须是禁用的
            check("门禁：只拉了列表还不能接入", app.cc_ready()[0] is False,
                  f"-> {app.cc_ready()[1]}")
            check("接入按钮默认禁用", str(app.btn_cc_install["state"]) == "disabled")
            app.var_model.set("claude-sonnet-5-5")
            app.run_generation()
            wait_until(lambda: not app.busy, root)
            check("生成测试通过", (app.site_result or {}).get("ok") is True,
                  f"-> {(app.site_result or {}).get('verdict')}")
            check("门禁：还缺通道检测，仍不可接入", app.cc_ready()[0] is False,
                  f"-> {app.cc_ready()[1]}")
            check("接入按钮仍然禁用", str(app.btn_cc_install["state"]) == "disabled")

            app.cc_check()
            wait_until(lambda: not app.busy, root)
            check("界面里的通道检测通过", (app.cc_state or {}).get("ok") is True,
                  f"-> {(app.cc_state or {}).get('verdict')}")
            check("门禁：两项都通过后解锁", app.cc_ready()[0] is True, f"-> {app.cc_ready()[1]}")
            check("接入按钮已可点", str(app.btn_cc_install["state"]) == "normal",
                  f"-> {app.btn_cc_install['state']}")
            # 通道过了但 ② 没过（例如只代理 Anthropic 的站）：应当解锁并给出提示
            saved_gen_ok = (app.site_result or {}).get("ok")
            app.site_result["ok"] = False
            r_ok, r_why = app.cc_ready()
            check("通道通过而生成测试没过时：解锁 + 提示",
                  r_ok is True and "注意" in r_why, f"-> {r_why}")
            app.site_result["ok"] = saved_gen_ok
            check("恢复后仍解锁", app.cc_ready()[0] is True)
            check("检测结果渲染到表格", len(app.tv_cc.get_children()) >= 5,
                  f"-> {len(app.tv_cc.get_children())}")
            check("检测到流式可用", (app.cc_state or {}).get("stream_ok") is True)

            env, _style = app.cc_build_env()
            check("env 指向当前站点", env.get("ANTHROPIC_BASE_URL") == base_cc,
                  f"-> {env.get('ANTHROPIC_BASE_URL')}")
            check("env 带上了 Key", env.get("ANTHROPIC_AUTH_TOKEN") == "sk-mock")
            check("env 里只有一个 key 变量",
                  ("ANTHROPIC_AUTH_TOKEN" in env) != ("ANTHROPIC_API_KEY" in env))

            app.cc_preview()
            log_txt = app.txt_cc.get("1.0", "end-1c")
            check("预览里 Key 已打码", "sk-mock" not in log_txt and "sk-***" in log_txt,
                  f"-> {'sk-mock' in log_txt}")

            app.cc_write_settings.set(True)
            app.cc_write_launcher.set(True)
            app.cc_write_isolated.set(True)
            app.cc_run_verify.set(False)          # 测试里不真去调 claude CLI
            app.cc_install()
            wait_until(lambda: not app.busy, root, timeout=40)
            check("一键接入写出了 settings.json", CLAUDE_SETTINGS_PATH.exists(),
                  f"-> {CLAUDE_SETTINGS_PATH}")
            saved_cc = json.loads(CLAUDE_SETTINGS_PATH.read_text(encoding="utf-8"))
            check("写入的 BASE_URL 正确",
                  saved_cc["env"]["ANTHROPIC_BASE_URL"] == base_cc,
                  f"-> {saved_cc['env'].get('ANTHROPIC_BASE_URL')}")
            check("写入的 sonnet 映射正确",
                  str(saved_cc["env"].get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""))
                  .startswith("claude-sonnet"))
            bats = list(CLAUDE_OUTPUT_DIR.rglob("*.bat"))
            check("生成了启动脚本", len(bats) >= 2, f"-> {bats}")
            check("启动脚本是纯 ASCII",
                  all(all(b < 128 for b in p.read_bytes()) for p in bats))
            check("接入日志有输出", "一键接入完成" in app.txt_cc.get("1.0", "end-1c"))

            # ---- Codex 页（同一站点：Anthropic mock 也实现了 /v1/responses）----
            check("门禁：Codex 未检测通道时不能接入", app.codex_ready()[0] is False,
                  f"-> {app.codex_ready()[1]}")
            check("Codex 接入按钮默认禁用",
                  str(app.btn_codex_install["state"]) == "disabled")
            app.var_codex_model.set("claude-sonnet-5-5")
            app.codex_check()
            wait_until(lambda: not app.busy, root)
            check("Codex 通道检测通过", (app.codex_state or {}).get("ok") is True,
                  f"-> {(app.codex_state or {}).get('verdict')}")
            check("检测出 wire_api", (app.codex_state or {}).get("wire_api") in ("responses", "chat"),
                  f"-> {(app.codex_state or {}).get('wire_api')}")
            check("检测结果渲染到 Codex 表格", len(app.tv_codex.get_children()) >= 5,
                  f"-> {len(app.tv_codex.get_children())}")
            check("门禁：生成测试 + 通道都过了，解锁",
                  app.codex_ready()[0] is True, f"-> {app.codex_ready()[1]}")
            check("Codex 接入按钮已可点",
                  str(app.btn_codex_install["state"]) == "normal")

            app.codex_write_config.set(True)
            app.codex_write_env.set(True)
            app.codex_write_launcher.set(True)
            app.codex_run_verify.set(False)       # 测试里不真去调 codex CLI
            app.codex_install()
            wait_until(lambda: not app.busy, root, timeout=40)
            check("写出了 config.toml", CODEX_CONFIG_PATH.exists(), f"-> {CODEX_CONFIG_PATH}")
            ctxt = CODEX_CONFIG_PATH.read_text(encoding="utf-8")
            check("config.toml 里有我们的 provider 表",
                  f"[model_providers.{CODEX_PROVIDER_ID}]" in ctxt)
            check("config.toml 的 base_url 带 /v1", "base_url" in ctxt and "/v1" in ctxt)
            check("config.toml 里 wire_api 已写入", "wire_api" in ctxt)
            check("写出了 .env", CODEX_ENV_PATH.exists())
            check(".env 里写进了 Key",
                  "sk-mock" in CODEX_ENV_PATH.read_text(encoding="utf-8"))
            cbats = [p for p in CLAUDE_OUTPUT_DIR.rglob("*.bat") if "codex" in p.name.lower()]
            check("生成了 Codex 启动脚本", bool(cbats), f"-> {cbats}")
            check("Codex 启动脚本是纯 ASCII",
                  all(all(b < 128 for b in p.read_bytes()) for p in cbats))
            check("Codex 日志有输出",
                  "一键接入 Codex 完成" in app.txt_codex.get("1.0", "end-1c"))
            check("测试全程用隔离的 CODEX_HOME",
                  "api_probe_codexhome_" in str(CODEX_CONFIG_PATH), f"-> {CODEX_CONFIG_PATH}")
        finally:
            srv_cc.shutdown()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        fails.append(f"异常: {e}")
    finally:
        try:
            root.destroy()
        except Exception:
            pass
        srv.shutdown()
        CONFIG_PATH = real_config_path
        CLAUDE_SETTINGS_PATH = real_claude_settings
        CLAUDE_OUTPUT_DIR = real_claude_outdir
        CODEX_HOME = real_codex_home
        CODEX_CONFIG_PATH = real_codex_config
        CODEX_ENV_PATH = real_codex_env

    if fails:
        print(f"界面流程自检失败 {len(fails)} 项：{fails}")
        return 1
    print("  [PASS] 界面两段式流程全部通过")
    return 0


# =====================================================================
# 十一、命令行
# =====================================================================

def cli_probe(args) -> int:
    def log(msg):
        if not args.json:
            print(msg)

    if args.list_only:
        res = probe_site(args.url, args.key, timeout=args.timeout,
                         verify=not args.no_verify, log=log)
        if args.json:
            print(json.dumps({"base": res["base"], "protocol": res["protocol_label"],
                              "fingerprint": res["fingerprint"],
                              "model_count": res["model_count"],
                              "verdict": res["verdict"],
                              "models": [m["id"] for m in res["models"]]},
                             ensure_ascii=False, indent=2))
        else:
            print()
            print(f"结论        : {res['verdict']}")
            print(f"归一化地址  : {res['base']}")
            print(f"协议族      : {res['protocol_label']}")
            print(f"站点指纹    : {res['fingerprint'] or '-'}")
            print(f"模型数量    : {res['model_count']}")
            print("\n模型列表：")
            for m in res["models"]:
                print(f"  - {m['id']}" + (f"   [{m['owner']}]" if m.get("owner") else ""))
            print("\n下一步：挑一个模型，加 --model 再跑一次即可做生成测试。")
        return 0 if res.get("ok_site") else 2

    if args.claude_check:
        base = normalize_base(args.url)
        chk = check_anthropic_channel(base, args.key, args.model or "", timeout=args.timeout,
                                      verify=not args.no_verify, log=log)
        if args.json:
            print(json.dumps(chk, ensure_ascii=False, indent=2))
            return 0 if chk["ok"] else 2
        print()
        print(f"结论        : {chk['verdict']}")
        print(f"端点        : {chk['url']}")
        print(f"认证方式    : {chk['auth_style'] or '-'}")
        print(f"HTTP 状态   : {chk['status']}")
        print(f"非流式耗时  : {chk['latency_ms'] or '-'} ms")
        print(f"流式首字    : {chk['first_ms'] or '-'} ms")
        print(f"返回内容    : {chk['reply'] or '-'}")
        for t in chk.get("tried", []):
            print(f"  尝试 {t.get('auth_style'):9} HTTP {t.get('status') or '-'}　{t.get('error') or ''}")
        env = build_claude_env(base, args.key or "sk-xxx", chk["auth_style"] or "bearer",
                               {}, args.model or "")
        print("\n可写入 ~/.claude/settings.json 的 env（示意，Key 已替换）：")
        print(json.dumps({"env": {k: (mask_key(v) if "KEY" in k or "TOKEN" in k else v)
                                  for k, v in env.items()}}, ensure_ascii=False, indent=2))
        return 0 if chk["ok"] else 2

    if args.codex_check:
        base = normalize_base(args.url)
        chk = check_openai_channel(base, args.key, args.model or "", timeout=args.timeout,
                                   verify=not args.no_verify, log=log)
        if args.json:
            print(json.dumps(chk, ensure_ascii=False, indent=2))
            return 0 if chk["ok"] else 2
        print()
        print(f"结论           : {chk['verdict']}")
        print(f"Codex base_url : {chk['base_url']}")
        print(f"wire_api       : {chk['wire_api'] or '-'}")
        print(f"HTTP 状态      : {chk['status']}")
        print(f"耗时           : {chk['latency_ms'] or '-'} ms")
        print(f"返回内容       : {chk['reply'] or '-'}")
        for t in chk.get("tried", []):
            print(f"  尝试 {t.get('wire_api'):10} HTTP {t.get('status') or '-'}　{t.get('error') or ''}")
        print("\n可写进 ~/.codex/config.toml 的 provider 段（Key 不放配置里）：")
        print(render_codex_provider_block(chk["base_url"] or "<BASE>/v1", "CODEX_API_KEY",
                                          chk["wire_api"] or "responses", "API Probe"))
        return 0 if chk["ok"] else 2

    res = probe(args.url, args.key, model=args.model or "", timeout=args.timeout,
                verify=not args.no_verify, deep=not args.no_deep, log=log)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2
    print()
    print(f"结论        : {res['verdict']}")
    print(f"归一化地址  : {res['base']}")
    print(f"协议族      : {res['protocol_label']}")
    print(f"站点指纹    : {res['fingerprint'] or '-'}")
    print(f"HTTP 状态   : {res['http_status']}")
    print(f"模型数量    : {res.get('model_count', 0)}")
    print(f"请求模型    : {res['used_model']}")
    print(f"返回模型    : {res['returned_model'] or '-'}")
    ft = res.get("first_token_ms")
    print(f"首字延迟    : {ft:.0f} ms" if ft is not None else "首字延迟    : -")
    print(f"usage       : {json.dumps(res['usage'], ensure_ascii=False) if res.get('usage') else '-'}")
    if args.show_models and res.get("models"):
        print("\n模型列表：")
        for m in res["models"]:
            print(f"  - {m['id']}" + (f"   [{m['owner']}]" if m.get("owner") else ""))
    print("\n检查项：")
    for lvl, text in res.get("warnings", []):
        print(f"  [{lvl:5}] {text}")
    return 0 if res.get("ok") else 2


def _fix_console_encoding() -> None:
    """让 Windows 控制台/管道输出中文不乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def main(argv=None) -> int:
    _fix_console_encoding()
    ap = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="API 地址（可含 /v1 等后缀）")
    ap.add_argument("--key", default="", help="API Key")
    ap.add_argument("--model", default="", help="指定用于生成测试的模型")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--no-verify", action="store_true", help="跳过 TLS 证书校验")
    ap.add_argument("--no-deep", action="store_true", help="关闭深度检测（少发一次请求）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    ap.add_argument("--show-models", action="store_true", help="打印完整模型列表")
    ap.add_argument("--list-only", action="store_true",
                    help="只做第 1 步：拉取模型列表后退出（不做生成测试）")
    ap.add_argument("--claude-check", action="store_true",
                    help="检测网关是否实现 Anthropic Messages 协议（Claude Code 直连的前提）")
    ap.add_argument("--codex-check", action="store_true",
                    help="检测网关的 OpenAI 通道（/v1/responses 与 /v1/chat/completions），判断 Codex 的 wire_api")
    ap.add_argument("--selftest", action="store_true", help="运行内置自检")
    ap.add_argument("--uitest", action="store_true", help="运行图形界面构建自检")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.uitest:
        return uitest()
    if args.url:
        return cli_probe(args)
    try:
        return run_gui()
    except Exception as e:  # noqa: BLE001
        print(f"无法启动图形界面：{e}\n可改用命令行：python api_probe.py --url URL --key KEY", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
