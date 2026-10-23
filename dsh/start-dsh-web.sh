#!/usr/bin/env bash
# moe-on-32gb：DSH 网页界面，接本地模型（先用 start-llama.sh 起模型）
# - 只开在本机 127.0.0.1:3080，不自动开浏览器；带访问令牌的地址打在输出里（输出存成文件的话，那个文件别给别人看）
# - 遥测关掉：默认会在你点反馈时把会话片段发到 DeepSeek 的服务器
# - 权限预设 workspace-write：沙箱只许写工作区，越界的先问你
# - 工作区不由启动目录决定：第一次用时 DSH 自己建「文档/deepseek-harness/default-workspace」；
#   要用别的目录，在界面左栏「工作区」旁边添加（见 docs/dsh.md）
#
# 用法：先改下面三个路径（或用同名环境变量覆盖），然后
#   bash dsh/start-dsh-web.sh
set -euo pipefail

DSH_DIR="${DSH_DIR:-./dsh-install}"          # npm install @deepseek-ai/dsh@0.2.0-rc.2 装到的目录
export DSH_HOME="${DSH_HOME:-./dsh-home}"     # DSH 的配置档、会话、凭据放这里（不放用户目录）
KEY_FILE="${KEY_FILE:-./llama.key}"           # 和 start-llama.sh 用同一个密钥文件

DSH_BIN="$DSH_DIR/node_modules/.bin/dsh"
[ -f "$DSH_BIN" ] || { echo "找不到 dsh：$DSH_BIN" >&2; exit 1; }
[ -f "$KEY_FILE" ] || { echo "找不到密钥文件：$KEY_FILE" >&2; exit 1; }
mkdir -p "$DSH_HOME"
DSH_HOME="$(cd "$DSH_HOME" && pwd)"           # 转成绝对路径

export DSH_TELEMETRY_DISABLED=1
export DSH_PERMISSION_MODE=workspace-write
LOCAL_LLAMA_KEY="$(grep -v '^#' "$KEY_FILE" | head -n 1 | tr -d '\r')"
export LOCAL_LLAMA_KEY
exec "$DSH_BIN" web --no-open
