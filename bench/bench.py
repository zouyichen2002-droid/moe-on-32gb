"""moe-on-32gb 测速：用给定参数起一次 llama-server，测 Agent 关心的几项，测完停掉。

测什么（都从 llama-server 自己返回的 timings 里取）：
  load    从启动到 /health 返回 200 的秒数
  pp      读一段新的长提示（默认约 8K token，模拟 Agent 第一轮的系统提示 + 工具说明）的速度
  tg      接着这段长提示往下生成 256 个 token 的速度（带着 8K 上下文生成，比空上下文慢一点）
  turn    在同一个前缀后面加一小段新内容再生成：看前缀缓存有没有用上（只该重读新加的几十个 token）
  mem     llama-server 进程「工作集-私有」峰值、测试期间系统剩余内存最低值、显存占用峰值
每项跑 --repeat 次取中位数。进程设成「低于正常」优先级（和平时一样）。

用法：
  python bench/bench.py --server <llama-server.exe> --model <gguf> [--label 名字] [--out results.jsonl] -- [额外的 llama-server 参数]
额外参数追加在基础参数（最初那版 start-llama.sh，不含 -ub / --cache-ram）后面（同名参数以后出现的为准）；要去掉某个基础参数用 --drop，比如 --drop=--fit。
需要 Python 3.9+ 和 psutil；显存用 nvidia-smi 读。
"""
import argparse
import json
import os
import random
import secrets
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import psutil

# 基础参数 = 最初那版 start-llama.sh（不含 -ub / --cache-ram，方便和优化前对照；不含 -m / --port / 密钥）
BASE_ARGS = [
    "--load-mode", "none",
    "--host", "127.0.0.1", "-c", "32768", "--device", "CUDA0", "--fit", "on",
    "-ctk", "q8_0", "-ctv", "q8_0", "-np", "1",
    "--jinja", "--reasoning-format", "deepseek", "--chat-template-kwargs", '{"enable_thinking":false}',
    "--alias", "qwen3.6-35b-a3b",
    "--temp", "0.7", "--top-p", "0.8", "--top-k", "20", "--min-p", "0", "--dry-multiplier", "0.8",
    "--cors-origins", "localhost", "--no-slots",
]
# 带值的基础参数：--drop 时连值一起去掉
TAKES_VALUE = {"--load-mode", "--host", "-c", "--device", "--fit", "-ctk", "-ctv", "-np", "--reasoning-format",
               "--chat-template-kwargs", "--alias", "--temp", "--top-p", "--top-k", "--min-p", "--dry-multiplier",
               "--cors-origins"}

WORDS = ("function return value config error retry timeout buffer token session agent tool result "
         "file path directory string number list object index offset length parse render update delete "
         "模型 工具 会话 文件 目录 参数 结果 错误 重试 超时 缓存 上下文 压缩 插件 预设 权限 沙箱 工作区 "
         "用户 请求 响应 接口 密钥 端口 进程 内存 显存 专家 层 批次 速度").split()


CHAT_TASK = ("写一个 Python 函数 parse_log(path)：读取日志文件，按行解析「时间 级别 模块 消息」四列，"
             "统计每个模块的 ERROR 数量，返回按数量从多到少排好的列表。先写代码，再用中文逐段解释，最后给三个测试用例。")


AGENT_TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "读取一个文本文件的内容",
    "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "文件路径"}},
                   "required": ["path"]}}}]
FAKE_FILES = {"a.txt": "项目名：demo\n版本：1.2.0\n作者：test", "b.txt": "依赖：requests>=2.31\n依赖：rich",
              "c.txt": "TODO：补测试\nTODO：改日志格式"}


def agent_turns(srv, ctx_words, side=False):
    """多轮 Agent：带工具、模型真的调工具，答完以后用户再追问。记每次请求实际重读了多少 token（prompt_n）。
    前缀缓存正常的话，除了第一次，每次都只该重读新加的那一小段。"""
    msgs = [{"role": "system", "content": "你是一个 Agent，按需调用 read_file 读文件，读完再回答。参考资料：\n" + make_text("agent", ctx_words)},
            {"role": "user", "content": "依次读 a.txt 和 b.txt（一次只调一个工具），然后用一句话总结。"}]
    steps = []
    for follow_up in (None, "再读 c.txt，告诉我还有几条 TODO。"):
        if follow_up:
            msgs.append({"role": "user", "content": follow_up})
        for _ in range(6):
            j = srv.post("/v1/chat/completions", {"model": "qwen3.6-35b-a3b", "temperature": 0, "max_tokens": 300,
                                                  "messages": msgs, "tools": AGENT_TOOLS})
            m = j["choices"][0]["message"]
            steps.append({"total": j["usage"]["prompt_tokens"], "reprocessed": j["timings"]["prompt_n"],
                          "ms": round(j["timings"]["prompt_ms"]), "user_turn": bool(follow_up),
                          "tool": bool(m.get("tool_calls"))})
            msgs.append({k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "reasoning_content")})
            if side and len(steps) == 1:
                # 旁路请求（像 Agent 框架给会话起标题）：会把主对话挤出唯一的槽位，下一步要靠 --cache-ram 里存的状态恢复
                srv.post("/v1/chat/completions", {"model": "qwen3.6-35b-a3b", "temperature": 0, "max_tokens": 16,
                                                  "messages": [{"role": "user", "content": "给这句话起个四个字的标题：读文件然后总结"}]})
                steps.append({"side_request": True})
            if not m.get("tool_calls"):
                break
            for tc in m["tool_calls"]:
                path = json.loads(tc["function"]["arguments"]).get("path", "")
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": FAKE_FILES.get(path, "（文件不存在）")})
    return steps


COPY_PATHS = [f"D:\\workspace\\项目甲\\第{n}版\\输出\\歌曲_扒谱_定稿（10-07_23.{n}0存）\\track_{n:02d}_vocal_main.svp"
              for n in range(1, 7)]


def copy_test(srv, runs, temperature):
    """抄长路径：6 条前缀很长、彼此相似的路径，让模型逐个调 read_file。数参数里的路径一字不差的有几条。
    防复读（DRY）会罚刚出现过的长片段，路径里的 \\ 和 / 又不是它的断点，可能把模型推着抄错。"""
    listing = "\n".join(COPY_PATHS)
    ok = total = calls = 0
    wrong = []
    for r in range(runs):
        msgs = [{"role": "system", "content": "你是一个 Agent。调用工具时，路径必须和用户给的一字不差。"},
                {"role": "user", "content": f"下面 6 个文件，请依次每个调用一次 read_file（6 次调用，同一条回复里全部发出）：\n{listing}"}]
        j = srv.post("/v1/chat/completions", {"model": "qwen3.6-35b-a3b", "temperature": temperature, "max_tokens": 1500,
                                              "messages": msgs, "tools": AGENT_TOOLS, "parallel_tool_calls": True,
                                              "seed": 1000 + r})
        got = []
        for tc in j["choices"][0]["message"].get("tool_calls") or []:
            try:
                got.append(json.loads(tc["function"]["arguments"]).get("path", ""))
            except Exception:
                got.append("<参数不是 JSON>")
        calls += len(got)
        for p in COPY_PATHS:
            total += 1
            if p in got:
                ok += 1
        wrong += [g for g in got if g not in COPY_PATHS][:3]
    return {"copy_exact": ok, "copy_total": total, "copy_calls": calls, "copy_wrong_examples": wrong[:5]}


def drop_args(args, drops):
    out, i = [], 0
    while i < len(args):
        if args[i] in drops:
            i += 2 if args[i] in TAKES_VALUE else 1
            continue
        out.append(args[i])
        i += 1
    return out


def make_text(seed, n_words):
    rng = random.Random(seed)
    lines = []
    for i in range(n_words // 12):
        lines.append(f"{i:05d} " + " ".join(rng.choice(WORDS) for _ in range(12)))
    return "\n".join(lines)


class Server:
    def __init__(self, exe, model, port, extra, drops, log_path):
        self.port = port
        self.key = secrets.token_hex(16)
        fd, self.key_file = tempfile.mkstemp(suffix=".key")
        with os.fdopen(fd, "w", newline="\n") as f:
            f.write("# bench\n" + self.key + "\n")
        args = drop_args(BASE_ARGS, set(drops)) + list(extra)
        self.cmd = [exe, "-m", model, "--port", str(port), "--api-key-file", self.key_file] + args
        self.log = open(log_path, "w", encoding="utf-8", errors="replace")
        self.proc = None

    def start(self, timeout=300):
        t0 = time.time()
        self.proc = subprocess.Popen(self.cmd, stdout=self.log, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server 退出了，退出码 {self.proc.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                    if r.status == 200:
                        return time.time() - t0
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("超时没起来")

    def post(self, path, body, timeout=600):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            for c in psutil.Process(self.proc.pid).children(recursive=True):
                c.kill()
            self.proc.kill()
            self.proc.wait(30)
        self.log.close()
        os.remove(self.key_file)


class MemWatch(threading.Thread):
    """每 0.5 秒记一次：进程工作集-私有（Windows 上是 private_bytes 以外的那个口径，用 psutil 的 private 近似）、系统剩余、显存。"""

    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid, self.stop_flag = pid, False
        self.proc_peak = 0
        self.sys_min = float("inf")
        self.gpu_peak = 0

    def run(self):
        p = psutil.Process(self.pid)
        while not self.stop_flag:
            try:
                mi = p.memory_full_info()
                # Windows：uss = 工作集里只属于这个进程的部分（≈「工作集-私有」），不含驱动给显存记的提交量
                self.proc_peak = max(self.proc_peak, mi.uss)
            except Exception:
                pass
            self.sys_min = min(self.sys_min, psutil.virtual_memory().available)
            try:
                out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5).stdout.split()
                self.gpu_peak = max(self.gpu_peak, int(out[0]))
            except Exception:
                pass
            time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--label", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--ctx-words", type=int, default=3600, help="长提示的词数（默认约 8K token）")
    ap.add_argument("--gen", type=int, default=256)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--drop", action="append", default=[])
    ap.add_argument("--agent", action="store_true", help="再跑一段多轮 Agent 对话，看追问时重读多少 token")
    ap.add_argument("--side", action="store_true", help="多轮 Agent 中间插一个旁路请求，看主对话能不能从缓存恢复")
    ap.add_argument("--copy", type=int, default=0, help="抄长路径测试跑几次（0 = 不跑）")
    ap.add_argument("--copy-temp", type=float, default=0.7)
    ap.add_argument("--skip-speed", action="store_true", help="不测速度，只跑 --copy / --agent")
    ap.add_argument("--log", default="")
    a, extra = ap.parse_known_args()
    if extra and extra[0] == "--":
        extra = extra[1:]

    log_path = a.log or os.path.join(tempfile.gettempdir(), f"bench-{int(time.time())}.log")
    srv = Server(a.server, a.model, a.port, extra, a.drop, log_path)
    res = {"label": a.label, "extra": extra, "drop": a.drop, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    watch = None
    try:
        res["load_s"] = round(srv.start(), 1)
        watch = MemWatch(srv.proc.pid)
        watch.start()
        pp, tg, turn_n, turn_ms, chat_tg, tool_n, tool_ms, ctx_tokens = [], [], [], [], [], [], [], 0
        for r in range(0 if a.skip_speed else a.repeat):
            seed = f"{time.time()}-{r}"           # 每次一段新文字：前缀缓存用不上，测的是真正的读提示速度
            text = make_text(seed, a.ctx_words)
            j = srv.post("/completion", {"prompt": text, "n_predict": a.gen, "ignore_eos": True,
                                         "temperature": 0, "cache_prompt": True})
            t = j["timings"]
            ctx_tokens = t["prompt_n"]
            pp.append(t["prompt_per_second"])
            tg.append(t["predicted_per_second"])
            # 同一前缀 + 一小段新内容：只该重读新加的部分
            j2 = srv.post("/completion", {"prompt": text + j["content"] + "\n下一步：检查 结果 并 返回", "n_predict": 32,
                                          "ignore_eos": True, "temperature": 0, "cache_prompt": True})
            turn_n.append(j2["timings"]["prompt_n"])
            turn_ms.append(j2["timings"]["prompt_ms"])
            # 同一前缀 + 一段约 250 token 的新内容（像一次工具结果）：Agent 每一步的典型新增量
            j4 = srv.post("/completion", {"prompt": text + j["content"] + "\n工具结果：\n" + make_text(seed + "-tool", 100),
                                          "n_predict": 1, "temperature": 0, "cache_prompt": True})
            tool_n.append(j4["timings"]["prompt_n"])
            tool_ms.append(j4["timings"]["prompt_ms"])
            # 真实任务的生成：随机词串选专家的规律和真话不一样，专家缓存这类优化要看这一项
            j3 = srv.post("/v1/chat/completions", {"model": "qwen3.6-35b-a3b", "temperature": 0, "max_tokens": a.gen,
                                                   "messages": [{"role": "user", "content": CHAT_TASK + f"（第 {r + 1} 次）"}]})
            chat_tg.append(j3["timings"]["predicted_per_second"])
        if a.copy:
            res.update(copy_test(srv, a.copy, a.copy_temp))
        if a.agent:
            steps = agent_turns(srv, a.ctx_words // 2, side=a.side)
            res["agent_steps"] = steps
            main = [s for s in steps if not s.get("side_request")]
            # 追问后的第一次请求：前缀缓存正常时只重读追问那几十个 token
            first_follow = next((s for s in main if s["user_turn"]), None)
            res["agent_followup_reprocessed"] = first_follow["reprocessed"] if first_follow else None
            # 旁路请求之后的那一步：缓存恢复成功时也只重读几十个 token
            if a.side and len(main) > 1:
                res["agent_after_side_reprocessed"] = main[1]["reprocessed"]
        if pp:
            res.update({
                "chat_tg_tps": round(statistics.median(chat_tg), 1), "chat_tg_all": [round(x, 1) for x in chat_tg],
                "ctx_tokens": ctx_tokens,
                "pp_tps": round(statistics.median(pp), 1), "pp_all": [round(x, 1) for x in pp],
                "tg_tps": round(statistics.median(tg), 1), "tg_all": [round(x, 1) for x in tg],
                "turn_prompt_n": int(statistics.median(turn_n)), "turn_prompt_ms": round(statistics.median(turn_ms)),
                "tool_prompt_n": int(statistics.median(tool_n)), "tool_prompt_ms": round(statistics.median(tool_ms)),
            })
    except Exception as e:
        res["error"] = str(e)
    finally:
        if watch:
            watch.stop_flag = True
            watch.join(5)
            res.update({"proc_uss_gb": round(watch.proc_peak / 2**30, 2),
                        "sys_avail_min_gb": round(watch.sys_min / 2**30, 2) if watch.sys_min != float("inf") else None,
                        "gpu_peak_mib": watch.gpu_peak})
        srv.stop()
    res["log"] = log_path
    line = json.dumps(res, ensure_ascii=False)
    print(line)
    if a.out:
        with open(a.out, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return 1 if "error" in res else 0


if __name__ == "__main__":
    sys.exit(main())
