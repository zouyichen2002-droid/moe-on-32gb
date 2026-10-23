"""
端到端跑一个 Agent 多步任务：照 DSH 的方式（只往后追加、带工具）直接和 llama-server 对话，记总耗时、读提示 / 生成各花多少。
系统提示做成和 DSH 差不多长（约 5K token），工具只有读文件、写文件（在一个临时目录里真读真写）。
任务：读 data.csv，给 5 首歌各建一个文件夹写 info.md，再写 index.md。

用法：python bench/agent_task.py <llama-server.exe> <模型.gguf> <名字> <结果.jsonl> [额外的 llama-server 参数...]
参数照 start-llama.sh 的默认配置；额外参数追加在后面，比如 --moe-cache-mib 2048。端口 8096。
"""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request

import psutil

EXE, MODEL, LABEL, OUT = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
EXTRA = sys.argv[5:]
PORT = 8096
key = secrets.token_hex(16)
fd, key_file = tempfile.mkstemp(suffix=".key")
with os.fdopen(fd, "w", newline="\n") as f:
    f.write("# t\n" + key + "\n")
args = [EXE, "-m", MODEL, "--port", str(PORT), "--api-key-file", key_file, "--load-mode", "none", "--host", "127.0.0.1",
        "-c", "32768", "--device", "CUDA0", "--fit", "on", "-ctk", "q8_0", "-ctv", "q8_0", "-np", "1",
        "-ub", "2048", "-b", "2048", "--cache-ram", "1024", "--jinja", "--reasoning-format", "deepseek",
        "--chat-template-kwargs", '{"enable_thinking":false}', "--alias", "m", "--temp", "0.7", "--top-p", "0.8",
        "--top-k", "20", "--min-p", "0", "--presence-penalty", "1.5", "--no-slots"] + EXTRA
proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS)

WS = tempfile.mkdtemp(prefix="ws-")
FILES = {"data.csv": "歌名,时长秒,版本数\n怪物,206,6\n逃跑的天使,231,3\n公主,198,7\n弈,244,3\n我奉陪,215,3\n"}
for n, c in FILES.items():
    with open(os.path.join(WS, n), "w", encoding="utf-8") as f:
        f.write(c)
TOOLS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a UTF-8 text file. Path relative to the workspace.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or overwrite a UTF-8 text file (parent folders are created). Path relative to the workspace.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                                                     "required": ["path", "content"]}}},
]
PAD = "\n".join(f"- Guideline {i}: keep tool calls minimal, prefer relative paths, verify results before answering, "
                f"and never invent file contents. Section {i} covers conventions for logs, notes and deliverables."
                for i in range(140))
SYSTEM = "You are a coding agent working in a local workspace.\n" + PAD
TASK = ("读 data.csv，给每首歌建一个文件夹（名字就是歌名），每个文件夹里写一个 info.md，内容是歌名、时长（换算成分秒）、"
        "版本数，再写一句说明。最后在根目录写一个 index.md，列出这五个 info.md 的路径。")


def post(body):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


res = {"label": LABEL, "extra": EXTRA}
try:
    t0 = time.time()
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.5)
        if time.time() - t0 > 300:
            raise SystemExit("no start")
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}]
    steps, pp_ms, tg_ms, tg_n, wall0 = 0, 0.0, 0.0, 0, time.time()
    for _ in range(20):
        j = post({"model": "m", "messages": msgs, "tools": TOOLS, "max_tokens": 1500})
        t = j["timings"]
        steps += 1
        pp_ms += t["prompt_ms"]
        tg_ms += t["predicted_ms"]
        tg_n += t["predicted_n"]
        m = j["choices"][0]["message"]
        msgs.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls")})
        if not m.get("tool_calls"):
            break
        for tc in m["tool_calls"]:
            a = json.loads(tc["function"]["arguments"])
            p = os.path.join(WS, a.get("path", ""))
            if tc["function"]["name"] == "read_file":
                out = open(p, encoding="utf-8").read() if os.path.isfile(p) else "error: not found"
            else:
                os.makedirs(os.path.dirname(p) or WS, exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(a.get("content", ""))
                out = "ok"
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
    made = sorted(os.path.relpath(os.path.join(d, f), WS) for d, _, fs in os.walk(WS) for f in fs)
    res.update(steps=steps, wall_s=round(time.time() - wall0, 1), prompt_s=round(pp_ms / 1000, 1),
               gen_s=round(tg_ms / 1000, 1), gen_tokens=tg_n, files=len(made))
finally:
    for c in psutil.Process(proc.pid).children(recursive=True):
        c.kill()
    proc.kill()
    proc.wait(30)
    os.remove(key_file)
line = json.dumps(res, ensure_ascii=False)
print(line)
with open(OUT, "a", encoding="utf-8") as f:
    f.write(line + "\n")
