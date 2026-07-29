# Hermes Agent — Linux 服务器部署与使用文档

本文档面向：已把 Hermes Agent 仓库 `git clone` 到 Linux 服务器、想按步骤跑起来的场景。

采用方案：

- **部署方式**：源码原生安装（用已 clone 的代码 + `uv` 建虚拟环境，不走 Docker）
- **使用形态**：终端交互（CLI/TUI）+ Web 看板（Dashboard，默认 `127.0.0.1:9119`）
- **模型来源**：OpenAI / 其他直连（在 `.env` 里填对应 API key）

Hermes 是 Python 3.11–3.13 项目，用 `uv` 管理依赖，主命令是 `hermes`。仓库自带
`setup-hermes.sh`——专为“手动 clone 的开发者/服务器”准备的一键安装脚本，是本文档的主线工具。

---

## 0. 前置条件（系统级依赖）

Debian/Ubuntu 服务器上先装好这些系统包（`uv` 会自动装 Python，无需手动装）：

```bash
sudo apt update
sudo apt install -y git curl xz-utils build-essential python3-dev libffi-dev ffmpeg ripgrep
```

- `git curl xz-utils`：clone、下载 uv/Node、解压
- `build-essential python3-dev libffi-dev`：编译个别 Python 原生轮子（如 STT）
- `ffmpeg`：语音转写 / TTS 音频解码（不用语音可省，装上无害）
- `ripgrep`：agent 的快速文件搜索（缺了会退化成 grep，建议装）

其它运行时（`uv`、Python 3.11、Node 22）由 `setup-hermes.sh` 自动安装。
CentOS/RHEL 系把 `apt install` 换成 `dnf install`（包名：`gcc gcc-c++ make python3-devel libffi-devel ffmpeg ripgrep`）。

---

## 1. 运行安装脚本

进入你 clone 的目录，执行仓库自带脚本：

```bash
cd /path/to/hermes-agent      # 你的 clone 路径
./setup-hermes.sh
```

`setup-hermes.sh` 会自动完成：

1. 安装 / 定位 `uv`（Astral 的 Python 包管理器）
2. 用 uv provision Python 3.11 并在 `./venv` 建虚拟环境
3. `uv sync --extra all --locked`（按 `uv.lock` 做哈希校验安装，首次 1–5 分钟）
4. 从 `.env.example` 复制出 `.env`（权限 chmod 600）
5. 把 `hermes` 命令软链到 `~/.local/bin/hermes`，并把 `~/.local/bin` 写进 shell 配置
6. 末尾询问是否跑 setup 向导——**可先跳过**，下一节手动配 key

脚本结束后刷新 PATH：

```bash
source ~/.bashrc     # 或 ~/.zshrc
hermes --help        # 验证命令可用
```

> 注意：脚本把 `venv/` 建在源码目录内。长跑 gateway/dashboard 没问题；但若之后让 agent
> 在它自己的 checkout 目录里跑破坏性命令，可能误删 venv。担心的话见文末“备选”。

---

## 2. 配置模型 API Key（OpenAI / 直连）

配置文件在 **`~/.hermes/`** 下：`.env`（密钥）和 `config.yaml`（模型/行为）。

### 2a. 填 API Key

```bash
nano ~/.hermes/.env
```

按你的直连服务商填其中一项（取消注释并填值）：

```dotenv
# OpenAI 直连（注意：语音/TTS 用的是单独的 VOICE_TOOLS_OPENAI_KEY）
OPENAI_API_KEY=sk-...

# 或 Google Gemini
# GOOGLE_API_KEY=...

# 或 DeepInfra / Fireworks / 其它 OpenAI 兼容端点
# DEEPINFRA_API_KEY=...
# FIREWORKS_API_KEY=...
```

`.env` 里每个 provider 上方的注释都写了申请地址和可选的 `*_BASE_URL` 覆盖项（自建/代理端点时用）。

### 2b. 选模型 / 确认 provider

模型默认值存在 `~/.hermes/config.yaml` 的 `model.default`（不再从 `.env` 读）：

```bash
hermes setup      # 交互式全量向导：选 provider、填 key、选模型（推荐首次用）
# 或：
hermes model      # 只选模型 / provider
```

### 2c. 自检

```bash
hermes doctor     # 诊断配置与依赖是否齐全（检查 key、Node、browser 等）
```

---

## 3. 启动方式

### 3a. 终端交互（CLI / TUI）

```bash
hermes            # 进入交互式 TUI，开始对话
```

常用会话内命令：`/new` 或 `/reset` 开新对话，`/model` 换模型，`/compress` 压缩上下文，`/skills` 看技能，`Ctrl+C` 打断。

### 3b. Web 看板（Dashboard）

```bash
hermes dashboard --host 127.0.0.1
```

- 默认端口 **9119**，默认只绑 `127.0.0.1`（本机回环）。
- **安全须知**：Dashboard 能读写 API key，**不要**直接 `--host 0.0.0.0` 暴露到公网/局域网。
  远程访问用 SSH 隧道从本地机器打通：

  ```bash
  # 在你的本地机器上执行：
  ssh -L 9119:localhost:9119 <user>@<server>
  # 然后本地浏览器打开 http://localhost:9119
  ```

  确需公开时，需配 Dashboard 自带的认证 provider（密码 / OAuth）并放到带认证的反向代理后面。

### 让服务在后台长跑

```bash
nohup hermes dashboard --host 127.0.0.1 --no-open > ~/.hermes/logs/dashboard.log 2>&1 &
```

（若之后要接 Telegram/Discord 等消息网关常驻，可用 `hermes gateway install` 装成 systemd 服务——本次用不到。）

---

## 4. 验证清单（端到端）

1. `hermes --help` — 命令已在 PATH 上
2. `hermes doctor` — 配置/依赖无报错（key 已识别）
3. `hermes -q "你好，用一句话介绍你自己"` — 能连上模型并返回（验证 LLM 链路）
4. `hermes dashboard --host 127.0.0.1` → SSH 隧道后本地浏览器打开 `http://localhost:9119` 能看到界面
5. 进 `hermes` TUI 发一句话，确认对话正常

任一步失败，先跑 `hermes doctor` 看诊断，再对照下面排错。

---

## 5. 常见问题排错

- **`hermes: command not found`**：`source ~/.bashrc`；或确认 `~/.local/bin` 在 PATH：`echo $PATH | tr ':' '\n' | grep local/bin`。
- **模型报鉴权 / 401**：检查 `~/.hermes/.env` 里 key 是否填对、是否被注释；`hermes model` 确认选的 provider 与填的 key 一致。
- **依赖装到一半失败**：多为缺编译工具，装好第 0 节的包后，在 clone 目录内激活 venv 重跑 `uv sync --extra all --locked`。
- **想用浏览器工具**（agent 上网）：需要 Node + Playwright Chromium。`setup-hermes.sh` 不装浏览器；单独跑 `hermes-acp --setup-browser` 或 `hermes tools post-setup agent_browser` 安装（约 400MB）。不用可忽略。
- **语音转写**：默认本地 `faster-whisper`，首次用自动下模型（~150MB），需 `ffmpeg`。不用语音可忽略。

---

## 备选：把 venv 建到源码树外（更安全的长跑方式）

若担心 agent 误删自己的 venv，按 README 推荐手动装（不使用 `setup-hermes.sh` 的树内 venv）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv ~/.hermes/venvs/hermes --python 3.11
source ~/.hermes/venvs/hermes/bin/activate
cd /path/to/hermes-agent
uv pip install -e ".[all]"
```

之后 `hermes setup` / `hermes dashboard` 用法与上文一致。

---

## 关键文件参考

- `setup-hermes.sh` — 源码安装主脚本（建 venv、装依赖、软链 hermes）
- `.env.example` — 环境变量/密钥模板（复制成 `~/.hermes/.env`）
- `cli-config.yaml.example` — 完整 config.yaml 参考
- `pyproject.toml` — Python 依赖与版本约束（`>=3.11,<3.14`）、`hermes` 入口
- `README.md` — 官方安装与命令速查
