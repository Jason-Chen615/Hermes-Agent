# QwenPaw Tool Result Compression Bench (Python)

这是原 `../run-sccs-bench.mjs`（OpenClaw + OpenViking）的 **QwenPaw 迁移版**：
Agent 换成 QwenPaw、记忆系统换成 QwenPaw 原生 ReMe Light、脚本语言换成 Python。

当前仅迁移 **test1 / test2**（kubernetes 相关用例）。

## 与原版的差异

| 维度 | 原版 (mjs) | 本版 (Python) |
| --- | --- | --- |
| Agent | OpenClaw (`openclaw agent --json` 子进程) | QwenPaw (HTTP SSE `POST /api/console/chat`) |
| 记忆 | OpenViking 实例 | QwenPaw 原生 ReMe Light（agent workspace 内） |
| token 指标 | stdout JSON `agentMeta.usage` | SSE `turn_usage` 事件 |
| 工具名 | `read` / `exec` / `rg` | `read_file` / `execute_shell_command` / `grep_search` |
| 记忆检索 | OpenViking 插件 | 原生 `memory_search` |
| 路径 | 硬编码 `/root/.openclaw/workspace/...` | `{{WORKSPACE_ROOT}}` 占位符，运行时解析 |

## 前置依赖

1. **Python 依赖**：
   ```bash
   pip install -r requirements.txt
   ```

2. **QwenPaw 服务已启动**：
   ```bash
   qwenpaw app --host 127.0.0.1 --port 8088 --log-level info
   # 确认就绪：
   curl http://127.0.0.1:8088/api/version
   ```

3. **认证配置（若启用）**：
   如果 QwenPaw 设置了 `QWENPAW_AUTH_ENABLED=true`，则需提供认证凭据。三种方式任选其一：

   - **环境变量（推荐）**：
     ```bash
     export QWENPAW_USERNAME=your_username
     export QWENPAW_PASSWORD=your_password
     ```
   
   - **命令行参数**：
     ```bash
     python run_sccs_bench.py run ... --username your_username --password your_password
     ```
   
   - **预先获取 token**（通过 Web 登录或 `/api/auth/login` 接口）：
     ```bash
     export QWENPAW_AUTH_TOKEN=your_bearer_token
     # 或命令行: --auth-token your_bearer_token
     ```

   **未启用认证时**无需任何凭据，脚本自动以无认证模式运行。

4. **kubernetes 仓库**放在 workspace 根目录下，且脚本能定位到它。用例只用到：
   - `kubernetes/pkg/kubelet/kubelet.go`
   - `kubernetes/pkg/scheduler/schedule_one.go`
   - `kubernetes/staging/src/k8s.io/apiserver/pkg/endpoints/handlers/create.go`
   - `kubernetes/staging/src/k8s.io/apimachinery/pkg/util/version/version.go`
   - `kubernetes/pkg/kubelet/pod_workers.go`
   - `kubernetes/pkg/kubelet/status/status_manager.go`
   - `kubernetes/pkg/kubelet/pleg/generic.go`

4. **（可选）混合检索 embedding**：要让 `memory_search` 走 向量+BM25 混合检索，
   在 agent 的 `agent.json`（`<WORKING_DIR>/workspaces/<agent>/agent.json`）里配置
   `running.reme_light_memory_config.embedding_model_config`
   （`backend / api_key / base_url / model_name / dimensions`）。
   未配置则自动退化为纯 BM25 关键词检索。

## 路径解析

用例 JSON 里用 `{{WORKSPACE_ROOT}}` 占位符表示 workspace 根，脚本运行时替换为绝对路径。
根目录解析优先级：

1. `--workspace-root` 参数
2. `BENCH_WORKSPACE_ROOT` 环境变量
3. 相对本脚本推断（`.../.openclaw/workspace`）

设置示例：
```bash
export BENCH_WORKSPACE_ROOT=/root/.openclaw/workspace   # Linux 服务器
# 或 Windows:
# set BENCH_WORKSPACE_ROOT=D:\...\.openclaw\workspace
```

## 运行单个用例

在本目录（`qwenpaw/`）执行：

```bash
python run_sccs_bench.py run \
  --prompts test1.json \
  --label test1 \
  --session-id "bench-qp-$(date +%s)-test1" \
  --timeout-sec 600 \
  --out-dir "bench-results/test1-qp" \
  --base-url http://127.0.0.1:8088 \
  --agent default
```

将 `test1.json` 替换为 `test2.json` 即可跑第二个用例。

## 用例间重置记忆

每个用例开始前重置 QwenPaw 原生记忆，保证干净起点。`reset_memory.py` 有两种模式：

### docker 模式（QwenPaw 跑在容器里，推荐）

QwenPaw 容器把记忆放在数据卷（如 `qwenpaw-data → /app/working`），host 上不便直接删，
用 `--docker <容器名>`，脚本 `docker exec` 进容器删：

```bash
# 先预览会删除哪些目录（不真删）
python reset_memory.py --agent default --docker qwenpaw --dry-run
# 实际删除
python reset_memory.py --agent default --docker qwenpaw
```

删除的是容器内 `/app/working/workspaces/<agent>/` 下的记忆子目录。若容器内 WORKING_DIR
不是 `/app/working`，用 `--container-working-dir` 覆盖。

### host 模式（本机能直接访问 WORKING_DIR 时）

```bash
python reset_memory.py --agent default --dry-run
python reset_memory.py --agent default
# 或指向 docker 数据卷 host 路径（需 root）：
python reset_memory.py --agent default \
  --working-dir /var/lib/docker/volumes/qwenpaw-data/_data
```

两种模式都删除 ReMe Light 记忆子目录
（`memory/ digest/ mem_metadata/ mem_session/ mem_agent/ resource/`，会读 agent.json 覆盖默认名），
且**只删除 agent workspace 之内**的目录。

`WORKING_DIR`（host 模式）解析：`--working-dir` > `QWENPAW_WORKING_DIR` 环境变量 > `~/.copaw`（存在时）> `~/.qwenpaw`。

## 批量运行示例

```bash
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

export BENCH_WORKSPACE_ROOT="${BENCH_WORKSPACE_ROOT:-/root/.openclaw/workspace}"
# 若启用认证：脚本自动读取以下环境变量，无需在命令行重复传
# export QWENPAW_USERNAME=your_username
# export QWENPAW_PASSWORD=your_password
AGENT="default"
DOCKER_CONTAINER="qwenpaw"   # QwenPaw 容器名；若在 host 直跑，删掉下面 --docker 参数
OUT_ROOT="bench-results"

run_case() {
  local name="$1"
  python reset_memory.py --agent "$AGENT" --docker "$DOCKER_CONTAINER"
  python run_sccs_bench.py run \
    --prompts "${name}.json" \
    --label "$name" \
    --session-id "bench-qp-$(date +%s)-${name}" \
    --timeout-sec 600 \
    --out-dir "${OUT_ROOT}/${name}-qp" \
    --agent "$AGENT"
}

run_case test1
run_case test2
```

## 对比两次报告

```bash
python run_sccs_bench.py compare \
  --baseline bench-results/test1-qp/test1-<stamp>.json \
  --candidate bench-results/test1-qp/test1-<stamp2>.json
```

## 输出

每次运行在 `--out-dir` 下生成 `<label>-<stamp>.json` 和 `.csv`：

- 逐轮：`promptTokens / completionTokens / totalTokens / estimatedContextTokens /
  contextUsageRatio / durationMs / replyChars / status / error`
- 汇总：totals、averages、latency p50/p90/max、compactionTriggeredTurns

> **token 来源**：`/api/console/chat` 在**最后一帧** `{"object":"response",
> "status":"completed"}` 的 `usage` 里给出 `prompt_tokens / completion_tokens /
> total_tokens`（脚本从这帧取，不依赖 `turn_usage` 事件——该 HTTP 路径不发此事件）。
> reply 文本从同一帧的 `output` 里向后找 `type=="message"` 的消息提取（跳过
> reasoning / function_call）。
>
> **本路径拿不到的指标**：`estimatedContextTokens` 与 `contextUsageRatio` 只在
> `turn_usage` 事件里才有，`/console/chat` 不提供，故这两列恒为 0。
> `compactionTriggeredTurns` 改用 **`promptTokens` 相对上一轮骤降 >30%** 近似判定
> （工具结果压缩的可观测量就是上下文/prompt token 的下降）。

`bench-results-*` 是本地运行产物，不需要提交。

## 校验记忆链路是否生效

1. 跑 test1 的前两轮（ingest + 设计）后，第三轮 `memory_search` 若能召回模块/设计要点，
   说明原生记忆写入+检索链路正常。
2. 运行 `reset_memory.py` 后重跑第三轮，`memory_search` 应召回为空，说明重置生效。
