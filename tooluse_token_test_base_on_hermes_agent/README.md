# Hermes-agent 工具调用 Token 节省测试（方案 B）

把 `qwenpaw/` 的「工具调用 token 节省」A/B 测试迁移到 Hermes-agent。框架、3 轮用例、
指标口径、compare 逻辑全部与 qwenpaw 对齐，**唯一变量**是被测 agent 换成 Hermes-agent。

- **A/B 变量**：`compression.proactive_prune_tokens` ——`0`（base，关）vs `48000`（cand，开）。
- **方案 B**：复用你已配好的**默认 profile**（`~/.hermes`），串行跑 4 个 run，靠「每 run 前重置记忆 + 每组 A/B 间只改这一个配置值」保证隔离。不建新 profile，模型/key 零额外配置。

> 脚本本身**不碰 LLM 配置**，只通过 HTTP 跟 Hermes API server 通信，只需 `--base-url` 和 `--api-key`。
> 模型/provider/key 都由 Hermes gateway 进程读 `~/.hermes` 里的配置，脚本无感知。

## 目录内容

- `run_hermes_bench.py` — 压测器（`run` / `compare` 两个子命令）
- `test1.json` / `test2.json` — 3 轮用例（工具名已改为 Hermes 的 `read_file` / `session_search` / 禁用 `terminal`·`search_files`）
- `requirements.txt` — 仅依赖 `httpx`
- `bench-results/` — 运行时生成的 JSON+CSV 报告

## 前置

- Python 3.8+，`pip install -r requirements.txt`（或 `uv pip install httpx`）
- k8s 源码在 `/root/test_code/kubernetes`；`--workspace-root` 传其**父目录** `/root/test_code`
  （prompt 里路径是 `{{WORKSPACE_ROOT}}/kubernetes/...`）。
- 默认 profile 已配好模型、能正常 `hermes` 聊天。

## 一、开启 API server（默认 profile，一次性）

Hermes API server 默认关闭，harness 走 HTTP，需先开。编辑 `~/.hermes/.env`：

```dotenv
API_SERVER_ENABLED=true
API_SERVER_KEY=<强密钥，≥16 位>
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
```

> 安全须知：必须配 `API_SERVER_KEY`（无 key 拒绝启动）。**不要**设 `API_SERVER_HOST=0.0.0.0`
> 暴露到公网/局域网——它能读写 API key。远程访问用 SSH 隧道 `ssh -L 8642:localhost:8642`。

把 key 导出给脚本用（或每条命令带 `--api-key`）：

```bash
export API_SERVER_KEY=<刚才那串>
```

## 二、A/B 变量：改一个值 + 重启 gateway

A/B 靠改 `~/.hermes/config.yaml` 的 `compression.proactive_prune_tokens` 实现：

- **base 阶段**：`proactive_prune_tokens: 0`
- **cand 阶段**：`proactive_prune_tokens: 48000`

其余 `compression.*`（尤其 `enabled: true`、`threshold`、`protect_last_n`）与 `memory.*` **保持不动**。
每改一次这个值，**必须重启 gateway** 让配置生效：

```bash
# 前台起（调试用，Ctrl+C 停）：
hermes gateway
# 或后台常驻：
mkdir -p ~/.hermes/logs
nohup hermes gateway > ~/.hermes/logs/gateway.log 2>&1 &
```

## 三、记忆重置（每个 run 前一次，绝不在轮次之间）

第 3 轮依赖前两轮沉淀的记忆/上下文，所以重置只在**每个 run 开始前**做一次：

```bash
hermes memory reset --yes                          # 清 MEMORY.md + USER.md
rm -f ~/.hermes/state.db                            # 清会话历史（session_search 的 FTS 数据源）
rm -f ~/.hermes/sessions/sessions.json             # 清会话索引
```

> 重置后需重启一次 gateway 再跑（确保它不持有旧 state.db 句柄）。

## 四、完整运行流程（4 run + 2 compare）

顺序：**test1-base → test1-cand → test2-base → test2-cand**。每组 base/cand 之间改一次
`proactive_prune_tokens` 并重启 gateway。

```bash
WS=/root/test_code            # kubernetes 的父目录
BASE=http://127.0.0.1:8642
MODEL=GLM-4.7-W8A8            # 必须与 config.yaml 的 model.default 一致，见「模型名坑」

# ── test1-base（先把 config.yaml 的 proactive_prune_tokens 设为 0，重启 gateway）──
hermes memory reset --yes && rm -f ~/.hermes/state.db ~/.hermes/sessions/sessions.json
# （重启 gateway）
python run_hermes_bench.py run --prompts test1.json --label test1-base --model $MODEL \
  --out-dir bench-results/t1-base --base-url $BASE --workspace-root $WS

# ── test1-cand（把 proactive_prune_tokens 改为 48000，重启 gateway）──
hermes memory reset --yes && rm -f ~/.hermes/state.db ~/.hermes/sessions/sessions.json
# （重启 gateway）
python run_hermes_bench.py run --prompts test1.json --label test1-cand --model $MODEL \
  --out-dir bench-results/t1-cand --base-url $BASE --workspace-root $WS

python run_hermes_bench.py compare \
  --baseline bench-results/t1-base/test1-base-*.json \
  --candidate bench-results/t1-cand/test1-cand-*.json

# ── test2 同理：base 阶段 prune=0，cand 阶段 prune=48000，各自 reset+重启，都带 --model $MODEL ──
# python run_hermes_bench.py run --prompts test2.json --label test2-base --model $MODEL ...
# python run_hermes_bench.py run --prompts test2.json --label test2-cand --model $MODEL ...
# python run_hermes_bench.py compare --baseline .../test2-base-*.json --candidate .../test2-cand-*.json
```

> `compare` 用了 shell 通配符 `*.json`；若目录里有多个报告，改成具体文件名。

### ⚠️ 模型名坑（必读）——`--model` 为什么是必填

Hermes API server 对外广告一个**虚拟模型名 `hermes-agent`**（`GET /v1/models` 里那个），
它不是真实模型。`/api/sessions` 这条路径有个 bug：创建 session 时若不指定 model，会把
`hermes-agent` 存进 session，之后每轮 `/chat` 把它当**真实模型名**发给 vLLM，导致
`404 The model hermes-agent does not exist`，且该错误被静默塞进回复、token 全记 0。

规避：**创建 session 时（即本脚本的 `--model`）传真实模型名**（`GLM-4.7-W8A8`，须与
`config.yaml` 的 `model.default` 一致）。只在 `/chat` body 里传没用——存进 session 的那个优先级更高。

脚本已内置保护：若某轮 `totalTokens == 0`，会打印 `WARN 0 tokens ...` 并回显那段回复，
方便你第一时间发现模型名/鉴权类静默失败，而不是收集到一堆全 0 的假数据。

## 五、指标与结论

报告（JSON+CSV）逐轮记录：`promptTokens`（上下文大小，关键）、`completionTokens`、`totalTokens`、
`durationMs`、`replyChars`、`compactionCountDelta`。`compare` 输出 7 个指标的 base vs cand
diff / diffPercent。

**预期**：cand（裁剪开）的 `totalTokens` / `promptTokens` 应**下降**（负 diff% = 节省），
`durationMs` 可能略升（压缩开销）。两个用例的结论再与 qwenpaw 同用例横向对比，即得
「Hermes vs QwenPaw 工具调用 token 节省」对照。

## 六、口径说明（与 qwenpaw 的一致性）

- **token 来源**：`usage.input_tokens/output_tokens/total_tokens`。Hermes 每个 HTTP 请求新建 agent 实例，
  usage 累加器只覆盖本轮，**天然是每轮增量**，与 qwenpaw 的 per-turn usage 语义一致。
- **estimatedContextTokens / contextUsageRatio**：此 HTTP 路径不提供，恒为 0（与 qwenpaw 的
  `/console/chat` 路径一致），仅为保持 CSV 列一致而保留。
- **compaction 启发式**：本轮 promptTokens < 上轮 ×0.7 记一次，与 qwenpaw 完全相同。
- **回复文本字段**：`/api/sessions/{id}/chat` 的回复在 `message.content`，脚本已优先读它。
  若某版本字段名不同导致 `replyChars` 恒为 0，打印一次完整响应 body 确认字段，补进
  `run_hermes_bench.py` 的 `extract_reply_text`。token 指标不受此影响。

