# moe-on-32gb

> **English summary.** Run **Qwen3.6-35B-A3B** (MoE: 35B total, ~3B active per token) locally on a **32 GB RAM + 12 GB VRAM** laptop with stock llama.cpp — no custom engine. Experts that don't fit in VRAM live in system RAM (`--fit on`), the model is loaded without mmap (`--load-mode none`, the key flag on a 32 GB machine), context is 32k, and sampling uses Qwen's official non-thinking values with presence penalty 1.5 — we measured that the DRY sampler (which we used before) corrupts long file paths the agent has to copy from its system prompt (26/72 vs 72/72 exact). On an RTX 5070 Ti Laptop (12 GB) with llama.cpp b11840, the default profile (`-ub 2048 -b 2048 --cache-ram 1024`) reads prompts at ~2,800 tok/s and generates ~57 tok/s; the fast profile adds the new GPU expert cache (`--moe-cache-mib 2048`) for ~75 tok/s generation at the cost of ~2 GB more RAM. Everything else we found online (MTP, DFlash, ik_llama.cpp, larger ubatch, offload thresholds, longer context, f16 KV) was measured and rejected — see `docs/benchmarks.md` and the benchmark script in `bench/`. One script (`start-llama.sh`) plus a per-flag explanation (`docs/params.md`), and a ready-to-use patch for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh/`, `docs/dsh.md`): local provider, a lean preset, and compaction thresholds recomputed for a 32k window (with the stock 65,536-token headroom, compaction never triggers on windows under ~70k), and per-tool output caps cut from 12,500 to 4,000 tokens. The idea of "experts in RAM, the rest on GPU" comes from [Strata](https://github.com/Niko1221/Strata), which targets a 125B model and needs 64 GB RAM; this repo is the 32 GB route. It is not based on Strata's code.

在 **32 GB 内存 + 12 GB 显存** 的笔记本上，用原版 llama.cpp 跑 **Qwen3.6-35B-A3B**（MoE：总共 35B 参数，每个字只用约 3B），当本地 Agent 的模型。不用另装推理引擎：一个启动脚本 + 每个参数为什么这么设；另附接 DeepSeek Harness（DSH）的补丁和启动脚本。

| 文件 | 是什么 |
|---|---|
| `start-llama.sh` | 起模型服务（OpenAI 兼容接口） |
| `docs/params.md` | 每个参数为什么这么设、踩过的坑 |
| `docs/benchmarks.md` | 网上找到的优化办法逐项实测：哪些有用、哪些没用 |
| `bench/` | 测速脚本（读提示、生成、Agent 每一步、内存、多轮前缀缓存、抄长路径）和原始结果 |
| `dsh/cordis.patch.yml`、`dsh/start-dsh-web.sh` | 接 DSH 网页界面：本地模型、精简预设、按 32k 重配的压缩线 |
| `docs/dsh.md` | DSH 的搭法、实测、踩过的坑、安全 |

## 和 Strata 的关系

思路来自 [Strata](https://github.com/Niko1221/Strata)：MoE 模型每个字只用到一小部分专家，所以每次都要用的部分（注意力、路由、上下文缓存）放显卡，大块的专家放内存。

Strata 跑的是 125B 的模型，中文正常的版本要 64 GB 内存，32 GB 的机器跑不了。这里换了一条路：

- **不换引擎**：llama.cpp 本来就能把专家放内存（`--fit` / `--n-cpu-moe`）
- **换小一点的 MoE**：总 35B、每个字只用 3B，量化后 17.7 GB，一半在显卡、一半在内存
- **为 32 GB 调参数**：不映射模型文件、KV 缓存 8 位、上下文 32k

本仓库没有用 Strata 的代码。

## 实测

环境：ROG Strix G16（AMD Ryzen 9 9955HX3D、RTX 5070 Ti Laptop 12 GB、32 GB DDR5）、Windows 11。

### 2026-10-23 优化后（llama.cpp b11836 / b11840）

| | 优化前（b11259，默认批次） | **默认** | **快速**（`MOE_CACHE_MIB=2048`） |
|---|---|---|---|
| 读长提示（约 7.5K token） | 1,235 token/秒 | **2,780** | 2,028 |
| 生成（真实任务） | 50.9 token/秒 | 58.4 | **75.1** |
| 读一次约 200 token 的工具结果 | — | **332 毫秒** | 569 毫秒 |
| 进程内存 | 9.9 GB | 10.1 GB | 12.0 GB |

- **默认**：读得快，内存和优化前差不多
- **快速**：写得快，内存多约 2 GB；按 Agent 一步「读 200、写 200 token」算，每步快约 16%

每一项怎么测的、网上还有哪些办法测了没用（MTP、DFlash、ik_llama.cpp、更大的批次……），见 [docs/benchmarks.md](docs/benchmarks.md)。

下午又在真的 DSH 里测了一轮，改了两处：
- **防复读从 DRY 换成 `presence_penalty 1.5`**：DRY 会把系统提示里的工作目录路径抄错（`deepseek-hardware`、`deepsee-harness`），读文件失败、多绕好几步。工作目录在系统提示里、一次用完整路径读 6 个文件，DRY 时 72 条只对 26 条，换了以后 72/72；同一个 DSH 任务从 4 到十几步降到 3 步
- **DSH 单次工具输出上限 12,500 → 4,000 token**（还有读文件 50 KB → 16 KB 等），见 [docs/dsh.md](docs/dsh.md)

也测了 64k、128k 上下文（64k 只慢约 3%，可选）和 KV f16（没好处）。实测 DSH 每一步只重读新加的部分，时间主要花在生成上。

### 2026-10-03 换模型时（llama.cpp b11259）

用法是给一个本地 Agent（[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)）当模型，对照组是同一台机器上之前用的稠密模型 Qwen3.8-27B（UD-IQ4_XS，上下文 16k）。

| | Qwen3.8-27B（稠密） | **Qwen3.6-35B-A3B（MoE，本仓库）** |
|---|---|---|
| 生成速度 | 6.5 token/秒 | **62 token/秒**（每次 57–66） |
| 读提示 | 约 500 token/秒 | 约 910 token/秒 |
| 上下文 | 16k | 32k |
| Agent 一轮「建项目」 | 36 秒 | 19 秒 |
| Agent 一轮长任务交付 | 12 分（16k 满了，反复压缩） | 5 秒（32k 用到 43%，没压缩） |
| 进程常驻内存 | 约 5–6 GB | 最多 10.3 GB |
| 显存 | 9.6 GB | 9.9 GB |
| 启动 | — | 约 15 秒 |

速度来自 llama-server 自己的日志，同一套 Agent 任务按原话各走一遍对比。只有一台机器，数字仅供参考。

**内存提醒**：模型开着时系统还剩约 5 GB（快速配置约 4 GB）。脚本默认空闲 30 分钟自动把模型从内存和显存卸掉（进程内存降到约 0.6 GB），下一个请求进来自动载回、等约 9 秒；玩游戏、跑别的大模型之前还是先关掉它。

## 怎么用

1. **llama.cpp**：从 [llama.cpp Releases](https://github.com/ggml-org/llama.cpp/releases) 下 CUDA 版（测过的是 b11840 + CUDA 13.4，b11840 修了 Windows 上连续运行约 10.5 天会崩溃的问题；Windows 要连同 cudart 那个包一起解压）。专家缓存 `--moe-cache-mib` 要 2026-10-07 以后的版本；新版的默认端口改成了 9931，脚本里写死了 8081。更新的版本应该也行，但参数名可能会变
2. **模型**：[unsloth/Qwen3.6-35B-A3B-GGUF](https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF) 里的 `Qwen3.6-35B-A3B-UD-IQ4_XS.gguf`（17,730,509,792 字节）。模型是阿里的，Apache 2.0
3. **密钥文件** `llama.key`：第一行 `#` 注释，第二行一个随机串，比如
   ```bash
   printf '# llama-server api key\n%s\n' "$(openssl rand -hex 24)" > llama.key
   ```
   密钥文件别提交（`.gitignore` 已经挡了 `*.key`）。**换行要用 LF**：在 Windows 上用记事本或 PowerShell 写出来的文件，行尾是 CRLF，`\r` 会被当成密钥的一部分，结果所有请求都返回 401
4. 改 `start-llama.sh` 开头四个路径（或者用同名环境变量），然后
   ```bash
   bash start-llama.sh
   ```
   要快速配置就在前面加 `MOE_CACHE_MIB=2048`；脚本后面的参数会原样追加给 llama-server。Windows 上用 Git Bash 跑
5. 接口：`http://127.0.0.1:8081/v1`（OpenAI 兼容），模型名 `qwen3.6-35b-a3b`，请求头 `Authorization: Bearer <密钥>`

试一下：

```bash
curl http://127.0.0.1:8081/v1/chat/completions \
  -H "Authorization: Bearer $(grep -v '^#' llama.key | head -n 1)" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.6-35b-a3b","messages":[{"role":"user","content":"用一句话介绍你自己"}]}'
```

### 接 DeepSeek Harness

现成的补丁和启动脚本在 `dsh/`，步骤见 [docs/dsh.md](docs/dsh.md)。2026-10-23 在新的空配置上原样搭过一遍：新会话默认用「本地 32k」预设和本地模型，一个查目录的小任务 1 轮 4 步、13 秒、61 token/秒。

### 接别的 Agent 时要注意

- **上下文窗口填 32768**，输出上限 4096 够用
- **压缩线要按 32k 重算**。有的 Agent 默认按大模型的窗口留余量（DeepSeek Harness 0.2.0-rc.2 默认留 65536 token），32k 窗口下算出来是负数，结果是只警告、从不主动压缩。我们用的是：触发线 = min(窗口 × 0.8, 窗口 − 输出上限 − 1024) = 26214
- **在机器上跑别的重活（占显卡、占内存）之前先停掉模型、跑完再开**，32 GB 两个一起开会很紧

## 参数说明

每个参数为什么这么设、踩过什么坑，见 [docs/params.md](docs/params.md)。最重要的四条：

1. `--load-mode none`：不映射模型文件。默认映射时系统剩余内存最低掉到 0.34 GB，不映射还剩 5.1 GB，速度还从 51 提到 62 token/秒
2. `-ub 2048 -b 2048`：读提示快将近一倍。放在内存里的专家，读长提示时要按批整块拷到显卡上算，批次越大拷得越少
3. 采样用 Qwen3.6 官方推荐值（含 `presence_penalty 1.5`）：llama.cpp 默认采样下 Agent 填工具参数会陷进复读；之前用的 DRY 防复读又会把工作目录这种长路径抄错（实测 26/72 一字不差，换成 presence 后 72/72）
4. 默认设置不安全（不要密钥、任何网页都能跨域调、`/slots` 能看到对话），脚本里都改了

## 许可

脚本和文档：MIT。模型权重和 llama.cpp 各按它们自己的许可。
