#!/usr/bin/env bash
# moe-on-32gb：32 GB 内存 + 12 GB 显存跑 Qwen3.6-35B-A3B（MoE，总 35B、每个字只用 3B）
# 每个参数为什么这么设，见 docs/params.md；实测数字见 docs/benchmarks.md
# 实测环境：Windows 11 + Git Bash、llama.cpp b11840 CUDA 13.4 版、RTX 5070 Ti Laptop 12 GB、32 GB DDR5、Ryzen 9 9955HX3D
#
# 用法：先改下面几个路径（或用同名环境变量覆盖），然后
#   bash start-llama.sh                      # 默认：省内存
#   MOE_CACHE_MIB=2048 bash start-llama.sh   # 快速：生成快约 30%，读提示慢约 25%，内存多占约 2 GB
#   SLEEP_IDLE_SECONDS=-1 bash start-llama.sh # 不要空闲自动卸载（默认空闲 30 分钟卸掉）
#   bash start-llama.sh -t 8 -C 55550000 --cpu-strict 1   # 后面的参数原样追加（比如绑核，见 docs/benchmarks.md）
# 起来以后是一个 OpenAI 兼容接口：http://127.0.0.1:8081/v1，模型名 qwen3.6-35b-a3b，请求头带 Authorization: Bearer <密钥>
set -euo pipefail

LLAMA_SERVER="${LLAMA_SERVER:-./llama.cpp/llama-server.exe}"        # llama.cpp 的 llama-server（Linux 上没有 .exe）；--moe-cache-mib 要 2026-10-07 以后的版本（测过 b11836、b11840）
MODEL="${MODEL:-./models/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf}"             # unsloth/Qwen3.6-35B-A3B-GGUF 的 UD-IQ4_XS
KEY_FILE="${KEY_FILE:-./llama.key}"                                    # 一行一个密钥，# 开头是注释；生成方法见 README
PORT="${PORT:-8081}"                                                   # 新版 llama.cpp 的默认端口改成了 9931，这里一定要写
MOE_CACHE_MIB="${MOE_CACHE_MIB:-0}"                                    # 0 = 不开专家缓存
SLEEP_IDLE_SECONDS="${SLEEP_IDLE_SECONDS:-1800}"                       # 空闲这么久就把模型从内存、显存卸掉（进程不退），下个请求自动载回（约 9 秒）；-1 = 不卸

[ -f "$LLAMA_SERVER" ] || { echo "找不到 llama-server：$LLAMA_SERVER" >&2; exit 1; }
[ -f "$MODEL" ] || { echo "找不到模型文件：$MODEL" >&2; exit 1; }
[ -f "$KEY_FILE" ] || { echo "找不到密钥文件：$KEY_FILE（不要不带密钥开服务，见 README「安全」）" >&2; exit 1; }
# CRLF 行尾的话，\r 会被当成密钥的一部分，所有请求都 401
if [ "$(tr -d '\r' < "$KEY_FILE" | wc -c)" -ne "$(wc -c < "$KEY_FILE")" ]; then echo "密钥文件是 CRLF 行尾，请改成 LF：$KEY_FILE" >&2; exit 1; fi

CACHE_ARGS=()
if [ "$MOE_CACHE_MIB" != "0" ]; then CACHE_ARGS=(--moe-cache-mib "$MOE_CACHE_MIB"); fi

exec "$LLAMA_SERVER" \
  -m "$MODEL" --load-mode none \
  --host 127.0.0.1 --port "$PORT" -c 32768 --device CUDA0 --fit on -ctk q8_0 -ctv q8_0 -np 1 \
  -ub 2048 -b 2048 --cache-ram 1024 "${CACHE_ARGS[@]}" --sleep-idle-seconds "$SLEEP_IDLE_SECONDS" \
  --jinja --reasoning-format deepseek --chat-template-kwargs '{"enable_thinking":false}' --alias qwen3.6-35b-a3b \
  --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0 --presence-penalty 1.5 \
  --api-key-file "$KEY_FILE" --cors-origins localhost --no-slots \
  "$@"
