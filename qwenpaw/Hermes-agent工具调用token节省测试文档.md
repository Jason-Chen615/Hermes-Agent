# Hermes-agent 工具调用 Token 节省测试文档

## 背景与目标

这是把 `qwenpaw/` 下已有的「工具调用 token 节省」A/B 测试，**原样迁移到 Hermes-agent** 上跑一遍。
测试框架、测试用例（3 轮 arc）、指标口径、compare 逻辑全部保持与 qwenpaw 一致，**唯一变量是被测 agent 从 QwenPaw 换成 Hermes-agent**。

- **测的东西**：agent 的「工具结果上下文压缩/裁剪」特性开 vs 关，对 token 消耗的影响。
- **实验类型**：单变量 A/B。同一测试用例跑两遍，只翻一个开关，token 差值即节省量。
- **对齐原则**：3 轮设计（工具密集读文件 → 无工具分析 → 记忆召回）、输出模板、CSV 列、summary/compare 算法，与 qwenpaw 完全相同，便于横向对比两个 agent。

> qwenpaw 测的是 `tool_result_pruning_config.enabled`。Hermes 没有完全同名的开关，
> 最贴近的等价物是 **`compression.proactive_prune_tokens`**（同样是"裁剪旧工具结果 payload"的确定性、无 LLM 的裁剪）。本测试以它为 A/B 变量。

---

## 1. 框架对齐关系（qwenpaw → Hermes）

| 维度 | qwenpaw 原始 | Hermes 对应 | 是否需改 harness |
|---|---|---|---|
| 传输层 | `POST /api/console/chat` SSE | `POST /api/sessions/{id}/chat`（API server） | **是**（重写 run_single_turn） |
| 会话延续 | 同一 `session_id` 串 3 轮 | 先 `POST /api/sessions` 建会话，再对同一 `session_id` 发 3 轮 | 是 |
| token 来源 | SSE `response.usage` | 响应体 `usage.{input_tokens,output_tokens,total_tokens}` | 是 |
| A/B 变量 | `tool_result_pruning_config.enabled` false/true | `compression.proactive_prune_tokens` **0 / 48000** | 否（改配置，不改脚本） |
| 读文件工具 | `read_file` | `read_file`（完全同名） | 否 |
| 记忆检索工具 | `memory_search` | `session_search`（FTS5 检索历史会话） | 改 prompt 里的工具名 |
| 禁用工具 | `execute_shell_command` / `grep_search` / `glob_search` | `terminal` / `process` / `search_files` | 改 prompt 里的工具名 |
| agent 隔离 | 4 个 agent-id | 4 个 `--profile`（各自独立 config+memory+state） | 否 |
| 记忆重置 | `reset_memory.py`（ReMe Light 子目录） | `hermes memory reset --yes` + 清 `state.db`/`sessions.json` | **是**（reset 脚本重写） |
| 测试语料 | kubernetes 源码，`{{WORKSPACE_ROOT}}` 占位 | 同语料，路径不同 → 运行时传 `--workspace-root` | 否（占位符机制不变） |

**可原样复用、无需改动的部分**：`compute_summary`、`percentile`、CSV writer、`compare_reports`、`{{WORKSPACE_ROOT}}` 替换、>30% prompt 掉落的 compaction 启发式——这些都是 agent 无关的通用逻辑。

---

## 2. A/B 变量定义

配置文件：`<HERMES_HOME>/config.yaml` 的 `compression` 块。

- **base（关）**：`compression.proactive_prune_tokens: 0`
- **cand（开）**：`compression.proactive_prune_tokens: 48000`

其余 `compression.*` 与 `memory.*` 全部两臂保持**完全一致**（尤其 `compression.enabled: true`、`threshold`、`protect_last_n`、`target_ratio` 固定），确保 token 差值只归因于工具结果裁剪这一个特性。

> 为什么用 48000：Hermes 文档建议值。含义是"重发历史里的工具结果超过 4.8 万 token 时，去重/摘要/截断旧的大工具结果，保护最近 N 条"。这正对应 qwenpaw 里的 tool_result_pruning 场景。

---

## 3. 4-agent 隔离设计

记忆/会话按 profile 隔离，每个（测试用例 × 配置）用独立 profile，避免串扰——与 qwenpaw 的 4-agent 设计一一对应：

| profile 名 | 测试用例 | proactive_prune_tokens |
|---|---|---|
| `hermes-t1-base` | test1 | 0（关） |
| `hermes-t1-cand` | test1 | 48000（开） |
| `hermes-t2-base` | test2 | 0（关） |
| `hermes-t2-cand` | test2 | 48000（开） |

每个 profile 是独立的 `HERMES_HOME`（`~/.hermes/profiles/<name>/`），自带 `config.yaml`、`memories/`、`state.db`。

**建 profile 并设置 A/B 变量**（每个 profile 做一次）：

```bash
# 以 t1-base 为例，其余三个同理，只是 prune 值不同
hermes --profile hermes-t1-base setup     # 首次：填同一个 OPENAI_API_KEY、选同一个模型
# 然后编辑该 profile 的 config.yaml，设 compression.proactive_prune_tokens
#   base 两个 → 0 ；cand 两个 → 48000
$EDITOR ~/.hermes/profiles/hermes-t1-base/config.yaml
```

> 关键：4 个 profile 的模型、provider、其余 compression/memory 配置必须**逐字相同**，只有 prune 值不同。
> 建议先配好 base，再 `cp -r` 复制成其它三个，然后只改 cand 两个的 prune 值——最省事且最不易引入无关差异。

---

## 4. 启动 API Server

Hermes 的 harness 走 HTTP，需要先开 API server（默认关闭）。在**每个 profile** 的 `.env` 里开启：

```dotenv
# ~/.hermes/profiles/<name>/.env
API_SERVER_ENABLED=true
API_SERVER_KEY=<强密钥，≥16 位，四个 profile 可用同一个>
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642        # 四个 profile 若同时跑，需各分配不同端口
```

启动（每个被测 profile 起一个 gateway）：

```bash
API_SERVER_PORT=8642 hermes --profile hermes-t1-base gateway &
API_SERVER_PORT=8643 hermes --profile hermes-t1-cand gateway &
# t2 两个同理，端口 8644 / 8645
```

> 安全须知：API server 必须配 `API_SERVER_KEY`（即使只绑 127.0.0.1，无 key 拒绝启动）。
> **不要**把 `API_SERVER_HOST` 设成 `0.0.0.0` 暴露到公网。远程访问用 SSH 隧道。

---

## 5. Harness 改造点（run_hermes_bench.py）

在 qwenpaw 的 `run_sccs_bench.py` 基础上改，只动传输层，其余照搬。

### 5a. 建会话 + 每轮请求

```python
# 建持久会话（每个 run 开始时一次）
# POST /api/sessions  →  拿 session_id
resp = httpx.post(f"{base_url}/api/sessions",
                  headers={"Authorization": f"Bearer {api_key}"},
                  json={"title": label})
session_id = resp.json()["session_id"]

# 每轮（3 轮串在同一 session_id 上，上下文/记忆累积）
r = httpx.post(f"{base_url}/api/sessions/{session_id}/chat",
               headers={"Authorization": f"Bearer {api_key}"},
               json={"input": prompt_text}, timeout=timeout_sec)
body = r.json()
```

### 5b. token 提取（替换 qwenpaw 的 SSE usage 解析）

```python
usage = body.get("usage") or {}
prompt_tokens     = usage.get("input_tokens")  or 0   # 对应 qwenpaw 的 prompt_tokens
completion_tokens = usage.get("output_tokens") or 0   # 对应 completion_tokens
total_tokens      = usage.get("total_tokens")  or 0
```

> 口径说明：Hermes 每个 HTTP 请求新建 agent 实例，`usage` 的三个 `session_*` 累加器只覆盖本轮（含本轮工具循环里的所有 LLM 调用），因此**天然就是每轮增量**，与 qwenpaw 的 per-turn usage 语义一致。历史每轮重放进模型，`input_tokens` 会随轮次增长——这正是裁剪特性要压的量。

### 5c. 回复文本提取

`/api/sessions/{id}/chat` 返回结构化响应，取其回复文本字段（首次跑打印一次完整 body 确认字段名，通常是 `output` / `content` / `message`）。compaction 启发式（本轮 prompt_tokens < 上轮 ×0.7 记一次）与 CSV 列、summary、compare **原样保留**。

### 5d. 保留不变

`compute_summary` / `percentile` / CSV writer（列：`turn,turnId,status,durationMs,promptTokens,completionTokens,totalTokens,estimatedContextTokens,contextUsageRatio,compactionCountDelta,replyChars,error`）/ `compare_reports`（7 个 dotted-path 指标算 diff/diffPercent）/ `{{WORKSPACE_ROOT}}` 替换——全部照搬 qwenpaw。`estimatedContextTokens`/`contextUsageRatio` 在此路径仍为 0（HTTP usage 不含这两项），与 qwenpaw 一致。

---

## 6. 测试用例改造（test1.json / test2.json）

3 轮 arc、输出模板、"严格调用 read_file N 次"约束**全部保留**，只改工具名：

- `memory_search` → **`session_search`**（第 3 轮记忆召回轮）
- 禁用工具表述里的 `execute_shell_command` / `grep_search` / `glob_search` → **`terminal`** / **`search_files`**（Hermes 把 grep/glob 合并进 `search_files`，shell 是 `terminal`）
- `read_file` 不变
- 文件路径用 `{{WORKSPACE_ROOT}}` 占位，运行时 `--workspace-root <你服务器上的 k8s 源码根>` 传入（你已确认语料存在但路径不同）

建议直接 `cp test1.json test1.hermes.json` 再改这几处，保留原文件供两个 agent 结果对照。

---

## 7. 运行流程

每个 profile：**重置记忆一次 → 跑 3 轮 → 两臂都跑完后 compare**。重置只在每个 run 前做一次，**绝不在轮次之间做**（第 3 轮依赖前两轮的上下文/记忆）。

### 7a. 记忆重置（替换 reset_memory.py）

Hermes 用内置命令，不用 qwenpaw 的 ReMe Light 脚本：

```bash
hermes --profile hermes-t1-base memory reset --yes    # 清 memories/MEMORY.md + USER.md
# 彻底隔离还需清会话历史（session_search 的 FTS 数据源）：
rm -f ~/.hermes/profiles/hermes-t1-base/state.db
rm -f ~/.hermes/profiles/hermes-t1-base/sessions/sessions.json
```

### 7b. 单个 run

```bash
python run_hermes_bench.py run \
  --prompts test1.hermes.json --label test1-base \
  --session-title "bench-t1-base" \
  --out-dir bench-results/t1-base \
  --base-url http://127.0.0.1:8642 \
  --api-key "$API_SERVER_KEY" \
  --workspace-root /path/to/kubernetes \
  --timeout-sec 600
```

### 7c. 完整 4 run + 2 compare

```bash
# test1
hermes --profile hermes-t1-base memory reset --yes && rm -f ~/.hermes/profiles/hermes-t1-base/state.db
python run_hermes_bench.py run --prompts test1.hermes.json --label test1-base \
  --out-dir bench-results/t1-base --base-url http://127.0.0.1:8642 --api-key "$KEY" --workspace-root /path/to/k8s
hermes --profile hermes-t1-cand memory reset --yes && rm -f ~/.hermes/profiles/hermes-t1-cand/state.db
python run_hermes_bench.py run --prompts test1.hermes.json --label test1-cand \
  --out-dir bench-results/t1-cand --base-url http://127.0.0.1:8643 --api-key "$KEY" --workspace-root /path/to/k8s
python run_hermes_bench.py compare \
  --baseline bench-results/t1-base/test1-base-*.json \
  --candidate bench-results/t1-cand/test1-cand-*.json

# test2 同理（端口 8644 / 8645，语料同一 k8s 根）
```

---

## 8. 指标与结论

沿用 qwenpaw 的四张结果表：

- **表1 逐轮指标**：每轮 prompt/completion/total tokens、durationMs、replyChars、compactionCountDelta。
- **表2 单 run 汇总**：totals + averages + 延迟 p50/p90/max + compactionTriggeredTurns。
- **表3 A/B compare（核心结论）**：base vs cand 的 `totalTokens`/`promptTokens`/`durationMs`/延迟/compaction 的 diff 与 diffPercent。
- **表4 记忆召回验证（可选）**：第 3 轮 `session_search` 是否召回前两轮证据；重置后是否返回空。

**预期结论**：裁剪开启（cand）后，`totalTokens` / `promptTokens` 应**下降**（负 diff% = 节省）；`durationMs` 可能略升（压缩开销），属可接受权衡。两个测试用例的结论再与 qwenpaw 同用例结果横向对比，即得"Hermes vs QwenPaw 工具调用 token 节省"对照。

---

## 9. 交付物清单

- `run_hermes_bench.py` — 基于 `run_sccs_bench.py` 改传输层（§5）
- `test1.hermes.json` / `test2.hermes.json` — 改工具名后的用例（§6）
- 4 个 profile：`hermes-t1-base/cand`、`hermes-t2-base/cand`（§3）
- `bench-results/` — 各 run 的 JSON+CSV 与 compare 输出
- 结论表 1–4（§8）

## 10. 待你确认/提供

1. **k8s 源码根路径**：`--workspace-root` 要传的绝对路径（你已确认语料在，只是路径不同）。
2. **模型**：4 个 profile 统一用哪个模型/provider（须与 qwenpaw 那次尽量可比）。
3. **`run_hermes_bench.py` 我是否直接写出来**——本文档只定方案，脚本落地是下一步。
