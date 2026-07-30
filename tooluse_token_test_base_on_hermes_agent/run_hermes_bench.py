#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hermes-agent tool-use token-saving bench.

Port of qwenpaw/run_sccs_bench.py to Hermes-agent. Same 3-turn arc, same
per-turn token/context metrics, same JSON/CSV report shape and compare
logic — only the transport layer and the A/B knob differ:

  * transport : POST /api/sessions  (create one persistent session)
                POST /api/sessions/{id}/chat  (one turn each, usage per turn)
  * tokens    : usage.input_tokens / output_tokens / total_tokens
  * A/B knob  : compression.proactive_prune_tokens  (0 = base, 48000 = cand)
                — set in <HERMES_HOME>/config.yaml, NOT here. This script is
                LLM-config agnostic: it only needs --base-url and --api-key.

Method B (default profile, serial runs). See README.md.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
DEFAULT_BASE_URL = "http://127.0.0.1:8642"   # Hermes API server default port
DEFAULT_TIMEOUT_SEC = 600


# ──────────────────────────────────────────────────────────────────────
# Path resolution
# ──────────────────────────────────────────────────────────────────────
def resolve_workspace_root(explicit: Optional[str] = None) -> Path:
    """Resolve workspace root from --workspace-root or BENCH_WORKSPACE_ROOT.

    Prompts reference files as ``{{WORKSPACE_ROOT}}/kubernetes/...`` so the
    workspace root is the PARENT of the kubernetes checkout. On the target
    server that is ``/root/test_code`` (kubernetes + hermes-agent are
    siblings under it). Fallback walks up from this script's location and
    accepts the first ancestor that actually contains ``kubernetes/``.
    """
    if explicit:
        return Path(explicit).resolve()
    env_val = os.environ.get("BENCH_WORKSPACE_ROOT")
    if env_val:
        return Path(env_val).resolve()
    # Fallback: script lives at <root>/hermes-agent/tooluse_.../run_hermes_bench.py
    # so <root> is three levels up. Verify it holds a kubernetes/ tree.
    script_dir = Path(__file__).resolve().parent
    for candidate in (script_dir.parent.parent, script_dir.parent.parent.parent):
        if (candidate / "kubernetes").exists():
            return candidate.resolve()
    raise ValueError(
        "Cannot resolve workspace root. Set BENCH_WORKSPACE_ROOT env var "
        "or pass --workspace-root (e.g. /root/test_code)."
    )


# ──────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────
def resolve_api_key(args: argparse.Namespace) -> str:
    """Resolve the API server bearer key.

    Precedence: --api-key, then HERMES_API_KEY, then API_SERVER_KEY env.
    The Hermes API server refuses to start without API_SERVER_KEY, so an
    empty key here almost certainly means a misconfigured run.
    """
    key = (
        args.api_key
        or os.environ.get("HERMES_API_KEY", "")
        or os.environ.get("API_SERVER_KEY", "")
    )
    return key


# ──────────────────────────────────────────────────────────────────────
# HTTP: create session + reply-text extraction
# ──────────────────────────────────────────────────────────────────────
def create_session(
    base_url: str,
    api_key: str,
    title: str,
    timeout_sec: int,
) -> str:
    """POST /api/sessions -> session_id. One persistent session per run."""
    url = f"{base_url}/api/sessions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with httpx.Client(timeout=min(60.0, timeout_sec + 10.0)) as client:
        resp = client.post(url, json={"title": title}, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    session_id = (
        data.get("session_id")
        or data.get("id")
        or (data.get("session") or {}).get("id")
    )
    if not session_id:
        raise ValueError(f"Create-session returned no session id: {data}")
    return str(session_id)


def extract_reply_text(body: Dict[str, Any]) -> str:
    """Pull the assistant reply text out of a /chat response body.

    Hermes' response shape can vary by version, so probe the common fields
    in order: a flat ``output``/``response``/``text``/``content``/``message``
    string, then a message-object's content, then an OpenAI-style
    ``choices[0].message.content``. Returns "" if nothing text-like is found
    (the run still records tokens; only replyChars is affected).
    """
    if not isinstance(body, dict):
        return ""

    # 1) flat string fields
    for key in ("output", "response", "text", "content", "message", "reply"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # 2) message object: {"message": {"content": "..."]}} or content blocks
    msg = body.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") in (None, "text")
            ]
            joined = "".join(parts).strip()
            if joined:
                return joined

    # 3) OpenAI-style choices[].message.content (chat/completions fallback)
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        ch = choices[0]
        if isinstance(ch, dict):
            m = ch.get("message") or {}
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()

    return ""


def extract_usage(body: Dict[str, Any]) -> Dict[str, int]:
    """Normalize the usage block to prompt/completion/total ints.

    Hermes returns ``usage.{input_tokens,output_tokens,total_tokens}``. Some
    endpoints (chat/completions compat) use ``prompt_tokens/completion_tokens``
    instead — accept both so the harness works across endpoints.
    """
    usage = body.get("usage") or {}
    prompt = usage.get("input_tokens")
    if prompt is None:
        prompt = usage.get("prompt_tokens")
    completion = usage.get("output_tokens")
    if completion is None:
        completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    prompt = prompt or 0
    completion = completion or 0
    total = total or (prompt + completion)
    return {
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": total,
    }


# ──────────────────────────────────────────────────────────────────────
# Core: one turn against a persistent session
# ──────────────────────────────────────────────────────────────────────
def run_single_turn(
    base_url: str,
    session_id: str,
    prompt: str,
    timeout_sec: int,
    api_key: str = "",
) -> Dict[str, Any]:
    """POST one turn to /api/sessions/{id}/chat, return metrics + reply.

    Turns run sequentially against the SAME session_id, so context and
    memory accumulate across the 3 turns (turn 3 depends on 1-2). The
    per-request agent instance means the returned usage is effectively a
    per-turn delta.
    """
    url = f"{base_url}/api/sessions/{session_id}/chat"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"input": prompt}

    start_time = time.time()
    body: Dict[str, Any] = {}
    error_msg = ""
    try:
        with httpx.Client(timeout=timeout_sec + 10.0) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:  # noqa: BLE001 — record any transport/parse failure
        error_msg = str(e)

    duration_ms = int((time.time() - start_time) * 1000)

    usage = extract_usage(body) if not error_msg else {
        "promptTokens": 0, "completionTokens": 0, "totalTokens": 0,
    }
    reply_text = extract_reply_text(body) if not error_msg else ""

    return {
        "durationMs": duration_ms,
        "promptTokens": usage["promptTokens"],
        "completionTokens": usage["completionTokens"],
        "totalTokens": usage["totalTokens"],
        # These two only exist on QwenPaw's turn_usage path; kept at 0 here
        # to preserve identical CSV columns for cross-agent comparison.
        "estimatedContextTokens": 0,
        "contextUsageRatio": 0.0,
        "replyChars": len(reply_text),
        "replyText": reply_text,
        "error": error_msg,
        "status": "error" if error_msg else "success",
    }


def load_prompts(prompts_path: str, workspace_root: Path) -> List[Dict[str, str]]:
    """Load prompts from JSON, substitute {{WORKSPACE_ROOT}}."""
    path = Path(prompts_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Prompts file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, list):
        raise ValueError("Prompts JSON must be an array")

    prompts = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            text = item
            item_id = f"turn-{i+1}"
        elif isinstance(item, dict) and "prompt" in item:
            text = item["prompt"]
            item_id = item.get("id", f"turn-{i+1}")
        else:
            raise ValueError(f"Invalid prompt entry at index {i}")
        if "{{WORKSPACE_ROOT}}" in text:
            text = text.replace("{{WORKSPACE_ROOT}}", str(workspace_root))
        prompts.append({"id": item_id, "prompt": text})

    if not prompts:
        raise ValueError("No prompts found")
    return prompts


def run_bench(args: argparse.Namespace) -> None:
    """Run one benchmark: create session, run all turns, write JSON+CSV."""
    workspace_root = resolve_workspace_root(args.workspace_root)
    prompts = load_prompts(args.prompts, workspace_root)
    api_key = resolve_api_key(args)

    started_at = datetime.now().isoformat()
    print(f"[hermes-bench] started at {started_at}")
    print(f"[hermes-bench] prompts: {args.prompts} ({len(prompts)} turns)")
    print(f"[hermes-bench] base-url: {args.base_url}")
    print(f"[hermes-bench] workspace: {workspace_root}")
    print(f"[hermes-bench] auth: {'bearer key' if api_key else 'NONE (likely misconfigured)'}")

    # One persistent session for the whole run.
    session_title = args.session_title or args.label
    session_id = create_session(args.base_url, api_key, session_title, args.timeout_sec)
    print(f"[hermes-bench] session_id: {session_id}")

    rows = []
    prev_prompt_tokens = 0

    for i, turn in enumerate(prompts):
        turn_num = i + 1
        print(f"[hermes-bench] turn {turn_num}/{len(prompts)} ({turn['id']})...", end=" ", flush=True)

        result = run_single_turn(
            args.base_url,
            session_id,
            turn["prompt"],
            args.timeout_sec,
            api_key=api_key,
        )

        # Compaction proxy: promptTokens drops >30% vs previous turn despite
        # accumulating history — same heuristic as the qwenpaw bench.
        compaction_delta = 0
        cur_prompt = result["promptTokens"]
        if (
            i > 0
            and prev_prompt_tokens > 0
            and cur_prompt > 0
            and cur_prompt < prev_prompt_tokens * 0.7
        ):
            compaction_delta = 1
        prev_prompt_tokens = cur_prompt

        rows.append({
            "turn": turn_num,
            "turnId": turn["id"],
            "prompt": turn["prompt"][:100],
            "status": result["status"],
            "durationMs": result["durationMs"],
            "promptTokens": result["promptTokens"],
            "completionTokens": result["completionTokens"],
            "totalTokens": result["totalTokens"],
            "estimatedContextTokens": result["estimatedContextTokens"],
            "contextUsageRatio": result["contextUsageRatio"],
            "compactionCountDelta": compaction_delta,
            "replyChars": result["replyChars"],
            "error": result["error"],
        })

        if result["error"]:
            print(f"ERROR: {result['error']}")
            if not args.continue_on_error:
                break
        else:
            print(f"OK ({result['durationMs']}ms, {result['totalTokens']} tokens)")

    ended_at = datetime.now().isoformat()
    summary = compute_summary(rows)

    report = {
        "metadata": {
            "label": args.label,
            "startedAt": started_at,
            "endedAt": ended_at,
            "promptsPath": str(Path(args.prompts).resolve()),
            "sessionId": session_id,
            "baseUrl": args.base_url,
            "workspaceRoot": str(workspace_root),
            "turnsExecuted": len(rows),
        },
        "summary": summary,
        "rows": rows,
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    base_name = f"{args.label}-{stamp}"
    json_path = out_dir / f"{base_name}.json"
    csv_path = out_dir / f"{base_name}.csv"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    write_csv(rows, csv_path)

    print(f"[hermes-bench] finished at {ended_at}")
    print(f"[hermes-bench] report json: {json_path}")
    print(f"[hermes-bench] report csv : {csv_path}")
    print(
        f"[hermes-bench] totals: prompt={summary['totals']['promptTokens']} "
        f"completion={summary['totals']['completionTokens']} "
        f"total={summary['totals']['totalTokens']}"
    )


# ──────────────────────────────────────────────────────────────────────
# Summary (identical to qwenpaw bench)
# ──────────────────────────────────────────────────────────────────────
def percentile(sorted_values: List[float], p: float) -> float:
    """Percentile via linear interpolation on a sorted list."""
    if not sorted_values:
        return 0.0
    rank = (p / 100) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    if lo == hi:
        return sorted_values[lo]
    weight = rank - lo
    return sorted_values[lo] * (1 - weight) + sorted_values[hi] * weight


def compute_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Totals + averages + latency percentiles + compaction count."""
    durations = sorted([r["durationMs"] for r in rows if r.get("durationMs")])

    def sum_field(field: str) -> int:
        return sum(r.get(field) or 0 for r in rows)

    count = len(rows)
    totals = {
        "promptTokens": sum_field("promptTokens"),
        "completionTokens": sum_field("completionTokens"),
        "totalTokens": sum_field("totalTokens"),
        "estimatedContextTokens": sum_field("estimatedContextTokens"),
        "replyChars": sum_field("replyChars"),
    }

    def avg(value: int) -> float:
        return value / count if count > 0 else 0.0

    return {
        "turns": count,
        "totals": totals,
        "averages": {
            "promptTokens": avg(totals["promptTokens"]),
            "completionTokens": avg(totals["completionTokens"]),
            "totalTokens": avg(totals["totalTokens"]),
            "durationMs": avg(sum_field("durationMs")),
            "replyChars": avg(totals["replyChars"]),
        },
        "latencyMs": {
            "p50": percentile(durations, 50),
            "p90": percentile(durations, 90),
            "max": durations[-1] if durations else 0,
        },
        "compactionTriggeredTurns": sum(
            1 for r in rows if r.get("compactionCountDelta", 0) > 0
        ),
    }


# ──────────────────────────────────────────────────────────────────────
# CSV output (identical columns to qwenpaw bench)
# ──────────────────────────────────────────────────────────────────────
def write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    headers = [
        "turn", "turnId", "status", "durationMs",
        "promptTokens", "completionTokens", "totalTokens",
        "estimatedContextTokens", "contextUsageRatio",
        "compactionCountDelta", "replyChars", "error",
    ]

    def escape_csv(value: Any) -> str:
        if value is None or value == "":
            return ""
        s = str(value) if not isinstance(value, str) else value
        if any(c in s for c in [",", '"', "\n"]):
            return '"' + s.replace('"', '""') + '"'
        return s

    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(escape_csv(row.get(h)) for h in headers))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")


# ──────────────────────────────────────────────────────────────────────
# Compare (identical to qwenpaw bench)
# ──────────────────────────────────────────────────────────────────────
def compare_reports(args: argparse.Namespace) -> None:
    base_path = Path(args.baseline).resolve()
    cand_path = Path(args.candidate).resolve()
    if not base_path.exists() or not cand_path.exists():
        print("ERROR: baseline/candidate file not found", file=sys.stderr)
        sys.exit(1)

    with open(base_path, "r", encoding="utf-8") as f:
        base = json.load(f)
    with open(cand_path, "r", encoding="utf-8") as f:
        cand = json.load(f)

    metrics = [
        "summary.totals.promptTokens",
        "summary.totals.completionTokens",
        "summary.totals.totalTokens",
        "summary.averages.durationMs",
        "summary.latencyMs.p50",
        "summary.latencyMs.p90",
        "summary.compactionTriggeredTurns",
    ]

    def pick(obj: Dict, path: str) -> float:
        val = obj
        for key in path.split("."):
            val = val.get(key, 0) if isinstance(val, dict) else 0
        return float(val or 0)

    rows = []
    for name in metrics:
        baseline = pick(base, name)
        candidate = pick(cand, name)
        diff = candidate - baseline
        pct = (diff / baseline * 100) if baseline != 0 else None
        rows.append({
            "metric": name,
            "baseline": baseline,
            "candidate": candidate,
            "diff": diff,
            "diffPercent": pct,
        })

    report = {
        "comparedAt": datetime.now().isoformat(),
        "baseline": {
            "path": str(base_path),
            "label": base.get("metadata", {}).get("label", "baseline"),
            "turns": base.get("summary", {}).get("turns", 0),
        },
        "candidate": {
            "path": str(cand_path),
            "label": cand.get("metadata", {}).get("label", "candidate"),
            "turns": cand.get("summary", {}).get("turns", 0),
        },
        "metrics": rows,
    }

    out_path = (
        Path(args.out).resolve() if args.out
        else cand_path.parent / f"compare-{int(time.time())}.json"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"[hermes-bench] compare report: {out_path}")
    for row in rows:
        pct_text = "n/a" if row["diffPercent"] is None else f"{row['diffPercent']:.2f}%"
        print(
            f"[hermes-bench] {row['metric']}: "
            f"baseline={row['baseline']} candidate={row['candidate']} "
            f"diff={row['diff']} ({pct_text})"
        )


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hermes-agent tool-use token-saving bench"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run benchmark")
    run_parser.add_argument("--prompts", required=True, help="Prompts JSON file")
    run_parser.add_argument("--label", required=True, help="Run label for output files")
    run_parser.add_argument("--session-title", default="", help="Session title (defaults to --label)")
    run_parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Hermes API server base URL")
    run_parser.add_argument("--api-key", default="", help="API_SERVER_KEY (or HERMES_API_KEY / API_SERVER_KEY env)")
    run_parser.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC, help="Per-turn timeout")
    run_parser.add_argument("--out-dir", default="bench-results", help="Output directory")
    run_parser.add_argument("--workspace-root", help="Workspace root (parent of kubernetes/); or BENCH_WORKSPACE_ROOT env")
    run_parser.add_argument("--continue-on-error", action="store_true", help="Continue on turn failures")

    compare_parser = subparsers.add_parser("compare", help="Compare two reports")
    compare_parser.add_argument("--baseline", required=True, help="Baseline JSON report")
    compare_parser.add_argument("--candidate", required=True, help="Candidate JSON report")
    compare_parser.add_argument("--out", help="Output path for compare report")

    args = parser.parse_args()

    if args.command == "run":
        run_bench(args)
    elif args.command == "compare":
        compare_reports(args)


if __name__ == "__main__":
    main()





