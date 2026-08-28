#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
live-brain 直播弹幕自动回复中枢 (101机器)
链路: 弹幕文件(D:\program\FlyAiLive\logs\用户发言记录_*.txt 增量tail)
      -> 过滤/队列 -> RAG(C:\lightrag :9621) 取知识 + LLM(192.168.5.100:8002 deepseek-v4-flash) 生成口播
      -> VSA TTS(:23456 bert-vits2) 合成 wav -> 本机默认声卡播放(winsound/winmm, 进OBS虚拟声卡)
控制面: http://0.0.0.0:23460  (/ 状态页 /api/status /test /say /pause /resume /health)
仅用 Python 标准库。
"""
import json
import random
import os
import re
import sys
import time
import queue
import struct
import http.client
import ctypes
import socket
import base64
import hashlib
import threading
import traceback
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- 配置 ----------------
LOG_DIR        = r"D:\program\FlyAiLive\logs"
BRAIN_PORT     = int(os.environ.get("BRAIN_PORT", "23460"))
VSA_TTS_URL    = os.environ.get("VSA_TTS_URL", "http://127.0.0.1:23456/voice/bert-vits2")
VOICE_ID       = os.environ.get("BRAIN_VOICE_ID", "1")          # 0=sdd 1=zxy
RAG_URL        = os.environ.get("RAG_URL", "http://127.0.0.1:9621/query")
LLM_BASE       = os.environ.get("LLM_BASE", "http://192.168.5.100:8002/v1")
LLM_MODEL      = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
OBS_HOST       = os.environ.get("OBS_WS_HOST", "127.0.0.1")
OBS_PORT       = int(os.environ.get("OBS_WS_PORT", "4455"))
OBS_PASSWORD   = os.environ.get("OBS_PASSWORD", "")   # 本机OBS无鉴权
DUCK_KINDS     = ("ffmpeg_source", "vlc_source", "browser_source", "game_capture")
POLL_INTERVAL  = float(os.environ.get("POLL_INTERVAL", "1.5"))
USER_COOLDOWN  = float(os.environ.get("USER_COOLDOWN", "15"))   # 同一用户N秒内只回一条
REPLY_MAXLEN   = 80                                             # 回复硬截断
QUEUE_SIZE     = 32

# ---------------- 可调配置(WebUI可改, 持久化config.json) ----------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_CONFIG = {
    "duck_delay_ms": 0,      # 视频源静音完成后, 延迟多少毫秒再开始播放TTS
    "duck_fade_ms": 0,       # 播报时视频声音淡出时长(0=立刻静音)
    "unduck_fade_ms": 0,     # 播完后视频声音淡入时长(0=立刻恢复)
    "voice_id": 1,           # VSA音色: 0=sdd 1=zxy
    # ---- TTS 播报控制(VSA透传+播放端后处理) ----
    "tts_volume": 130,          # 音量增益% (0~200)
    "tts_normalize": True,      # 峰值归一化(防爆音, 压到-1dBFS)
    "tts_eq": True,             # 音质EQ(切低频浑浊+提2-4k清晰度, 对齐视频原声)
    "tts_speed": 100,           # 语速% (60~160; 映射VSA length=100/speed)
    "tts_sdp": 30,              # 语调抑扬 sdp_ratio(0~100 -> 0~1.0)
    "tts_noise": 33,            # 情感起伏 noise(20~80 -> /100)
    "tts_emotion": "normal",    # normal/happy/angry/sad/experiment(实验通道)
    "gap_min_ms": 250,          # 句间最小停顿ms
    "gap_max_ms": 700,          # 句间最大停顿ms
    "filter_emoji": True,       # 过滤纯表情弹幕(emoji/淘宝表情码/礼物计数)
    "reply_mode": "instant",    # 回复模式: instant=逐条即答 / smart=同人聚合(窗口内多条合并回复)
    "agg_window_s": 6,          # 智能聚合窗口秒数(3~15): 首条弹幕起等N秒收集同观众后续弹幕
    "user_cooldown": 15,     # 同一观众N秒内只回一条
    "rag_enabled": True,     # 是否查询知识库增强回复
    "llm_main": {            # 主模型
        "base_url": os.environ.get("LLM_BASE", "http://192.168.5.100:8002/v1"),
        "model": os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
        "api_key": "EMPTY",
    },
    "llm_backup": {          # 备用模型(base_url留空=不启用); 主模型连续失败自动切换
        "base_url": "",
        "model": "",
        "api_key": "",
    },
    "system_prompt": "你是一家水晶直播间的助播，替主播生成对着观众说的口播回复。",
    "reply_include_nick": True,    # 回复里是否自然带上观众昵称
    "reply_echo_danmu": False,     # 回复里是否先简短复述观众的发言
    "scene_enabled": True,         # 弹幕关键词自动切场景(替代lua插件)
    "scene_switch_delay_s": 3,     # 检测到指令后延迟几秒切换(窗口内新指令覆盖旧定时)
    "scene_hold_s": 180,           # 切换后保持秒数, 到期自动回默认场景; 期间锁定忽略新指令
}
CONFIG = dict(DEFAULT_CONFIG)

# ---------------- 场景表(cmd -> OBS实际场景名, 已按101机器OBS实测对齐) ----------------
SCENE_TABLE = [
    {"cmd": "默认",  "name": "默认"},
    {"cmd": "6",     "name": "6"},
    {"cmd": "8",     "name": "8"},
    {"cmd": "10",    "name": "10"},
    {"cmd": "12",    "name": "12"},
    {"cmd": "14",    "name": "14"},
    {"cmd": "16",    "name": "16"},
    {"cmd": "10泡",  "name": "10泡"},
    {"cmd": "12泡",  "name": "12泡"},
    {"cmd": "14泡",  "name": "14泡"},
    {"cmd": "16泡",  "name": "16泡"},
]
SCENE_BY_CMD = {s["cmd"]: s for s in SCENE_TABLE}
PLAIN_LEVELS = [6, 8, 10, 12, 14, 16]
BUBBLE_LEVELS = [10, 12, 14, 16]
CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}

SCENE_STATE = {
    "pending": None,        # {"at": ts, "cmd": str}
    "hold_until": None,     # 锁定期截止 ts
    "switch_count": 0,
}

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        for k in DEFAULT_CONFIG:
            if k in d:
                if isinstance(DEFAULT_CONFIG[k], dict) and isinstance(d[k], dict):
                    CONFIG[k].update(d[k])      # 嵌套dict(llm_main等)合并而非覆盖
                else:
                    CONFIG[k] = d[k]
        log("config loaded: llm_main=%s/%s backup=%s" %
            (CONFIG["llm_main"]["base_url"], CONFIG["llm_main"]["model"],
             "on" if CONFIG["llm_backup"].get("base_url") else "off"))
    except FileNotFoundError:
        log("no config.json, using defaults")
    except Exception as e:
        err("load config: %s" % e)

def save_config():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        err("save config: %s" % e)
        return False

STATE = {
    "enabled": True,
    "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "watched": "",
    "offset": 0,
    "queue": 0,
    "seen": 0, "replied": 0, "failed": 0,
    "skip_cooldown": 0, "skip_dup": 0, "skip_short": 0, "dropped_full": 0,
    "last_danmu": None,
    "last_reply": None,
    "playing": False,
    "obs_ok": False,
    "duck_active": False,
    "duck_muted": 0,
    "llm_model": "-",
    "failovers": 0,
    "current_scene": "默认",
    "last_scene_detect": "",
    "scene_switches": 0,
    "errors": deque(maxlen=5),
}
ITEMS = deque(maxlen=8)            # 最近处理记录
RECENT_TEXTS = deque(maxlen=50)    # 去重
COOLDOWN = {}                      # nick -> ts
Q = queue.Queue(maxsize=QUEUE_SIZE)
LOCK = threading.Lock()
PLAY_LOCK = threading.Lock()

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\[用户发言\](.*)$")
IGNORE_TEXTS = {"6", "66", "666", "6666", "哈哈", "哈哈哈", "来了", "签到"}
# 控制字符/零宽字符: 真实弹幕里混入会导致下游JSON解析422
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2028\u2029\u2060\ufeff]")
# ---- 表情弹幕过滤(2026-08-27): 剥离纯表情类弹幕, 只留有实际内容的发言 ----
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF"
    "\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF\uFE0F\u2b50\u3030\u303d\u3297\u3299\u2705\u274c]"
)
TAOBAO_FACE_RE = re.compile(r"\[-?[^][]*\]")      # 淘宝方括号表情码: [666] [-哈哈哈] [-鼓掌]
GIFT_COUNTER_RE = re.compile(r"^[\s\U0001F000-\U0001FAFF]*(?:减|点|送|赞|亮了|收藏|分享)\s*[0-9０-９]*[\s.。!！]*$")  # 礼物互动计数噪音

def effective_text(s):
    """剥离表情后剩余的有效文本(用于判断是否纯表情弹幕)"""
    s = TAOBAO_FACE_RE.sub("", s)
    s = EMOJI_RE.sub("", s)
    return CTRL_RE.sub("", s).strip()

def clean_text(s):
    return CTRL_RE.sub("", s).strip()

def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"), exist_ok=True)
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "brain.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except UnicodeEncodeError:                  # Windows GBK控制台遇emoji(2026-08-27): 降级输出防scan线程中断
        try:
            import sys
            print(line.encode("gbk", "replace").decode("gbk"), flush=True)
        except Exception:
            pass

def err(msg):
    log("ERROR: " + msg)
    with LOCK:
        STATE["errors"].append("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg[:180]))

# ---------------- 弹幕文件监控 ----------------
OFFSETS = {}    # path -> 已读偏移
REMAIN = {}     # path -> 不完整行残留bytes

def find_newest_record():
    """返回最新的 用户发言记录*.txt 路径"""
    best, best_m = None, -1
    try:
        for name in os.listdir(LOG_DIR):
            if name.startswith("用户发言记录") and name.endswith(".txt"):
                p = os.path.join(LOG_DIR, name)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if m > best_m:
                    best, best_m = p, m
    except FileNotFoundError:
        pass
    return best

def parse_line(raw):
    """解析一行弹幕 -> (time_str, nick, text) 或 None"""
    try:
        s = raw.decode("utf-8", errors="replace").strip("\r\n")
    except Exception:
        return None
    m = LINE_RE.match(s)
    if not m:
        return None
    t, content = m.group(1), m.group(2).strip()
    content = clean_text(content.lstrip("「").rstrip("」"))
    if "：" in content:
        nick, text = content.split("：", 1)
    elif ":" in content:
        nick, text = content.split(":", 1)
    else:
        nick, text = "观众", content
    return t, clean_text(nick), clean_text(text)

def scan_once(mypath=None):
    """增量读取文件(默认最新用户发言记录, 可指定如淘宝桥接文件)，新弹幕入队"""
    path = mypath or find_newest_record()
    if not path:
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    first_seen = path not in OFFSETS
    if first_seen:
        OFFSETS[path] = size            # 首次见到跳过历史
        REMAIN[path] = b""
        with LOCK:
            STATE["watched"] = os.path.basename(path)
            STATE["offset"] = size
        return
    off = OFFSETS[path]
    if size == off:
        return
    if size < off:                      # 文件被轮转/重写
        OFFSETS[path] = size
        REMAIN[path] = b""
        return
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
    except OSError as e:
        err("read %s: %s" % (path, e))
        return
    OFFSETS[path] = off + len(data)
    buf = REMAIN.pop(path, b"") + data
    lines = buf.split(b"\n")
    if buf.endswith(b"\n"):
        rem = b""
    else:
        rem = lines.pop()               # 最后一段可能不完整
    REMAIN[path] = rem
    for ln in lines:
        parsed = parse_line(ln)
        if parsed:
            handle_danmu(*parsed)

# ---------------- 队列与过滤 ----------------
# 智能聚合模式(2026-08-27): 同一观众窗口期内多条弹幕合并成一条再回复
PENDING = {}          # nick -> {"texts": [..], "timer": threading.Timer}
PENDING_LOCK = threading.Lock()

def _flush_pending(nick):
    """窗口到期: 把该观众缓冲的多条弹幕合并入队"""
    with PENDING_LOCK:
        ent = PENDING.pop(nick, None)
    if not ent:
        return
    texts = ent["texts"]
    if not texts:
        return
    if len(texts) == 1:
        merged = texts[0]
    else:
        merged = " / ".join(texts)
        with LOCK:
            STATE["agg_merged"] = STATE.get("agg_merged", 0) + len(texts) - 1
    log("agg-flush [%s]: %d条合并 => %s" % (nick[:12], len(texts), merged[:50]))
    enqueue(nick, merged)

def handle_danmu(t, nick, text):
    nick = clean_text(nick)
    text = clean_text(text)
    if not text:
        return
    scene_tag = scene_on_danmu(text) if CONFIG.get("scene_enabled", True) else None
    with LOCK:
        STATE["seen"] += 1
        STATE["last_danmu"] = {"time": t, "nick": nick, "text": text}
    # 表情弹幕过滤: 剥掉emoji/淘宝表情码/礼物计数后无实际内容 → 丢弃(不进RAG/LLM/TTS)
    if CONFIG.get("filter_emoji", True):
        eff = effective_text(text)
        if not eff or len(eff) < 2 or GIFT_COUNTER_RE.match(eff):
            with LOCK:
                STATE["skip_emoji"] = STATE.get("skip_emoji", 0) + 1
            log("filter-emoji: %s[%s]" % (nick[:12], text[:20]))
            return
    if len(text) < 2 or text in IGNORE_TEXTS:
        with LOCK:
            STATE["skip_short"] += 1
        return
    key = text.lower().strip()
    if key in RECENT_TEXTS:
        with LOCK:
            STATE["skip_dup"] += 1
        return
    now = time.time()
    # 智能聚合模式: 同观众弹幕进缓冲窗, 窗尾合并回复; 即时模式: 直接入队(原逻辑)
    if CONFIG.get("reply_mode", "instant") == "smart":
        win = max(2.0, min(float(CONFIG.get("agg_window_s", 6)), 15.0))
        with PENDING_LOCK:
            ent = PENDING.get(nick)
            if ent:
                ent["texts"].append(text)
                # 已有窗在跑: 弹幕追加, 窗口尾不变(首条起算)
            else:
                ent = {"texts": [text]}
                timer = threading.Timer(win, _flush_pending, args=(nick,))
                timer.daemon = True
                ent["timer"] = timer
                PENDING[nick] = ent
                timer.start()
        COOLDOWN[nick] = now
        return
    last = COOLDOWN.get(nick, 0)
    if now - last < float(CONFIG.get("user_cooldown", USER_COOLDOWN)):
        with LOCK:
            STATE["skip_cooldown"] += 1
        return
    COOLDOWN[nick] = now
    RECENT_TEXTS.append(key)
    enqueue(nick, text)

def enqueue(nick, text, synthetic=False):
    try:
        Q.put_nowait({"nick": nick, "text": text, "synthetic": synthetic})
    except queue.Full:
        try:
            Q.get_nowait()              # 丢最旧
        except queue.Empty:
            pass
        try:
            Q.put_nowait({"nick": nick, "text": text, "synthetic": synthetic})
        except queue.Full:
            pass
        with LOCK:
            STATE["dropped_full"] += 1
    with LOCK:
        STATE["queue"] = Q.qsize()

# ---------------- RAG / LLM / TTS ----------------
def http_json(url, payload=None, timeout=30, method=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return body

def rag_lookup(question):
    """查知识库(仅取上下文,不触发LLM), 3秒熔断。失败自动降级重试一次再放弃"""
    q = CTRL_RE.sub("", question)[:60]          # 控制字符清洗防422
    for attempt, timeout in ((1, 3), (2, 3)):   # 首次熔断3s; 失败快速重试1次
        try:
            d = http_json(RAG_URL, {"query": q, "mode": "naive",
                                     "only_need_context": True}, timeout=timeout)
            resp = (d.get("response") or "").strip() if isinstance(d, dict) else ""
            if len(resp) < 12 or "no-context" in resp.lower():
                return ""
            return resp[:400]
        except Exception as e:
            err("rag attempt%d fail(%s)" % (attempt, str(e)[:60]))
            STATE["rag_skip"] = STATE.get("rag_skip", 0) + 1
    return ""

def apply_config(d):
    """校验并应用前端提交的配置"""
    def num(key, lo, hi, cast=int):
        if key in d:
            try:
                CONFIG[key] = max(lo, min(cast(float(d[key])), hi))
            except (TypeError, ValueError):
                pass
    num("duck_delay_ms", 0, 10000)
    num("duck_fade_ms", 0, 10000)
    num("unduck_fade_ms", 0, 10000)
    num("user_cooldown", 0, 300, float)
    if "voice_id" in d:
        try:
            v = int(float(d["voice_id"]))
            if v in (0, 1):
                CONFIG["voice_id"] = v
        except (TypeError, ValueError):
            pass
    if "rag_enabled" in d:
        CONFIG["rag_enabled"] = bool(d["rag_enabled"])
    if "reply_include_nick" in d:
        CONFIG["reply_include_nick"] = bool(d["reply_include_nick"])
    if "reply_echo_danmu" in d:
        CONFIG["reply_echo_danmu"] = bool(d["reply_echo_danmu"])
    if "scene_enabled" in d:
        CONFIG["scene_enabled"] = bool(d["scene_enabled"])
    num("scene_switch_delay_s", 0, 60)
    num("scene_hold_s", 0, 3600)
    # TTS 播报控制
    num("tts_volume", 0, 200)
    num("tts_speed", 60, 160)
    num("tts_sdp", 0, 100)
    num("tts_noise", 20, 80)
    num("gap_min_ms", 0, 1500)
    num("gap_max_ms", 0, 2000)
    if "tts_normalize" in d:
        CONFIG["tts_normalize"] = bool(d["tts_normalize"])
    if "tts_eq" in d:
        CONFIG["tts_eq"] = bool(d["tts_eq"])
    if "filter_emoji" in d:
        CONFIG["filter_emoji"] = bool(d["filter_emoji"])
    if isinstance(d.get("reply_mode"), str) and d["reply_mode"] in ("instant", "smart"):
        CONFIG["reply_mode"] = d["reply_mode"]
    num("agg_window_s", 3, 15)
    if isinstance(d.get("tts_emotion"), str) and d["tts_emotion"] in ("normal","happy","angry","sad","experiment"):
        CONFIG["tts_emotion"] = d["tts_emotion"]
    if CONFIG.get("gap_max_ms", 0) < CONFIG.get("gap_min_ms", 0):
        CONFIG["gap_max_ms"] = CONFIG["gap_min_ms"]
    if isinstance(d.get("system_prompt"), str):
        sp = d["system_prompt"].strip()
        CONFIG["system_prompt"] = sp or DEFAULT_CONFIG["system_prompt"]
    for slot in ("llm_main", "llm_backup"):
        if isinstance(d.get(slot), dict):
            cur = dict(CONFIG[slot])
            sub = d[slot]
            if "base_url" in sub:
                cur["base_url"] = str(sub["base_url"]).strip().rstrip("/")
            if "model" in sub:
                cur["model"] = str(sub["model"]).strip()
            if "api_key" in sub and str(sub["api_key"]).strip():
                cur["api_key"] = str(sub["api_key"]).strip()   # 留空=不改动已存key
            CONFIG[slot] = cur
    save_config()
    log("config updated: main=%s/%s backup=%s nick=%s echo=%s" %
        (CONFIG["llm_main"]["base_url"], CONFIG["llm_main"]["model"],
         (CONFIG["llm_backup"]["base_url"] + "/" + CONFIG["llm_backup"]["model"])
         if CONFIG["llm_backup"].get("base_url") else "off",
         CONFIG["reply_include_nick"], CONFIG["reply_echo_danmu"]))

SYSTEM_PROMPT = (
    "你是一家水晶直播间的助播，替主播生成对着观众说的口播回复。"
    "规则：1.只输出一句口语化中文短句，不超过40个字；"
    "2.禁止表情符号、序号、引号、括号；"
    "3.有参考资料时优先依据资料回答，没有则凭常识简短回应或自然引导点赞关注；"
    "4.不透露自己是AI。"
)

LLM_STATE = {"use_backup": False, "fail_count": 0}   # 主模型连续失败>=2 切备用; 备用失败>=5 探测回主
LLM_LOCK = threading.Lock()

def _llm_chat_once(base_url, model, api_key, msgs, timeout=20):
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    key = (api_key or "").strip()
    if key and key != "EMPTY":
        headers["Authorization"] = "Bearer " + key
    payload = {"model": model, "messages": msgs, "max_tokens": 400,
               "temperature": 0.8}
    # 推理型模型处理: OpenRouter类(glm-5.3-flash)强制推理→reasoning.max_tokens限思考+exclude;
    # 注意reasoning_effort与reasoning.max_tokens互斥(400) → 只留max_tokens, 不发reasoning_effort
    payload["reasoning"] = {"max_tokens": 120, "exclude": True}
    for k, v in (("enable_thinking", False),
                 ("thinking", {"type": "disabled"}), ("chat_template_kwargs", {"thinking": False})):
        payload[k] = v
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    # OpenRouter等: reasoning模型可能把内容放reasoning, content为null/空 → 回退拼接
    reply = (msg.get("content") or "").strip()
    if not reply:
        reply = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
        reply = re.sub(r"\s+", " ", reply)[-REPLY_MAXLEN:]   # 兜底取末尾结论
    if not reply:
        raise RuntimeError("空回复(全部被推理消耗, max_tokens=80不足)")
    for ch in "「」『』“”‘’\"'":
        reply = reply.replace(ch, "")
    reply = re.sub(r"\s+", " ", reply).strip()
    actual = d.get("model") or model
    return reply[:REPLY_MAXLEN], str(actual)

def _pick_llm():
    """按故障状态选当前应使用的模型配置"""
    with LLM_LOCK:
        if LLM_STATE["use_backup"] and CONFIG["llm_backup"].get("base_url"):
            return CONFIG["llm_backup"], True
        return CONFIG["llm_main"], False

def _force_switch_llm():
    """手动在主备之间切换(重试策略用)"""
    with LLM_LOCK:
        if LLM_STATE["use_backup"]:
            LLM_STATE["use_backup"] = False
            log("LLM switch -> main(%s)" % CONFIG["llm_main"]["model"])
        elif CONFIG["llm_backup"].get("base_url"):
            LLM_STATE["use_backup"] = True
            log("LLM switch -> backup(%s)" % CONFIG["llm_backup"]["model"])

def _llm_report(ok):
    """反馈成功/失败, 决定是否切换主备"""
    with LLM_LOCK:
        if ok:
            LLM_STATE["fail_count"] = 0
            return
        LLM_STATE["fail_count"] += 1
        using_backup = LLM_STATE["use_backup"]
        limit = 5 if using_backup else 2
        if LLM_STATE["fail_count"] >= limit:
            new_state = not using_backup
            # 目标是"切回主模型"时要求主模型有配置; 切到备用要求备用已配置
            target_ok = CONFIG["llm_main"].get("base_url") if not new_state \
                else CONFIG["llm_backup"].get("base_url")
            if target_ok:
                LLM_STATE["use_backup"] = new_state
                LLM_STATE["fail_count"] = 0
                with LOCK:
                    STATE["failovers"] += 1
                log("LLM FAILOVER -> %s" % ("backup" if new_state else "main"))

def llm_reply(nick, text, knowledge):
    main, backup = CONFIG["llm_main"], CONFIG["llm_backup"]
    rules = []
    rules.append("只输出一句口语化中文短句，不超过40个字。")
    rules.append("禁止表情符号、序号、引号、括号、换行。")
    if CONFIG.get("reply_include_nick", True):
        rules.append("回复中自然地带上观众的名字（如「%s」），但不要每次都用同样句式。" % nick)
    else:
        rules.append("不要提到观众的名字。")
    if CONFIG.get("reply_echo_danmu", False):
        rules.append("先用几个字简短点出观众问了什么，再回答。")
    else:
        rules.append("直接回答，不复述观众的发言。")
    rules.append("有参考资料时优先依据资料回答；没有则凭常识简短回应或自然引导点赞关注。")
    rules.append("不透露自己是AI，语气像熟悉产品的主播本人。")
    sys_prompt = (CONFIG.get("system_prompt") or DEFAULT_CONFIG["system_prompt"]).rstrip()
    msgs = [
        {"role": "system", "content": sys_prompt + "\n规则：" + "；".join(rules)},
        {"role": "user", "content":
            "观众弹幕：%s\n【参考资料】%s\n请给出口播回复：" % (text, knowledge or "（无）")},
    ]
    last_exc = None
    for attempt in range(3):                    # 单模型内重试(429/瞬时故障)
        cfg, is_bk = _pick_llm()
        try:
            reply, actual_m = _llm_chat_once(cfg["base_url"], cfg["model"], cfg.get("api_key", ""), msgs)
            with LOCK:
                STATE["llm_model"] = actual_m + ("(备)" if is_bk else "")
            _llm_report(True)
            return reply
        except Exception as e:
            last_exc = e
            _llm_report(False)
            log("llm[%s] attempt %d failed: %s" % (cfg["model"], attempt + 1, e))
            time.sleep(min(2 * (attempt + 1), 4))
    raise last_exc

EMOTION_PRESETS = {
    "normal": {"emotion": 0, "text_prompt": ""},
    "happy":  {"emotion": 5, "text_prompt": "Happy"},
    "angry":  {"emotion": 7, "text_prompt": "Angry"},
    "sad":    {"emotion": 3, "text_prompt": "Sad"},
}
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;…])\s*")

def tts_synth_one(seg):
    """合成单句(带语速/语调/情绪参数)"""
    emo = EMOTION_PRESETS.get(CONFIG.get("tts_emotion", "normal"), EMOTION_PRESETS["normal"])
    speed = max(60, min(int(CONFIG.get("tts_speed", 100)), 160))
    qs = {
        "text": seg, "id": str(CONFIG.get("voice_id", VOICE_ID)),
        "format": "wav", "lang": "auto",
        "length": round(100.0 / speed, 3),
        "sdp_ratio": round(max(0, min(int(CONFIG.get("tts_sdp", 30)), 100)) / 100.0, 3),
        "noise": round(max(20, min(int(CONFIG.get("tts_noise", 33)), 80)) / 100.0, 3),
        "noisew": 0.4,
    }
    if CONFIG.get("tts_emotion") == "experiment":
        qs["emotion"] = str(emo["emotion"])
        if emo["text_prompt"]:
            qs["text_prompt"] = emo["text_prompt"]
            qs["style_weight"] = "0.8"
    qstr = urllib.parse.urlencode(qs)
    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(VSA_TTS_URL + "?" + qstr)
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
            if body[:4] != b"RIFF":
                raise RuntimeError("tts非wav: %s..." % body[:60])
            return body
        except Exception as e:
            last = e
            time.sleep(0.6)
    raise last

def _split_sentences(text):
    """按标点分句; 超长句按逗号二分"""
    parts = [p for p in _SENT_SPLIT.split(text.strip()) if p.strip()]
    out = []
    for p in parts:
        cut = max(p.find("，"), p.find(","))
        if len(p) > 60 and cut > 5:
            out.append(p[:cut+1].strip()); out.append(p[cut+1:].strip())
        else:
            out.append(p.strip())
    return [x for x in out if x]

def _apply_eq(data, sr):
    """参数化EQ对齐视频原声: 切低频嗡声+提2-4kHz清晰度+柔和高频空气感
    (2026-08-27 频谱实测: TTS低频29.5%/清晰区0.9% vs 视频12.4%/3.2%)"""
    import numpy as np
    n = len(data)
    if n < 64:
        return data
    # rfft 频域处理
    spec = np.fft.rfft(data)
    freqs = np.fft.rfftfreq(n, 1.0/sr)
    # 1) 低切: 110Hz以下-4dB/倍频程 → 80-250嗡声能量砍半
    hp = np.ones_like(freqs)
    m1 = freqs < 110
    hp[m1] = np.maximum(10**(-4.0/20 * np.log2(np.maximum(110,1)/np.maximum(freqs[m1],1))), 0.05)
    # 简化: Butterworth 2阶高通 (用解析近似)
    wc = 110.0 / (sr/2)
    hp = 1.0 / (1.0 + (wc/np.maximum(freqs/ (sr/2), 1e-6))**2)
    # 2) 中高清晰度峰: 2.8kHz +4.5dB Q=1.4
    f0, g_db, Q = 2800.0, 4.5, 1.4
    A = 10**(g_db/40)
    w0 = 2*np.pi*f0/sr
    alpha = np.sin(w0)/(2*Q)
    # 频域近似 peaking EQ
    peak_gain = np.ones_like(freqs)
    # (对全频段应用 peaking 曲线)
    fw = freqs/f0
    peak_gain = 10**((g_db/20) / (1 + (np.maximum(fw, 1e-6) - 1/fw)**2 * Q**2))
    # 3) 高频空气感: 5kHz以上 shelf +2dB
    shelf = np.ones_like(freqs)
    shelf[freqs >= 5000] = 10**(2.0/20)
    # 组合
    spec *= hp * peak_gain * shelf
    return np.fft.irfft(spec, n=n)

def _process_wav(body):
    """播放端后处理: 音量增益+可选峰值归一化"""
    try:
        import numpy as np
        fmt, (doff, dlen) = _parse_wav(body)
        _, ch, sr, abr, ba, bits = struct.unpack("<HHIIHH", fmt[:16])
        if bits != 16:
            return body
        data = np.frombuffer(body[doff:doff+dlen], dtype="<i2").astype(np.float64) / 32768.0
        gain = max(0, min(float(CONFIG.get("tts_volume", 130)), 200)) / 100.0
        data *= gain
        if CONFIG.get("tts_normalize", True):
            peak = float(np.max(np.abs(data))) or 1.0
            target = 10 ** (-1.0 / 20)          # -1dBFS
            if peak > target:
                data *= target / peak           # 只压不拉
        data = np.clip(data, -1.0, 1.0)
        # ---- 音质均衡EQ: 对齐视频原声频谱特性 (2026-08-27 实测分析) ----
        # TTS低频(80-250Hz)能量占29.5% vs 视频12.4% -> 高切浑浊感
        # TTS清晰度区(2-4kHz)仅0.9% vs 视频带泛音 -> 提亮
        if CONFIG.get("tts_eq", True):
            try:
                data = _apply_eq(data, sr)
            except Exception as e:
                err("eq: %s" % e)
        pcm = (data * 32767).astype("<i2").tobytes()
        out = bytearray(body[:doff])
        out += struct.pack("<I", len(pcm)) + pcm
        struct.pack_into("<I", out, 4, len(out) - 8)
        return bytes(out)
    except Exception as e:
        err("wav process: %s" % e)
        return body

def _concat_wavs(parts):
    import numpy as np
    chunks, sr_ref, ch_n, bits = [], None, 1, 16
    for b in parts:
        if b[:4] != b"RIFF":
            continue
        fmt, (doff, dlen) = _parse_wav(b)
        _, ch, sr, _, ba, bi = struct.unpack("<HHIIHH", fmt[:16])
        if sr_ref is None:
            sr_ref, ch_n, bits = sr, ch, bi
        chunks.append(np.frombuffer(b[doff:doff+dlen], dtype="<i2"))
    pcm = np.concatenate(chunks).astype("<i2").tobytes() if chunks else b""
    byte_rate = sr_ref * ch_n * bits // 8
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    block_align = ch_n * bits // 8
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, ch_n, sr_ref, byte_rate, block_align, bits)
    hdr += b"data" + struct.pack("<I", len(pcm))
    return hdr + pcm

def tts_synth(text):
    """分句合成→句间随机静音→响度处理。任一环节失败回退整句合成"""
    sents = _split_sentences(text) or [text]
    gmin = max(0, int(CONFIG.get("gap_min_ms", 250)))
    gmax = max(gmin, int(CONFIG.get("gap_max_ms", 700)))
    try:
        wavs, sr_ref = [], None
        for idx, seg in enumerate(sents):
            b = tts_synth_one(seg)
            if sr_ref is None:
                fmt, _ = _parse_wav(b)
                sr_ref = struct.unpack("<HHIIHH", fmt[:16])[2]
            wavs.append(b)
            if idx < len(sents) - 1 and gmax > 0 and sr_ref:
                gap_ms = random.randint(gmin, gmax) if gmax > gmin else gmin
                n = int(sr_ref * gap_ms / 1000.0) * 2   # 16bit≈2字节/样点
                wavs.append(bytes(n))
        merged = wavs[0] if len(wavs) == 1 else _concat_wavs(wavs)
    except Exception as e:
        err("multi-synth fallback: %s" % e)
        merged = tts_synth_one(text)
    return _process_wav(merged)

def _parse_wav(b):
    if b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        raise ValueError("not wav")
    pos, fmt, dat = 12, None, None
    while pos + 8 <= len(b):
        cid = b[pos:pos+4]
        size = int.from_bytes(b[pos+4:pos+8], "little")
        body = b[pos+8:pos+8+size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            dat = (pos + 8, size)
            break
        pos += 8 + size + (size & 1)
    if not fmt or not dat:
        raise ValueError("bad wav chunks")
    return fmt, dat

class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_uint), ("nAvgBytesPerSec", ctypes.c_uint),
                ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]

class _WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", ctypes.c_uint),
                ("dwBytesRecorded", ctypes.c_uint), ("dwUser", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint), ("dwLoops", ctypes.c_uint),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_void_p)]

def _play_winmm(path):
    """winmm 直放，阻塞到播完"""
    with open(path, "rb") as f:
        b = f.read()
    fmt, (doff, dlen) = _parse_wav(b)
    tag, ch, sr, abr, ba, bits = struct.unpack("<HHIIHH", fmt[:16])
    wfx = _WAVEFORMATEX(tag, ch, sr, abr, ba, bits, 0)
    winmm = ctypes.WinDLL("winmm")
    hwo = ctypes.c_void_p()
    rc = winmm.waveOutOpen(ctypes.byref(hwo), 0xFFFFFFFF, ctypes.byref(wfx), None, None, 0)
    if rc != 0:
        raise RuntimeError("waveOutOpen rc=%d" % rc)
    buf = ctypes.create_string_buffer(b[doff:doff+dlen], dlen)
    hdr = _WAVEHDR()
    hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
    hdr.dwBufferLength = dlen
    winmm.waveOutPrepareHeader(hwo, ctypes.byref(hdr), ctypes.sizeof(_WAVEHDR))
    winmm.waveOutWrite(hwo, ctypes.byref(hdr), ctypes.sizeof(_WAVEHDR))
    while not (hdr.dwFlags & 0x1):      # WHDR_DONE
        time.sleep(0.05)
    try:
        winmm.waveOutUnprepareHeader(hwo, ctypes.byref(hdr), ctypes.sizeof(_WAVEHDR))
    except Exception:
        pass
    winmm.waveOutClose(hwo)

def _play_winsound(path):
    import winsound
    winsound.PlaySound(path, winsound.SND_FILENAME)

def play_tts(text):
    """先合成TTS(不占静音窗口) → 静音视频源 → 可配延迟 → 播放 → 恢复。返回(耗时秒, 字节数)"""
    with PLAY_LOCK:
        with LOCK:
            STATE["playing"] = True
        ducked = []
        try:
            t0 = time.time()
            wav = tts_synth(text)
            tmp = os.path.join(os.environ.get("TEMP", "."), "live_brain_tts.wav")
            with open(tmp, "wb") as f:
                f.write(wav)
            ducked = obs_duck_on()          # OBS不在时内部兜底返回[]
            delay_ms = max(0, int(CONFIG.get("duck_delay_ms", 0)))
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
            try:
                _play_winmm(tmp)
            except Exception as e:
                log("winmm失败改用winsound: %s" % e)
                _play_winsound(tmp)
            return time.time() - t0, len(wav)
        finally:
            obs_duck_off(ducked)            # 播完立即恢复
            with LOCK:
                STATE["playing"] = False

# ---------------- OBS websocket (纯标准库最小实现, obs-websocket 5.x) ----------------
class _MiniWS:
    """极简 websocket 客户端: 连接/发文本/收文本, 够用即可"""
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=8)
        self.sock.settimeout(15)
        self._buf = b""

    def handshake(self, host, port):
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET / HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (host, port, key))
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("ws握手失败")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("ws握手被拒: %s" % head[:80])
        self._buf = rest

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("ws closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_text(self):
        while True:
            h = self._recv_exact(2)
            fin_op = h[0]
            ln = h[1] & 0x7F
            masked = h[1] & 0x80
            if ln == 126:
                ln = int.from_bytes(self._recv_exact(2), "big")
            elif ln == 127:
                ln = int.from_bytes(self._recv_exact(8), "big")
            mask = self._recv_exact(4) if masked else None
            data = bytearray(self._recv_exact(ln))
            if mask:
                for i in range(len(data)):
                    data[i] ^= mask[i % 4]
            if (fin_op & 0x0F) == 1:      # 文本帧(不考虑分片)
                return bytes(data).decode("utf-8", errors="replace")
            # ping/其他帧忽略继续收

    def send_text(self, s):
        payload = s.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        header += mask
        enc = bytearray(payload)
        for i in range(n):
            enc[i] ^= mask[i % 4]
        self.sock.sendall(bytes(header) + bytes(enc))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

_OBS = {"ws": None, "lock": threading.Lock(), "rid": 0}

def _obs_connect():
    ws = _MiniWS(OBS_HOST, OBS_PORT)
    ws.handshake(OBS_HOST, OBS_PORT)
    hello = json.loads(ws.recv_text())
    ident = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
    auth = (hello.get("d") or {}).get("authentication")
    if auth:
        import hmac as _hmac
        sec = base64.b64encode(_hmac.new(
            (OBS_PASSWORD + auth["salt"]).encode(), auth["challenge"].encode(),
            hashlib.sha256).digest()).decode()
        ident["d"]["authentication"] = sec
    ws.send_text(json.dumps(ident))
    while True:
        r = json.loads(ws.recv_text())
        if r.get("op") == 2:
            log("obs-ws connected %s:%s" % (OBS_HOST, OBS_PORT))
            return ws
        if r.get("op") == 3:
            raise RuntimeError("obs鉴权失败")

def _obs_call(req_type, data=None):
    """带重连的 OBS 请求; OBS不在时抛异常由调用方兜底"""
    with _OBS["lock"]:
        last = None
        for attempt in range(2):
            try:
                if not _OBS["ws"]:
                    _OBS["ws"] = _obs_connect()
                _OBS["rid"] += 1
                rid = str(_OBS["rid"])
                _OBS["ws"].send_text(json.dumps({
                    "op": 6,
                    "d": {"requestType": req_type, "requestId": rid, "requestData": data or {}}}))
                while True:
                    r = json.loads(_OBS["ws"].recv_text())
                    if r.get("op") == 7 and r["d"].get("requestId") == rid:
                        st = r["d"].get("requestStatus", {})
                        if not st.get("result", st.get("code") == 100):
                            raise RuntimeError("%s: %s" % (req_type, st.get("comment") or st.get("code")))
                        with LOCK:
                            STATE["obs_ok"] = True
                        return r["d"].get("responseData", {})
            except Exception as e:
                last = e
                with LOCK:
                    STATE["obs_ok"] = False
                try:
                    if _OBS["ws"]:
                        _OBS["ws"].close()
                except Exception:
                    pass
                _OBS["ws"] = None
                if attempt:
                    break
                time.sleep(0.5)
        raise last

def _obs_video_inputs():
    """OBS全部视频类输入(不限当前场景)。播报期间lua插件可能切场景,
    全量处理更稳。返回(当前场景名, 视频输入名列表)"""
    scene = _obs_call("GetCurrentProgramScene").get("sceneName")
    inputs = (_obs_call("GetInputList") or {}).get("inputs", [])
    return scene, [i.get("inputName") for i in inputs
                   if i.get("inputKind") in DUCK_KINDS and i.get("inputName")]

def _fade_volume(name, frm, to, fade_ms):
    """线性渐变输入音量(mul)。阻塞直到完成"""
    steps = max(2, int(fade_ms / 50))
    per = fade_ms / 1000.0 / steps
    for i in range(1, steps + 1):
        cur = frm + (to - frm) * i / steps
        try:
            _obs_call("SetInputVolume", {"inputName": name, "inputVolumeMul": round(cur, 4)})
        except Exception as e:
            err("fade %s: %s" % (name, e))
            return
        time.sleep(per)

def obs_duck_on():
    """播报前静音视频源。返回【本次处理】列表 [{name, orig_mul}]。
    duck_fade_ms>0 时先渐弱音量再置静音; =0 立刻静音。"""
    touched = []
    try:
        scene, names = _obs_video_inputs()
        fade = max(0, int(CONFIG.get("duck_fade_ms", 0)))
        for name in names:
            try:
                muted = bool((_obs_call("GetInputMute", {"inputName": name}) or {}).get("inputMuted"))
                if muted:
                    continue                      # 本来就静音的不管
                vol = (_obs_call("GetInputVolume", {"inputName": name}) or {}).get("inputVolumeMul", 1.0)
                touched.append({"name": name, "orig_mul": float(vol)})
                if fade > 0:
                    _fade_volume(name, float(vol), 0.0, fade)
                _obs_call("SetInputMute", {"inputName": name, "inputMuted": True})
                _obs_call("SetInputVolume", {"inputName": name, "inputVolumeMul": 0.0})
            except Exception as e:
                err("obs mute %s: %s" % (name, e))
        with LOCK:
            STATE["duck_active"] = True
            STATE["duck_muted"] = len(touched)
        log("obs duck ON [%s]: %d/%d muted fade=%dms" % (scene, len(touched), len(names), fade))
    except Exception as e:
        err("obs duck on: %s" % e)
    return touched

def obs_duck_off(touched):
    """播报后恢复。unduck_fade_ms>0 时从0淡入回原音量; =0 立刻恢复原音量。"""
    if not touched:
        return
    fade = max(0, int(CONFIG.get("unduck_fade_ms", 0)))
    for t in touched:
        name, orig = t["name"], t["orig_mul"]
        try:
            _obs_call("SetInputMute", {"inputName": name, "inputMuted": False})
            if fade > 0:
                _obs_call("SetInputVolume", {"inputName": name, "inputVolumeMul": 0.01})
                _fade_volume(name, 0.01, max(orig, 0.01), fade)
            _obs_call("SetInputVolume", {"inputName": name, "inputVolumeMul": round(orig, 4)})
        except Exception as e:
            err("obs unmute %s: %s" % (name, e))
    with LOCK:
        STATE["duck_active"] = False
        STATE["duck_muted"] = 0
    log("obs duck OFF: %d restored fade=%dms" % (len(touched), fade))

# ---------------- 场景切换引擎(替代lua插件) ----------------
def _cn_number_runs(text):
    """提取中文数字串并求值: '十六'->16, '十二'->12。返回值列表(>1)"""
    vals = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in CN_DIGITS or ch == "十":
            j = i
            while j < n and (text[j] in CN_DIGITS or text[j] == "十"):
                j += 1
            run = text[i:j]
            # 求值: 支持十/十X/X十/X十Y
            v = None
            if run == "十":
                v = 10
            elif "十" in run:
                parts = run.split("十")
                left = CN_DIGITS.get(parts[0], 1) if parts[0] else 1
                right = CN_DIGITS.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
                v = left * 10 + right
            else:
                v = CN_DIGITS.get(run)
            if v and v > 1:
                vals.append(v)
            i = j
        else:
            i += 1
    return vals

def _snap_level(v, levels):
    """就近归档: 落到不大于v的最大档位; 低于最小档取最小档"""
    for lv in reversed(levels):
        if v >= lv:
            return lv
    return levels[0]

def scene_extract_cmd(content):
    """意图识别(与lua danmaku_extract_cmd 等价):
    触发词+尺寸 -> 'N'/'N泡'; 无触发词或无尺寸 -> None(静默)"""
    trigger_words = ("看", "卡", "试", "展示")
    bubble_words = ("泡泡", "泡子", "珠珠")
    if not content:
        return None
    if not any(w in content for w in trigger_words):
        return None
    has_bubble = any(w in content for w in bubble_words)
    levels = BUBBLE_LEVELS if has_bubble else PLAIN_LEVELS
    # 阿拉伯数字优先
    m = re.search(r"\d+", content)
    if m:
        return str(_snap_level(int(m.group()), levels)) + ("泡" if has_bubble else "")
    # 中文数字: 优先恰好等于档位的串
    for v in _cn_number_runs(content):
        if v in levels:
            return str(v) + ("泡" if has_bubble else "")
    for v in _cn_number_runs(content):
        return str(_snap_level(v, levels)) + ("泡" if has_bubble else "")
    return None

def scene_on_danmu(content):
    """每条弹幕都进这里: 命中指令则安排延迟切换。返回tag描述"""
    cmd = scene_extract_cmd(content)
    if cmd is None:
        with LOCK:
            STATE["last_scene_detect"] = "- " + content[:40]
        return "静默"
    now = time.time()
    hold_until = SCENE_STATE["hold_until"]
    if CONFIG.get("scene_hold_s", 180) > 0 and hold_until and now < hold_until:
        with LOCK:
            STATE["last_scene_detect"] = "⏳ 锁定中剩余%ds，忽略 %s" % (int(hold_until - now), cmd)
        return "锁定·忽略 → " + cmd
    delay = max(0, int(CONFIG.get("scene_switch_delay_s", 3)))
    SCENE_STATE["pending"] = {"at": now + delay, "cmd": cmd}
    with LOCK:
        STATE["last_scene_detect"] = "▶ 检测 " + content[:30] + " -> " + cmd
    if delay > 0:
        log("scene: 检测到 [%s] -> %s，%d秒后切换" % (content[:40], cmd, delay))
    return "→ " + cmd

def scene_tick():
    """主循环调用: 执行到期的延迟切换 + 锁定期满回默认"""
    now = time.time()
    pend = SCENE_STATE["pending"]
    if pend and now >= pend["at"]:
        SCENE_STATE["pending"] = None
        cmd = pend["cmd"]
        entry = SCENE_BY_CMD.get(cmd)
        if not entry:
            with LOCK:
                STATE["last_scene_detect"] = "✗ %s -> 无对应场景" % cmd
            return
        try:
            _obs_call("SetCurrentProgramScene", {"sceneName": entry["name"]})
            SCENE_STATE["switch_count"] += 1
            hold = int(CONFIG.get("scene_hold_s", 180))
            SCENE_STATE["hold_until"] = time.time() + hold if hold > 0 else None
            with LOCK:
                STATE["current_scene"] = entry["name"]
                STATE["last_scene_detect"] = "✓ %s -> 场景[%s]" % (cmd, entry["name"])
            log("scene switched: cmd=%s scene=[%s] hold=%ds (累计%d次)"
                % (cmd, entry["name"], hold, SCENE_STATE["switch_count"]))
        except Exception as e:
            err("scene switch %s: %s" % (entry["name"], e))
            with LOCK:
                STATE["last_scene_detect"] = "✗ 切换失败: %s" % e
    # 锁定期满回默认
    hu = SCENE_STATE["hold_until"]
    if hu and now >= hu:
        SCENE_STATE["hold_until"] = None
        try:
            _obs_call("SetCurrentProgramScene", {"sceneName": "默认"})
            with LOCK:
                STATE["current_scene"] = "默认"
            log("scene: 锁定期结束, 回默认场景")
        except Exception as e:
            err("scene back-to-default: %s" % e)

def scene_manual(cmd):
    """手动切换(WebUI/面板按钮用)。返回是否成功入队"""
    cmd = str(cmd).strip()
    if cmd not in SCENE_BY_CMD:
        return False
    delay = max(0, int(CONFIG.get("scene_switch_delay_s", 3)))
    SCENE_STATE["pending"] = {"at": time.time() + min(delay, 1), "cmd": cmd}
    return True


# ---------------- 工作线程 ----------------
def worker():
    log("worker started")
    while True:
        try:
            item = Q.get(timeout=2)
        except queue.Empty:
            continue
        with LOCK:
            STATE["queue"] = Q.qsize()
        nick, text = item["nick"], item["text"]
        if not STATE["enabled"]:
            continue
        t0 = time.time()
        try:
            knowledge = rag_lookup(text) if CONFIG.get("rag_enabled", True) else ""
            t1 = time.time(); t_rag = t1 - t0
            reply = llm_reply(nick, text, knowledge)
            t2 = time.time(); t_llm = t2 - t1
            if not reply:
                raise RuntimeError("llm返回空回复")
            try:
                dur_tts, nbytes = play_tts(reply)
            except Exception as te:      # VSA可能冷启动中, 等15s重试一次
                err("tts首次失败(15s后重试): %s" % te)
                time.sleep(15)
                dur_tts, nbytes = play_tts(reply)
            dt = time.time() - t0
            with LOCK:
                STATE["replied"] += 1
                STATE["last_reply"] = {"time": datetime.now().strftime("%H:%M:%S"),
                                       "nick": nick, "danmu": text, "reply": reply,
                                       "rag": bool(knowledge)}
            ITEMS.appendleft({"time": datetime.now().strftime("%H:%M:%S"), "nick": nick,
                              "danmu": text, "reply": reply, "ms": int(dt * 1000),
                              "rag": bool(knowledge), "ok": True})
            log("OK [%s]%s => %s (rag %.1fs llm %.1fs tts %.1fs %dB)"
                % (nick, text, reply, t_rag, t_llm, dur_tts, nbytes))
        except Exception as e:
            with LOCK:
                STATE["failed"] += 1
            ITEMS.appendleft({"time": datetime.now().strftime("%H:%M:%S"), "nick": nick,
                              "danmu": text, "reply": "", "ms": int((time.time()-t0)*1000),
                              "rag": False, "ok": False})
            err("worker[%s]: %s" % (text, e))
            traceback.print_exc()
            # 429等高峰期故障: 弹幕回队重试一次(带标记防无限循环)
            if not item.get("retried") and Q.qsize() < 8:
                time.sleep(3)                       # 等限流窗口过去
                try:
                    Q.put_nowait({"nick": nick, "text": text, "synthetic": item.get("synthetic", False), "retried": True})
                    log("danmu requeued once: [%s]%s" % (nick, text))
                except queue.Full:
                    pass

def monitor_loop():
    log("monitor watching %s" % LOG_DIR)
    while True:
        try:
            scan_once()                                   # FlyAiLive(抖音等)最新记录
            tbp = os.path.join(LOG_DIR, "用户发言记录_tb.txt")
            if os.path.exists(tbp):
                scan_once(tbp)                            # 淘宝弹幕桥接文件
        except Exception as e:
            err("scan: %s" % e)
        try:
            if CONFIG.get("scene_enabled", True):
                scene_tick()
                with LOCK:
                    STATE["scene_switches"] = SCENE_STATE["switch_count"]
        except Exception as e:
            err("scene tick: %s" % e)
        time.sleep(POLL_INTERVAL)

# ---------------- HTTP 控制面 ----------------
PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>直播大脑 · live-brain</title><style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,"Microsoft YaHei";background:#0f172a;color:#e2e8f0;padding:24px}
.wrap{max-width:900px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}.sub{color:#64748b;font-size:13px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.card{background:#1e293b;border-radius:12px;padding:12px}.k{color:#64748b;font-size:12px}.v{font-size:16px;font-weight:600;margin-top:4px}
.badge{display:inline-block;padding:4px 14px;border-radius:99px;font-weight:700;font-size:14px}
.on{background:#064e3b;color:#34d399}.off{background:#450a0a;color:#f87171}
table{width:100%;border-collapse:collapse;background:#1e293b;border-radius:12px;overflow:hidden;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #334155}th{background:#0b1220;color:#94a3b8}
.ok{color:#34d399}.fail{color:#f87171}.mut{color:#64748b}
.row{display:flex;gap:10px;margin:14px 0;flex-wrap:wrap;align-items:center}
button,input{border:none;border-radius:8px;padding:9px 18px;font-size:14px;cursor:pointer;background:#38bdf8;color:#04283d;font-weight:600}
input{background:#0f172a;color:#e2e8f0;border:1px solid #334155;cursor:text;width:260px}
button.ghost{background:#334155;color:#cbd5e1}
#tip{font-size:13px;color:#94a3b8;min-height:18px;margin-top:6px}
.svc-entry{display:flex;gap:12px;margin:0 0 14px}
.dot{font-size:16px;color:#64748b}
.dot.on{color:#34d399}
.dot.off{color:#f87171}
</style></head><body><div class="wrap">
<h1>🧠 直播大脑 <span id="badge" class="badge on">运行中</span></h1>
<div class="sub">弹幕 → RAG+LLM → TTS → 自动播放 · 端口 __PORT__</div>
<div class="svc-entry">
<a href="http://127.0.0.1:23461/" target="_blank" style="text-decoration:none"><button class="ghost">📦 弹幕桥控制台 <span id="dot_tb" class="dot">·</span></button></a>
<a href="http://127.0.0.1:7862/" target="_blank" style="text-decoration:none"><button class="ghost">🛡️ 实时驱虫控制台 <span id="dot_dd" class="dot">·</span></button></a>
</div>
<div class="grid">
<div class="card"><div class="k">监控文件</div><div class="v" id="watch">-</div></div>
<div class="card"><div class="k">待处理队列</div><div class="v" id="queue">0</div></div>
<div class="card"><div class="k">累计弹幕</div><div class="v" id="seen">0</div></div>
<div class="card"><div class="k">已回复播放</div><div class="v" id="replied">0</div></div>
<div class="card"><div class="k">失败</div><div class="v" id="failed">0</div></div>
<div class="card"><div class="k">冷却跳过</div><div class="v" id="cool">0</div></div>
<div class="card"><div class="k">重复跳过</div><div class="v" id="dup">0</div></div>
<div class="card"><div class="k">OBS联动</div><div class="v" id="obs">-</div></div>
<div class="card"><div class="k">Ducking</div><div class="v" id="duck">正常</div></div>
<div class="card"><div class="k">当前模型</div><div class="v" id="llm" style="font-size:13px">-</div></div>
<div class="card"><div class="k">模型切换次数</div><div class="v" id="fo">0</div></div>
<div class="card"><div class="k">当前场景</div><div class="v" id="scn">默认</div></div>
<div class="card"><div class="k">切景检测</div><div class="v" id="scd" style="font-size:12px">-</div></div>
<div class="card"><div class="k">最近弹幕</div><div class="v" id="ld" style="font-size:13px">-</div></div>
</div>
<div class="row">
<button onclick="act('/pause')" class="ghost">⏸ 暂停回复</button>
<button onclick="act('/resume')">▶️ 恢复回复</button>
<a href="/config" style="text-decoration:none"><button class="ghost">⚙️ 播报设置</button></a>
<input id="t" placeholder="模拟弹幕内容，如：黄水晶有什么功效">
<button onclick="sendTest()">📨 模拟弹幕</button>
<button onclick="sendSay()" class="ghost">🔊 直接播报输入框文字</button>
</div>
<div id="tip"></div>
<table><thead><tr><th>时间</th><th>观众</th><th>弹幕</th><th>回复(已播)</th><th>耗时</th></tr></thead>
<tbody id="items"><tr><td colspan="5" class="mut">暂无记录</td></tr></tbody></table>
<script>
async function refresh(){
 try{
  const s=await (await fetch('/api/status')).json();
  const b=document.getElementById('badge');
  b.textContent=s.enabled?'运行中':'已暂停';
  b.className='badge '+(s.enabled?'on':'off');
  watch.textContent=s.watched||'-'; queue.textContent=s.queue;
  seen.textContent=s.seen; replied.textContent=s.replied; failed.textContent=s.failed;
  cool.textContent=s.skip_cooldown; dup.textContent=s.skip_dup;
  const o=document.getElementById('obs');
  o.textContent=s.obs_ok?'已连接':'未连接';
  o.style.color=s.obs_ok?'#34d399':'#f87171';
  const dk=document.getElementById('duck');
  if(s.duck_active){dk.textContent='🔇 视频源静音中('+s.duck_muted+')';dk.style.color='#fbbf24';}
  else{dk.textContent='正常播放';dk.style.color='#e2e8f0';}
  llm.textContent=s.llm_model||'-';
  fo.textContent=s.failovers||0;
  scn.textContent=s.current_scene||'默认';
  scd.textContent=(s.last_scene_detect||'-').slice(0,26);
  ld.textContent=s.last_danmu?(s.last_danmu.nick+'：'+s.last_danmu.text):'-';
  const tb=document.getElementById('items');
  tb.innerHTML=s.items.map(i=>'<tr><td>'+i.time+'</td><td>'+i.nick+'</td><td>'+i.danmu+
    '</td><td class="'+(i.ok?'ok':'fail')+'">'+(i.reply||'(失败)')+'</td><td>'+(i.ms/1000).toFixed(1)+'s'+
    (i.rag?' 📚':'')+'</td></tr>').join('')||'<tr><td colspan=5 class=mut>暂无</td></tr>';
 }catch(e){}
 try{
  const h=await (await fetch('/api/svc_health')).json();
  const set=(id,ok)=>{const d=document.getElementById(id);d.className='dot '+(ok?'on':'off');d.textContent=ok?'●':'●';};
  set('dot_tb',h.tb); set('dot_dd',h.dedup);
 }catch(e){}
}
async function act(u){const r=await fetch(u);document.getElementById('tip').textContent=u+' => '+(await r.text());refresh()}
function sendTest(){const t=t.value.trim();if(!t)return tip.textContent='先填内容';
 fetch('/test?text='+encodeURIComponent(t)).then(r=>r.text()).then(x=>{tip.textContent=x;t.value='';refresh()})}
function sendSay(){const t=t.value.trim();if(!t)return tip.textContent='先填内容';
 tip.textContent='⏳ 合成播放中…';
 fetch('/say?text='+encodeURIComponent(t)).then(r=>r.text()).then(x=>{tip.textContent=x;t.value=''})}
refresh();setInterval(refresh,4000);
</script></div></body></html>"""

CONFIG_PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>直播大脑 · 播报设置</title><style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,"Microsoft YaHei";background:#0f172a;color:#e2e8f0;padding:24px}
.wrap{max-width:680px;margin:0 auto}h1{font-size:21px;margin:0 0 4px}.sub{color:#64748b;font-size:13px;margin-bottom:20px}
.card{background:#1e293b;border-radius:14px;padding:20px;margin-bottom:14px}
.card h2{margin:0 0 14px;font-size:15px;color:#38bdf8}
.row{display:flex;justify-content:space-between;align-items:center;margin:12px 0;gap:14px}
.lab{font-size:14px}.hint{font-size:12px;color:#64748b;margin-top:3px}
input[type=range]{flex:1;accent-color:#38bdf8}
input[type=number],select{background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:7px;width:110px;font-size:14px}
.val{width:74px;text-align:right;font-weight:600;color:#38bdf8;font-size:15px}
.switch{position:relative;width:46px;height:26px}.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;inset:0;background:#334155;border-radius:99px;transition:.2s}
.slider:before{content:"";position:absolute;height:20px;width:20px;left:3px;top:3px;background:#94a3b8;border-radius:50%;transition:.2s}
input:checked+.slider{background:#059669}input:checked+.slider:before{transform:translateX(20px);background:#fff}
button{border:none;border-radius:9px;padding:10px 22px;font-size:14px;font-weight:600;cursor:pointer;background:#38bdf8;color:#04283d}
button.ghost{background:#334155;color:#cbd5e1}
.preset{display:flex;gap:10px;margin-bottom:4px}
#tip{font-size:13px;color:#34d399;min-height:18px;margin-top:8px}
.back{font-size:13px;color:#64748b;text-decoration:none}
</style></head><body><div class="wrap">
<h1>🎙️ 播报设置 <a class="back" href="/">← 返回状态页</a></h1>
<div class="sub">改动保存后立即生效，重启不丢失</div>

<div class="card"><h2>🔇 视频静音切换（Ducking）</h2>
<div class="row"><div><div class="lab">静音后延迟播放</div><div class="hint">视频源静音完成后，等待再开播报。给混音留缓冲</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:330px">
<input type="range" id="duck_delay_ms" min="0" max="3000" step="100" oninput="v(this,'d1')"><span class="val" id="d1"></span></div></div>
<div class="row"><div><div class="lab">视频声音淡出时长</div><div class="hint">0 = 立刻硬切静音；推荐 300~800ms 自然过渡</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:330px">
<input type="range" id="duck_fade_ms" min="0" max="3000" step="100" oninput="v(this,'d2')"><span class="val" id="d2"></span></div></div>
<div class="row"><div><div class="lab">播完后淡入恢复时长</div><div class="hint">0 = 立刻满音量恢复；推荐 500~1500ms 渐入</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:330px">
<input type="range" id="unduck_fade_ms" min="0" max="3000" step="100" oninput="v(this,'d3')"><span class="val" id="d3"></span></div></div>
<div class="preset">
<button class="ghost" onclick='preset(0,0,0)'>⚡ 极速硬切</button>
<button class="ghost" onclick='preset(300,500,1000)'>🌊 平滑过渡</button>
<button class="ghost" onclick='preset(1000,1500,2000)'>🎬 综艺感</button>
</div></div>

<div class="card"><h2>🧠 大模型（主备自动切换）</h2>
<div class="row"><div><div class="lab">主模型 Base URL</div><div class="hint">OpenAI 兼容 /v1 地址；连续失败2次自动切备用</div></div>
<input type="text" id="m_base" style="width:280px" placeholder="http://192.168.5.100:8002/v1"></div>
<div class="row"><div><div class="lab">主模型名称</div><div class="hint">如 deepseek-v4-flash、gpt-4o-mini 等</div></div>
<input type="text" id="m_model" style="width:200px"></div>
<div class="row"><div><div class="lab">主模型 API Key</div><div class="hint">留空=不修改已保存的key；本地服务填 EMPTY</div></div>
<input type="password" id="m_key" style="width:200px" placeholder="••••••"></div>

<div style="border-top:1px solid #334155;margin:14px 0"></div>

<div class="row"><div><div class="lab">备用模型 Base URL</div><div class="hint">留空=不启用备用；免费API常挂建议配上</div></div>
<input type="text" id="b_base" style="width:280px" placeholder="https://api.xxx.com/v1"></div>
<div class="row"><div><div class="lab">备用模型名称</div><div class="hint">主模型挂掉后自动用它顶上</div></div>
<input type="text" id="b_model" style="width:200px"></div>
<div class="row"><div><div class="lab">备用 API Key</div><div class="hint">留空=不修改已保存的key</div></div>
<input type="password" id="b_key" style="width:200px" placeholder="••••••"></div>
<div class="row"><span></span><button class="ghost" onclick="testLlm()">🧪 测试当前主模型连通</button></div>
</div>

<div class="card"><h2>💬 回复风格与提示词</h2>
<div class="row" style="flex-direction:column;align-items:stretch;gap:6px">
<div class="lab">系统提示词（人设）</div>
<textarea id="system_prompt" rows="4" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:10px;font-size:13px;resize:vertical;font-family:inherit"></textarea>
<div class="hint">告诉 AI 它是谁、卖什么、什么语气。输出长度/禁表情等规则由系统自动附加，无需写在这里</div></div>
<div class="row"><div><div class="lab">回复带观众昵称</div><div class="hint">如「小美你好呀～」更有互动感；关掉则泛称</div></div>
<label class="switch"><input type="checkbox" id="reply_include_nick"><span class="slider"></span></label></div>
<div class="row"><div><div class="lab">回复先复述观众发言</div><div class="hint">如「你问黄水晶呀——」确认式互动；默认关</div></div>
<label class="switch"><input type="checkbox" id="reply_echo_danmu"><span class="slider"></span></label></div>
<div class="row"><div><div class="lab">RAG 知识库增强</div><div class="hint">开启后回复优先引用知识库资料</div></div>
<label class="switch"><input type="checkbox" id="rag_enabled"><span class="slider"></span></label></div>
</div>

<div class="card"><h2>🎬 弹幕切场景（替代OBS lua插件）</h2>
<div class="row"><div><div class="lab">启用弹幕自动切场景</div><div class="hint">关闭后可完全用下方按钮手动控制</div></div>
<label class="switch"><input type="checkbox" id="scene_enabled"><span class="slider"></span></label></div>
<div class="row"><div><div class="lab">切换延迟</div><div class="hint">检测到指令后等几秒再切（窗口内新指令覆盖旧指令）</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:300px">
<input type="range" id="scene_switch_delay_s" min="0" max="15" step="1" oninput="v(this,'d5')"><span class="val" id="d5"></span></div></div>
<div class="row"><div><div class="lab">场景保持时长</div><div class="hint">切换后保持秒数，到期自动回「默认」；期间锁定忽略新指令。0=不回默认</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:300px">
<input type="range" id="scene_hold_s" min="0" max="600" step="30" oninput="v(this,'d6')"><span class="val" id="d6"></span></div></div>
<div class="hint" style="margin-top:8px">触发规则：含「看/卡/试/展示」+ 尺寸(6/8/10/12/14/16，支持中文数字如十六) → 白水；再加「泡泡/泡子/珠珠」→ 泡泡珠(10-16)</div>
<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:12px">
<button class="ghost" style="padding:6px 12px;font-size:12px" onclick="sc('默认')">默认</button>
<button class="ghost" style="padding:6px 12px;font-size:12px" onclick="sc('6')">白水6m</button>
<button class="ghost" style="padding:6px 12px;font-size:12px" onclick="sc('8')">白水8m</button>
<button class="ghost" style="padding:6px 12px;font-size:12px" onclick="sc('10')">白水10m</button>
<button class="ghost" style="padding:6px 12px;font-size:12px" onclick="sc('12')">白水12m</button>
<button class="ghost" style="padding:6px 12px;font-size:12px" onclick="sc('14')">白水14m</button>
<button class="ghost" style="padding:6px 12px;font-size:12px" onclick="sc('16')">白水16m</button>
<button class="ghost" style="padding:6px 12px;font-size:12px" onclick="sc('10泡')">泡泡珠10m</button>
<button class="ghost" style="padding:6px 12px;font-size:12px" onclick="sc('12泡')">泡泡珠12m</button>
<button class="ghost" style="padding:6px 12px;font-size:12px" onclick="sc('14泡')">泡泡珠14m</button>
<button class="ghost" style="padding:6px 12px;font-size:12px" onclick="sc('16泡')">泡泡珠16m</button>
</div></div>

<div class="card"><h2>🗣️ 语音播报</h2>
<div class="row"><div><div class="lab">TTS 音色</div><div class="hint">VSA Bert-VITS2 模型音色</div></div>
<select id="voice_id"><option value="0">sdd</option><option value="1">zxy</option></select></div>
<div class="row"><div><div class="lab">情绪模式</div><div class="hint">实验=透传VSA emotion/CLAP通道(效果依模型)</div></div>
<select id="tts_emotion">
<option value="normal">😐 平常</option>
<option value="happy">😄 高兴(实验)</option>
<option value="angry">😠 愤怒(实验)</option>
<option value="sad">😢 悲伤(实验)</option>
<option value="experiment">🧪 全参数实验通道</option>
</select></div>
<div class="row"><div><div class="lab">音量增益</div><div class="hint">% 播放端放大/缩小, >100放大</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:330px">
<input type="range" id="tts_volume" min="50" max="200" step="5" oninput="v2(this)"><span class="val" id="v_vol"></span></div></div>
<div class="row"><div><div class="lab">响度归一化</div><div class="hint">峰值压到-1dB防爆音(推荐开)</div></div>
<input type="checkbox" id="tts_normalize"></div>
<div class="row"><div><div class="lab">音质EQ</div><div class="hint">切低频浑浊+提2-4k清晰度, 对齐视频原声</div></div>
<input type="checkbox" id="tts_eq"></div>
<div class="row"><div><div class="lab">过滤表情弹幕</div><div class="hint">emoji/淘宝[-xx]表情码/礼物计数(减8·点23)不进LLM</div></div>
<input type="checkbox" id="filter_emoji"></div>
<div class="row"><div><div class="lab">回复模式</div><div class="hint">即时=每条弹幕单独回; 智能聚合=同人窗口内多条合并成一条回复</div></div>
<select id="reply_mode" style="background:#1b2437;color:#eee;border:1px solid #334;border-radius:6px;padding:6px 10px">
<option value="instant">⚡ 即时（逐条回复）</option>
<option value="smart">🧠 智能聚合（同人合并回复）</option>
</select></div>
<div class="row"><div><div class="lab">聚合窗口</div><div class="hint">秒。智能模式下首条弹幕起等N秒收集同人后续弹幕(3~15)</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:330px">
<input type="range" id="agg_window_s" min="3" max="15" step="1" oninput="document.getElementById('v_aggw').textContent=this.value+' s'"><span class="val" id="v_aggw"></span></div></div>
<div class="row"><div><div class="lab">语速</div><div class="hint">% 100=原速, 越大越快</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:330px">
<input type="range" id="tts_speed" min="60" max="160" step="5" oninput="v2(this)"><span class="val" id="v_spd"></span></div></div>
<div class="row"><div><div class="lab">语调抑扬 sdp_ratio</div><div class="hint">0平板~100起伏大</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:330px">
<input type="range" id="tts_sdp" min="0" max="100" step="5" oninput="v2(this)"><span class="val" id="v_sdp"></span></div></div>
<div class="row"><div><div class="lab">情感起伏 noise</div><div class="hint">20平稳~80情感波动大</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:330px">
<input type="range" id="tts_noise" min="20" max="80" step="1" oninput="v2(this)"><span class="val" id="v_noi"></span></div></div>
<div class="row"><div><div class="lab">句间停顿 最小~最大</div><div class="hint">每句之间随机取区间内时长</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:420px">
<input type="range" id="gap_min_ms" min="0" max="1500" step="50" oninput="vgap()"><span class="val" id="v_gmin"></span>
<span style="color:#667">~</span>
<input type="range" id="gap_max_ms" min="0" max="2000" step="50" oninput="vgap()"><span class="val" id="v_gmax"></span></div></div>
<div class="row"><div><div class="lab">同一观众回复冷却</div><div class="hint">秒。防止刷屏连环回复</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:330px">
<input type="range" id="user_cooldown" min="0" max="120" step="5" oninput="v(this,'d4')"><span class="val" id="d4"></span></div></div>
</div>

<div class="row" style="margin-top:18px">
<button onclick="save()">💾 保存并生效</button>
<button class="ghost" onclick="testSay()">🔊 用当前配置试听一句</button>
</div>
<div id="tip"></div>
</div>
<script>
const F=['duck_delay_ms','duck_fade_ms','unduck_fade_ms','user_cooldown'];
function v2(el){const m={tts_volume:'v_vol',tts_speed:'v_spd',tts_sdp:'v_sdp',tts_noise:'v_noi'};
 document.getElementById(m[el.id]).textContent=el.value+(el.id==='tts_volume'||el.id==='tts_speed'?' %':'');}
function vgap(){const a=document.getElementById('gap_min_ms'),b=document.getElementById('gap_max_ms');
 if(+b.value<+a.value)b.value=a.value;
 document.getElementById('v_gmin').textContent=a.value+' ms';
 document.getElementById('v_gmax').textContent=b.value+' ms';}
function v(el,id){const u=el.id.endsWith('_ms')?' ms':' s';let t=el.value+u;if(el.id==='scene_hold_s'&&+el.value>=60)t+=' (~'+(+el.value/60).toFixed(1)+'分)';document.getElementById(id).textContent=t}
async function load(){
 const c=await (await fetch('/api/config')).json();
 F.forEach(k=>{const e=document.getElementById(k);e.value=c[k];v(e,'d'+(F.indexOf(k)+1))});
 document.getElementById('voice_id').value=c.voice_id;
 document.getElementById('tts_volume').value=c.tts_volume??130; v2(document.getElementById('tts_volume'));
 document.getElementById('tts_normalize').checked=c.tts_normalize!==false;
 document.getElementById('tts_eq').checked=c.tts_eq!==false;
 document.getElementById('filter_emoji').checked=c.filter_emoji!==false;
 document.getElementById('reply_mode').value=c.reply_mode||'instant';
 const aw=document.getElementById('agg_window_s'); aw.value=c.agg_window_s??6;
 document.getElementById('v_aggw').textContent=aw.value+' s';
 document.getElementById('tts_speed').value=c.tts_speed??100; v2(document.getElementById('tts_speed'));
 document.getElementById('tts_sdp').value=c.tts_sdp??30; v2(document.getElementById('tts_sdp'));
 document.getElementById('tts_noise').value=c.tts_noise??33; v2(document.getElementById('tts_noise'));
 document.getElementById('tts_emotion').value=c.tts_emotion||'normal';
 document.getElementById('gap_min_ms').value=c.gap_min_ms??250;
 document.getElementById('gap_max_ms').value=c.gap_max_ms??700; vgap();
 document.getElementById('rag_enabled').checked=c.rag_enabled;
 document.getElementById('reply_include_nick').checked=c.reply_include_nick;
 document.getElementById('reply_echo_danmu').checked=c.reply_echo_danmu;
 document.getElementById('system_prompt').value=c.system_prompt||'';
 document.getElementById('scene_enabled').checked=c.scene_enabled;
 const e5=document.getElementById('scene_switch_delay_s');e5.value=c.scene_switch_delay_s;v(e5,'d5');
 const e6=document.getElementById('scene_hold_s');e6.value=c.scene_hold_s;v(e6,'d6');
 m_base.value=c.llm_main.base_url; m_model.value=c.llm_main.model;
 b_base.value=c.llm_backup.base_url||''; b_model.value=c.llm_backup.model||'';
}
function collect(){
 const o={};F.forEach(k=>o[k]=+document.getElementById(k).value);
 o.voice_id=+document.getElementById('voice_id').value;
 o.tts_volume=+document.getElementById('tts_volume').value;
 o.tts_normalize=document.getElementById('tts_normalize').checked;
 o.tts_eq=document.getElementById('tts_eq').checked;
 o.filter_emoji=document.getElementById('filter_emoji').checked;
 o.reply_mode=document.getElementById('reply_mode').value;
 o.agg_window_s=+document.getElementById('agg_window_s').value;
 o.tts_speed=+document.getElementById('tts_speed').value;
 o.tts_sdp=+document.getElementById('tts_sdp').value;
 o.tts_noise=+document.getElementById('tts_noise').value;
 o.tts_emotion=document.getElementById('tts_emotion').value;
 o.gap_min_ms=+document.getElementById('gap_min_ms').value;
 o.gap_max_ms=+document.getElementById('gap_max_ms').value;
 o.rag_enabled=document.getElementById('rag_enabled').checked;
 o.reply_include_nick=document.getElementById('reply_include_nick').checked;
 o.reply_echo_danmu=document.getElementById('reply_echo_danmu').checked;
 o.system_prompt=document.getElementById('system_prompt').value;
 o.scene_enabled=document.getElementById('scene_enabled').checked;
 o.scene_switch_delay_s=+document.getElementById('scene_switch_delay_s').value;
 o.scene_hold_s=+document.getElementById('scene_hold_s').value;
 o.llm_main={base_url:m_base.value,model:m_model.value,api_key:m_key.value};
 o.llm_backup={base_url:b_base.value,model:b_model.value,api_key:b_key.value};
 return o;
}
async function save(){
 const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collect())});
 if(r.ok){const c=await r.json();b_base.value=c.llm_backup.base_url||'';document.getElementById('tip').textContent='✅ 已保存并即时生效';}
 else{document.getElementById('tip').textContent='❌ 保存失败';}
}
async function testLlm(){
 const btn=[...document.querySelectorAll('button')].find(b=>b.textContent.includes('测试当前主模型'));
 const tip=document.getElementById('tip');
 const old=btn?btn.textContent:'';
 if(btn){btn.disabled=true;btn.textContent='⏳ 测试中…(最多15s)';}
 tip.textContent='⏳ 正在测试主模型…';
 const ctl=new AbortController(); const tid=setTimeout(()=>ctl.abort(),15000);
 try{
  const r=await fetch('/api/llmtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collect()),signal:ctl.signal});
  const j=await r.json();
  if(j.ok){
   tip.textContent='✅ 主模型连通: '+j.reply+(j.note?(' '+j.note):'')+'（应答模型: '+j.actual_model+'）';
  }else{tip.textContent='❌ 失败: '+j.error;}
 }catch(e){tip.textContent=e.name==='AbortError'?'❌ 超时(15s): 主模型无响应, 高峰限流或地址不可达':'❌ '+e;}
 clearTimeout(tid);
 if(btn){btn.disabled=false;btn.textContent=old;}
}
function preset(a,b,c){[['duck_delay_ms',a],['duck_fade_ms',b],['unduck_fade_ms',c]].forEach(([k,v_])=>{const e=document.getElementById(k);e.value=v_;v(e,'d'+(F.indexOf(k)+1))})}
function sc(cmd){
 document.getElementById('tip').textContent='⏳ 切换到 '+cmd+' …';
 fetch('/scene?cmd='+encodeURIComponent(cmd)).then(r=>r.text()).then(t=>{
  document.getElementById('tip').textContent=t;setTimeout(load,1500);
 }).catch(e=>document.getElementById('tip').textContent='❌ '+e);
}
function testSay(){document.getElementById('tip').textContent='⏳ 试听播放中…';
 fetch('/say?text='+encodeURIComponent('这是当前配置下的试听效果，视频声音正在被压低')).then(()=>setTimeout(()=>{document.getElementById('tip').textContent='✅ 试听完成';},4000))}
load();
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                self._send(200, PAGE.replace("__PORT__", str(BRAIN_PORT)), "text/html; charset=utf-8")
            elif u.path in ("/config", "/settings"):
                self._send(200, CONFIG_PAGE, "text/html; charset=utf-8")
            elif u.path == "/api/config":
                with LOCK:
                    snap = json.loads(json.dumps(CONFIG))   # 深拷贝
                for _s in ("llm_main", "llm_backup"):       # key不回传前端
                    snap[_s]["api_key"] = ""
                self._send(200, json.dumps(snap, ensure_ascii=False))
            elif u.path == "/health":
                self._send(200, "ok")
            elif u.path == "/api/svc_health":
                # 旁路服务在线探测(弹幕桥23461/实时驱虫7862), 供WebUI入口状态点
                def _probe(port):
                    try:
                        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                        c.request("GET", "/")
                        ok = c.getresponse().status < 500
                        c.close()
                        return ok
                    except Exception:
                        return False
                self._send(200, json.dumps({"tb": _probe(23461), "dedup": _probe(7862)}))
            elif u.path == "/api/status":
                with LOCK:
                    snap = {k: (list(v) if isinstance(v, deque) else v) for k, v in STATE.items()}
                snap["items"] = list(ITEMS)
                snap["now"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._send(200, json.dumps(snap, ensure_ascii=False))
            elif u.path == "/test":
                text = (qs.get("text") or [""])[0].strip()
                if not text:
                    return self._send(400, "missing text")
                enqueue("__test__", text, synthetic=True)
                log("TEST danmu enqueued: " + text)
                self._send(200, "已入队: " + text)
            elif u.path == "/say":
                text = (qs.get("text") or [""])[0].strip()
                if not text:
                    return self._send(400, "missing text")
                threading.Thread(target=self._say_bg, args=(text,), daemon=True).start()
                self._send(200, "开始合成播报: " + text)
            elif u.path == "/scene":
                cmd = (qs.get("cmd") or [""])[0].strip()
                if not cmd:
                    return self._send(400, "missing cmd")
                if cmd == "status":
                    return self._send(200, json.dumps({
                        "current": STATE.get("current_scene"),
                        "hold_until": SCENE_STATE["hold_until"],
                        "pending": SCENE_STATE["pending"],
                        "switch_count": SCENE_STATE["switch_count"],
                    }, ensure_ascii=False))
                if scene_manual(cmd):
                    return self._send(200, "已安排切换: " + cmd)
                return self._send(400, "未知命令: " + cmd)
            elif u.path == "/pause":
                with LOCK:
                    STATE["enabled"] = False
                self._send(200, "paused")
            elif u.path == "/resume":
                with LOCK:
                    STATE["enabled"] = True
                self._send(200, "resumed")
            else:
                self._send(404, "not found")
        except Exception as e:
            self._send(500, "error: %s" % e)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            d = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            d = {}
        if u.path == "/api/llmtest":
            sub = d if isinstance(d, dict) else {}
            slot = dict(CONFIG["llm_main"])
            s = sub.get("llm_main") or {}
            if str(s.get("base_url") or "").strip():
                slot["base_url"] = str(s["base_url"]).strip().rstrip("/")
            if str(s.get("model") or "").strip():
                slot["model"] = str(s["model"]).strip()
            if str(s.get("api_key") or "").strip():
                slot["api_key"] = str(s["api_key"]).strip()
            try:
                reply, actual_m = _llm_chat_once(slot["base_url"], slot["model"], slot.get("api_key", ""),
                                       [{"role": "user", "content": "回复两个字：正常"}], timeout=15)
                same = actual_m == slot["model"]
                note = "" if same else ("（⚠ 8002代理改写为 %s）" % actual_m)
                return self._send(200, json.dumps({"ok": True, "reply": reply, "actual_model": actual_m, "note": note}, ensure_ascii=False))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)[:160]}, ensure_ascii=False))
        if u.path == "/api/config":
            try:
                apply_config(d)
                return self._send(200, json.dumps(CONFIG, ensure_ascii=False))
            except Exception as e:
                return self._send(400, "bad config: %s" % e)
        text = (d.get("text") or "").strip()
        nick = (d.get("nick") or "__test__").strip()
        if u.path == "/test" and text:
            enqueue(nick, text, synthetic=True)
            return self._send(200, "已入队: " + text)
        self._send(404, "not found")

    def _say_bg(self, text):
        try:
            dur, nb = play_tts(text)
            log("SAY played (%.1fs %dB): %s" % (dur, nb, text))
        except Exception as e:
            err("say: %s" % e)

def main():
    load_config()
    log("=" * 50)
    log("live-brain starting on port %d" % BRAIN_PORT)
    log("log_dir=%s tts=%s rag=%s llm=%s(%s)" %
        (LOG_DIR, VSA_TTS_URL, RAG_URL, LLM_BASE, LLM_MODEL))
    log("config: %s" % CONFIG)
    def obs_probe():
        try:
            v = _obs_call("GetVersion").get("obsVersion", "?")
            log("obs probe ok, version %s" % v)
        except Exception as e:
            err("obs probe: %s" % e)
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=obs_probe, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", BRAIN_PORT), Handler)
    srv.serve_forever()

if __name__ == "__main__":
    main()
