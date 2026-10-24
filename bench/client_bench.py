"""Client-only benchmark against any OpenAI-compatible server (llama-server or PolyStrata), measured from the client
with streaming: time to first token (prompt read) and decode rate. Also path-copy accuracy and a multi-step agent task.
Usage: python client_bench.py <base_url e.g. http://127.0.0.1:8097/v1> <api_key> <label> <out.jsonl>"""
import json
import os
import random
import sys
import tempfile
import time
import urllib.request

import psutil

BASE, KEY, LABEL, OUT = sys.argv[1:5]
WORDS = ("function return value config error retry timeout buffer token session agent tool result file path "
         "模型 工具 会话 文件 目录 参数 结果 错误 重试 超时 缓存 上下文 压缩 插件 预设 权限 沙箱 工作区").split()


def make_text(seed, n_words):
    rng = random.Random(seed)
    return "\n".join(f"{i:05d} " + " ".join(rng.choice(WORDS) for _ in range(12)) for i in range(n_words // 12))


def stream(messages, max_tokens, tools=None, temperature=0.0):
    body = {"model": "m", "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": True,
            "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(BASE + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    t0 = time.time()
    first = None
    n_chunks = 0
    usage = {}
    content, calls = "", {}
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            j = json.loads(data)
            if j.get("usage"):
                usage = j["usage"]
            for ch in j.get("choices", []):
                d = ch.get("delta") or {}
                if d.get("content") or d.get("tool_calls"):
                    if first is None:
                        first = time.time()
                    n_chunks += 1
                content += d.get("content") or ""
                for tc in d.get("tool_calls") or []:
                    c = calls.setdefault(tc.get("index", 0), {"id": tc.get("id"), "name": "", "args": ""})
                    f = tc.get("function") or {}
                    c["name"] += f.get("name") or ""
                    c["args"] += f.get("arguments") or ""
                    if tc.get("id"):
                        c["id"] = tc["id"]
    t1 = time.time()
    gen = usage.get("completion_tokens") or n_chunks
    ttft = (first or t1) - t0
    rate = (gen - 1) / (t1 - first) if first and t1 > first and gen > 1 else 0
    return {"ttft": ttft, "total": t1 - t0, "gen": gen, "rate": rate, "prompt": usage.get("prompt_tokens"),
            "content": content, "calls": [calls[k] for k in sorted(calls)]}


TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a text file",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                                          "required": ["path"]}}},
         {"type": "function", "function": {"name": "write_file", "description": "Create or overwrite a text file",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                                                           "content": {"type": "string"}},
                                                          "required": ["path", "content"]}}}]
CHAT = ("写一个 Python 函数 parse_log(path)：读取日志文件，按行解析「时间 级别 模块 消息」四列，统计每个模块的 ERROR 数量，"
        "返回按数量从多到少排好的列表。先写代码，再用中文逐段解释，最后给三个测试用例。")
res = {"label": LABEL}
avail_min = psutil.virtual_memory().available

# 1. long prompt (~7.5K tokens) read + 256 generated; fresh text each time so no prefix reuse
pp, tg = [], []
for r in range(2):
    s = stream([{"role": "user", "content": make_text(f"{time.time()}-{r}", 3600) + "\n\n续写上面的列表。"}], 256)
    pp.append((s["prompt"] or 0) / s["ttft"] if s["ttft"] else 0)
    tg.append(s["rate"])
    avail_min = min(avail_min, psutil.virtual_memory().available)
res.update(long_prompt_tokens=s["prompt"], pp_tps=round(max(pp), 1), long_tg_tps=round(max(tg), 1))

# 2. real task generation
ch = [stream([{"role": "user", "content": CHAT + f"（第 {r + 1} 次）"}], 256)["rate"] for r in range(2)]
res["chat_tg_tps"] = round(max(ch), 1)

# 3. path copy: cwd in the system prompt (en + cn), 6 parallel reads, temp 0.7
CASES = {"en": (r"C:\Users\demo\Documents\deepseek-harness\default-workspace",
                ["notes.txt", "todo.txt", "data.csv", "plan.md", "readme.md", "config.json"]),
         "cn": (r"E:\workspace\公主", [r"r02\公主_扒谱_定稿（10-07_23.40存）.svp", r"r02\说明.md", r"r02\歌词.txt",
                                     r"r07\公主_扒谱_终版（10-08_00.18存）.svp", r"r07\说明.md", r"视频\v03\公主_歌词视频_v03.mp4"])}
for name, (cwd, files) in CASES.items():
    want = [cwd + "\\" + f for f in files]
    ok = 0
    bad = []
    for r in range(8):
        s = stream([{"role": "system", "content": f"You are a coding agent. Your working directory is {cwd}. Always pass absolute paths to tools."},
                    {"role": "user", "content": "读一下工作目录里的 " + "、".join(files) + "，同一条回复里把 6 次 read_file 全部发出，用完整路径。"}],
                   1500, tools=TOOLS[:1], temperature=0.7)
        got = []
        for c in s["calls"]:
            try:
                got.append(json.loads(c["args"]).get("path", ""))
            except Exception:
                got.append("<bad json>")
        ok += sum(1 for p in want if p in got)
        bad += [g for g in got if g not in want][:2]
    res[f"copy_{name}"] = f"{ok}/{8 * len(want)}"
    res[f"copy_{name}_bad"] = bad[:3]

# 4. multi-step agent task with real file tools in a temp dir
ws = tempfile.mkdtemp(prefix="cb-")
with open(os.path.join(ws, "data.csv"), "w", encoding="utf-8") as f:
    f.write("歌名,时长秒,版本数\n怪物,206,6\n逃跑的天使,231,3\n公主,198,7\n弈,244,3\n我奉陪,215,3\n")
pad = "\n".join(f"- Guideline {i}: keep tool calls minimal, prefer relative paths, verify results before answering, "
                f"and never invent file contents. Section {i} covers conventions for logs, notes and deliverables." for i in range(140))
msgs = [{"role": "system", "content": "You are a coding agent working in a local workspace.\n" + pad},
        {"role": "user", "content": "读 data.csv，给每首歌建一个文件夹（名字就是歌名），每个文件夹里写一个 info.md，内容是歌名、时长（换算成分秒）、"
                                    "版本数，再写一句说明。最后在根目录写一个 index.md，列出这五个 info.md 的路径。"}]
t0 = time.time()
steps = gen = 0
for _ in range(20):
    s = stream(msgs, 1500, tools=TOOLS, temperature=0.7)
    steps += 1
    gen += s["gen"]
    avail_min = min(avail_min, psutil.virtual_memory().available)
    asst = {"role": "assistant", "content": s["content"] or ""}
    if s["calls"]:
        asst["tool_calls"] = [{"id": c["id"] or f"c{steps}{i}", "type": "function",
                               "function": {"name": c["name"], "arguments": c["args"]}} for i, c in enumerate(s["calls"])]
    msgs.append(asst)
    if not s["calls"]:
        break
    for i, c in enumerate(s["calls"]):
        try:
            a = json.loads(c["args"])
        except Exception:
            a = {}
        p = os.path.join(ws, a.get("path", ""))
        if c["name"] == "read_file":
            out = open(p, encoding="utf-8").read() if os.path.isfile(p) else "error: not found"
        else:
            os.makedirs(os.path.dirname(p) or ws, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(a.get("content", ""))
            out = "ok"
        msgs.append({"role": "tool", "tool_call_id": asst["tool_calls"][i]["id"], "content": out})
made = sorted(os.path.relpath(os.path.join(d, f), ws) for d, _, fs in os.walk(ws) for f in fs)
res.update(agent_steps=steps, agent_wall_s=round(time.time() - t0, 1), agent_gen_tokens=gen, agent_files=len(made),
           sys_avail_min_gb=round(avail_min / 2**30, 2))
line = json.dumps(res, ensure_ascii=False)
print(line)
with open(OUT, "a", encoding="utf-8") as f:
    f.write(line + "\n")
