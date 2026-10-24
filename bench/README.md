# 测速脚本

`bench.py` 用给定参数起一次 llama-server，测 Agent 关心的几项，测完停掉。`docs/benchmarks.md` 里的数字都是它测出来的，原始结果在 `runs/`（每行一次，JSON）。

```bash
python bench/bench.py --server <llama-server.exe> --model <gguf> --label 名字 --out bench/runs/mine.jsonl -- -ub 2048 -b 2048
```

- `--` 后面的参数追加在 `start-llama.sh` 那套基础参数后面；要去掉某个基础参数用 `--drop=--fit` 这种写法
- 需要 Python 3.9+ 和 `psutil`；显存用 `nvidia-smi` 读
- 进程设成「低于正常」优先级

`agent_task.py` 测端到端：照 DSH 的方式跑一个多步任务（读一个 CSV、建 5 个文件夹写 6 个文件），记总耗时和读提示 / 生成各花多少。比测速脚本更接近真实使用，适合比较两套参数：

```bash
python bench/agent_task.py <llama-server.exe> <gguf> default bench/runs/mine-e2e.jsonl
python bench/agent_task.py <llama-server.exe> <gguf> fast bench/runs/mine-e2e.jsonl --moe-cache-mib 2048
```

模型每次写的字数不一样，比较时要按生成 token 数折算。

`client_bench.py` 只当客户端，对任何 OpenAI 兼容的服务（llama-server、PolyStrata 等）用流式接口从客户端计时：读长提示的首字延迟、生成速度、抄路径、多步 Agent 任务。比较不同引擎时用它，保证是同一把尺子：

```bash
python bench/client_bench.py http://127.0.0.1:8081/v1 <密钥> 名字 bench/runs/mine-client.jsonl
```

## 测什么

| 字段 | 是什么 |
|---|---|
| `load_s` | 启动到 `/health` 返回 200 的秒数 |
| `pp_tps` | 读一段新的约 7.5K token 长提示的速度（模拟 Agent 第一轮） |
| `tg_tps` | 接着这段长提示生成 256 token 的速度 |
| `chat_tg_tps` | 一个真实任务（写代码 + 中文解释）的生成速度。专家缓存这类优化要看这一项：随机词串选专家的规律和真话不一样 |
| `turn_prompt_n` | 同一前缀后面加十来个 token 再请求时，实际重读了多少 token（前缀缓存正常就是十来个） |
| `tool_prompt_ms` | 同一前缀后面加约 200 token（像一次工具结果）时，读这段花的毫秒数。Agent 每一步都要付这一笔 |
| `proc_uss_gb` | llama-server 进程独占内存峰值（≈ 任务管理器里的「工作集-私有」，不含显卡驱动给显存记的提交量） |
| `sys_avail_min_gb` | 测试期间系统可用内存最低值（和当时开着什么别的程序有关，只在同一时段内比较） |
| `gpu_peak_mib` | 显存占用峰值（含其他程序） |

可选：

| 参数 | 做什么 |
|---|---|
| `--agent` | 多轮 Agent：带工具、模型真的调工具、答完用户再追问，记每一步实际重读了多少 token |
| `--side` | 和 `--agent` 一起用：第一步之后插一个旁路请求（像框架给会话起标题），看主对话能不能从 `--cache-ram` 恢复 |
| `--copy N` | 抄长路径：6 条前缀很长、彼此相似的中文路径写在用户消息里，让模型在一条回复里调 6 次工具，跑 N 次，数参数里一字不差的有几条 |
| `--copy-cn N` | 中文工作目录、文件名里重复歌名（翻唱项目那种路径）；看 presence penalty 会不会罚路径里重复的字（实测 0 / 0.5 / 1.5 都是 72/72）。也数工具调用漏进正文的次数 |
| `--copy-cwd N` | 抄长路径的另一种测法，更像 Agent 框架：工作目录写在系统提示里，用户只给 6 个文件名，模型要自己拼完整路径。DRY 防复读的问题只有这种测法测得出来（DRY 0.8 时 26/72，presence 1.5 时 72/72） |
| `--rewrite N` | 改写文件：约 1,500 token 的代码，改一个函数名、其余原样输出，跑 N 次；记生成速度和输出跟正确答案的相似度（1.0 = 一字不差）。n-gram 投机解码专门加速这种情况 |
| `--skip-speed` | 不测速度，只跑上面这些 |

## 注意

- 每项默认跑 3 次取中位数。同一配置隔一段时间再测，生成速度会差 ±5% 左右，比这小的差别别当成提升
- 测之前看一眼 `nvidia-smi`：别的程序（浏览器、桌面壁纸、正在流式输出的聊天客户端）占着显卡时，生成会明显变慢
