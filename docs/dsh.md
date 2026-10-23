# 接 DeepSeek Harness（DSH）

把 `start-llama.sh` 起的本地模型接到 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的网页界面上，当本地 Agent 用。文件在 `dsh/`：

| 文件 | 是什么 |
|---|---|
| `dsh/cordis.patch.yml` | DSH 的补丁层：模型 → 本地 llama-server；精简预设「本地 32k」（压缩线按 32k 重配）；选目录用网页对话框 |
| `dsh/start-dsh-web.sh` | 起 DSH 网页界面：只开本机、关遥测、权限预设 workspace-write、从 `llama.key` 读密钥 |

测过的版本：DSH 0.2.0-rc.2（开发者预览，升级前先读它的升级说明）、Node 24、Windows 11 + Git Bash。

## 步骤

1. **装 DSH**（锁版本）：
   ```bash
   mkdir dsh-install && cd dsh-install && npm install @deepseek-ai/dsh@0.2.0-rc.2
   ```
   npm 会提示有几个包的安装脚本没跑。在 Windows 上查过都不需要（一个只在 macOS / Linux 用；node-pty 有现成的 win32-x64 预编译）
2. **建配置档**：设好 `DSH_HOME` 后跑一次 `dsh --profile web --dump-config`，DSH 会在 `$DSH_HOME/profiles/web/` 下生成配置档
3. **放补丁**：把 `dsh/cordis.patch.yml` 拷成 `$DSH_HOME/profiles/web/cordis.patch.yml`（覆盖生成的空补丁），再跑一次 `dsh --profile web --dump-config`，输出里应该能找到 `provider: local-llama`、`default: local32k`
4. **起模型**：`bash start-llama.sh`（约 15 秒）
5. **起 DSH**：改好 `dsh/start-dsh-web.sh` 开头的路径，`bash dsh/start-dsh-web.sh`，在浏览器打开它输出的 `http://127.0.0.1:3080/?token=…`
6. **工作区**：第一次用时，DSH 自己建 `文档/deepseek-harness/default-workspace`，跟从哪个目录启动无关。要用自己的目录，在左栏「工作区」旁边添加。最好用一个专用的空目录，别直接指向重要文件夹（原因见下面「安全」）

停：停掉 llama-server 和 DSH 两个进程。在 Git Bash 下停外层的 bash，不一定会连带停掉 node / llama-server，要按端口（DSH 3080、模型 8081）查到进程，核对命令行再停。

## 实测

2026-10-23 用本仓库的两个脚本和补丁原样搭一遍（新的空 `DSH_HOME`）：

- 界面里新会话默认就是「本地 32k」预设、`qwen3.6-35b-a3b` 模型
- 让它「用命令行查当前工作目录的完整路径和文件数」：1 轮 4 步、13 秒、61 token/秒，正确答出路径和文件数，结束时上下文用到 16%

预设的固定开销（2026-10-01 在 16k 窗口下量的，同一组 16 个工具）：原装 standard 预设每个新会话光系统提示词 + 工具说明 + 运行时上下文就约 7.4K token；精简后约 4.8K。

## 补丁里每一块为什么这么写

- **模型**：`providers` 下只放 `local-llama`。按 id 覆盖是整块替换，所以 DSH 默认带的 DeepSeek 在线模型会被拿掉；要同时用在线模型，就在 `providers` 下加回来。密钥用 `apiKeyEnv: LOCAL_LLAMA_KEY` 从环境变量读，不写进补丁
- **另建预设、不改 standard**：预设里的子行不能用补丁按 id 改（补丁只往 `group: true` 的分组里找），要改只能整份重抄。另建一份更干净，standard 原样留着，界面里随时能切回去对照。去掉的是子 Agent / workflow、长期目标、计划模式
- **压缩线**：原装按「留 65536 token 余量」算触发线，`floor(min(W×0.8, W−输出预留−65536))` 在 32k 窗口下是负数，DSH 只警告一次、从不主动压缩，长会话直接撑爆，而且日志和界面上都看不出来。补丁里只给本地模型配 `headroomTokens: 1024`，触发线 = 26214；摘要输出上限 1024
- **工具结果截断**：`thresholdChars` 从 8192 减到 4096（头 2048、尾 1024），长命令输出不至于一次吃掉一大块上下文
- **选目录**：本机 + Windows 时，自动选择器会选原生窗口（弹在桌面上）。补丁不能改已有条目的插件名，所以关掉自动选择器、另插 browse 的**两半**（本机服务 + 网页界面）；只插一半的话，「添加工作区」按钮会消失

## 踩过的坑

| 现象 | 原因 | 解法 |
|---|---|---|
| 补丁报 name mismatch | 补丁不能改已有条目的插件名 | 原条目 `disabled: true`，另 `insert` 一条 |
| 沙箱里的命令全失败：`SetNamedSecurityInfoW failed (Win32 5)` | Windows 沙箱要给工作区打「低完整性」标签，要求你本人对目录有**完全控制**；E 盘这类非系统盘根下新建的目录，往往只继承到「修改」 | 给工作区目录加上你自己账号的「完全控制」 |
| 长会话越来越慢、最后出错 | 压缩从不触发（见上） | 补丁里的 `modelPolicies` |
| Agent 填工具参数时复读、写满输出上限 | DSH 不传采样参数，用的是 llama.cpp 默认值 | `start-llama.sh` 里的采样 + DRY，见 `docs/params.md` |
| 补丁文件被改过 | 第一次点掉「预览版说明」时，DSH 会往 `cordis.patch.yml` 末尾写一条 `ui-settings-general`，还会重排注释的空格 | 正常现象，留着就行 |
| 所有请求都 401 | 密钥文件是 CRLF 行尾 | 用 LF；`start-llama.sh` 会拦 |

## 安全

- **权限预设 workspace-write**：沙箱里的命令只能写工作区，越界的（比如要 danger-full-access）先问你。测过：往工作区外写会被拒
- **沙箱会留下常驻标记**：工作区上会多出三条改动（能力 SID 写入授权、Everyone 拒绝删除子项、低完整性标签）。DSH 关了也还在，`icacls` 撤不掉。低完整性标签意味着同一账号下别的低完整性程序也能写这个目录，所以工作区只放 DSH 的产出
- **遥测**：`DSH_TELEMETRY_DISABLED=1` 关掉
- **联网**：`web_fetch` 不要密钥，能从本机抓任意网址；网页里的文字可能夹带指令。`web_search` 要 `DEEPSEEK_API_KEY`
- **插件以你的权限运行、不进沙箱**：别人的插件先审代码再装
