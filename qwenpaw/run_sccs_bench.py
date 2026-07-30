#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QwenPaw SCCS Bench — toolresult compression regression

Drives QwenPaw via HTTP SSE, collects per-turn token/context metrics,
and outputs JSON/CSV reports compatible with the original mjs bench.
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
DEFAULT_BASE_URL = "http://127.0.0.1:8088"
DEFAULT_TIMEOUT_SEC = 600


# ──────────────────────────────────────────────────────────────────────
# Authentication
# ──────────────────────────────────────────────────────────────────────
def get_auth_token(base_url: str, username: str, password: str) -> str:
    """Login to QwenPaw and return Bearer token."""
    login_url = f"{base_url}/api/auth/login"
    payload = {"username": username, "password": password}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(login_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token", "")
        if not token:
            raise ValueError("Login succeeded but no token returned")
        return token


# ──────────────────────────────────────────────────────────────────────
# Path resolution
# ──────────────────────────────────────────────────────────────────────
def resolve_workspace_root(explicit: Optional[str] = None) -> Path:
    """Resolve workspace root from --workspace-root or BENCH_WORKSPACE_ROOT."""
    if explicit:
        return Path(explicit).resolve()
    env_val = os.environ.get("BENCH_WORKSPACE_ROOT")
    if env_val:
        return Path(env_val).resolve()
    # Fallback: assume script is in .../toolresult_compression_tests/qwenpaw/
    # and workspace is ../../../../../../../.openclaw/workspace
    script_dir = Path(__file__).parent
    candidate = script_dir.parent.parent.parent.parent.parent.parent / ".openclaw" / "workspace"
    if candidate.exists():
        return candidate.resolve()
    raise ValueError(
        "Cannot resolve workspace root. Set BENCH_WORKSPACE_ROOT env var "
        "or pass --workspace-root."
    )


def resolve_repo_path(workspace_root: Path, rel: str) -> str:
    """Convert workspace-relative path to absolute."""
    return str((workspace_root / rel).resolve())


# ──────────────────────────────────────────────────────────────────────
# SSE parsing
# ──────────────────────────────────────────────────────────────────────
def parse_sse_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse a single SSE line 'data: {...}'."""
    stripped = line.strip()
    if stripped.startswith("data: "):
        try:
            return json.loads(stripped[6:])
        except json.JSONDecodeError:
            return None
    return None


def _message_text(msg: Dict[str, Any]) -> str:
    """Concatenate the text blocks of one message dict."""
    parts = []
    for item in msg.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text") or ""
            if text:
                parts.append(text)
    return "".join(parts)


def extract_text_content(response_data: Dict[str, Any]) -> str:
    """Extract the assistant reply text from a completed `response` frame.

    `response.output` is a list of messages ordered like
    ``[reasoning, message, ...]`` (tool turns also interleave
    ``function_call`` / ``function_call_output`` messages). The naive
    ``output[-1]`` often lands on a reasoning or tool message with no text,
    so we search backwards for the last ``type == "message"`` entry that
    actually carries text — mirroring QwenPaw's own ``_response_to_text``.
    """
    if not isinstance(response_data, dict):
        return ""
    output = response_data.get("output") or []
    if not isinstance(output, list):
        return ""
    for msg in reversed(output):
        if not isinstance(msg, dict):
            continue
        if msg.get("type") != "message":
            continue
        text = _message_text(msg)
        if text.strip():
            return text.strip()
    return ""


# ──────────────────────────────────────────────────────────────────────
# Request construction
# ──────────────────────────────────────────────────────────────────────
def build_chat_request(
    agent: str,
    session_id: str,
    prompt: str,
) -> Dict[str, Any]:
    """Build /api/console/chat request payload."""
    return {
        "session_id": session_id,
        "user_id": agent,
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
        ],
        "request_context": {
            "root_agent_id": agent,
        },
    }


# ──────────────────────────────────────────────────────────────────────
# Core run logic
# ──────────────────────────────────────────────────────────────────────
def run_single_turn(
    base_url: str,
    agent: str,
    session_id: str,
    prompt: str,
    timeout_sec: int,
    auth_token: str = "",
) -> Dict[str, Any]:
    """Execute one turn via SSE, return metrics + reply."""
    api_url = f"{base_url}/api/console/chat"
    headers = {"X-Agent-Id": agent}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    payload = build_chat_request(agent, session_id, prompt)

    start_time = time.time()
    turn_usage = None
    response_final = None       # last object=="response" && status=="completed"
    msg_text_parts: List[str] = []  # fallback: completed message frames
    error_msg = ""

    try:
        with httpx.Client(timeout=timeout_sec + 10.0) as client:
            with client.stream("POST", api_url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    parsed = parse_sse_line(line)
                    if not parsed:
                        continue
                    # Defensive: some paths may emit a standalone turn_usage
                    # frame. If present it overrides response.usage.
                    if parsed.get("type") == "turn_usage":
                        turn_usage = parsed
                        continue
                    obj = parsed.get("object")
                    status = parsed.get("status")
                    # The authoritative frame: carries both `output` and
                    # `usage`. Only the completed one has real token counts
                    # (created/in_progress frames have usage=null).
                    if obj == "response" and status == "completed":
                        response_final = parsed
                    # Fallback text source: completed assistant message frames
                    # (type=="message", not reasoning/function_call).
                    elif (
                        obj == "message"
                        and status == "completed"
                        and parsed.get("type") == "message"
                    ):
                        text = _message_text(parsed)
                        if text.strip():
                            msg_text_parts.append(text.strip())
    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        error_msg = str(e)

    duration_ms = int((time.time() - start_time) * 1000)

    # Extract usage: prefer standalone turn_usage frame (if any), else the
    # completed response frame's `usage` (the real source on /console/chat).
    usage = (turn_usage or {}).get("usage") or (response_final or {}).get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    total_tokens = usage.get("total_tokens") or 0
    # context_usage only exists on the turn_usage path (absent on
    # /console/chat), so these stay 0 there — see README note.
    context_usage = (turn_usage or {}).get("context_usage") or {}
    estimated_tokens = context_usage.get("estimated_tokens") or 0
    context_usage_ratio = context_usage.get("context_usage_ratio") or 0.0

    # Extract reply text from the completed response frame; fall back to
    # accumulated completed-message frames.
    reply_text = extract_text_content(response_final or {})
    if not reply_text and msg_text_parts:
        reply_text = "\n".join(msg_text_parts).strip()

    return {
        "durationMs": duration_ms,
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "totalTokens": total_tokens,
        "estimatedContextTokens": estimated_tokens,
        "contextUsageRatio": context_usage_ratio,
        "replyChars": len(reply_text),
        "replyText": reply_text,
        "error": error_msg,
        "status": "error" if error_msg else "success",
    }


def load_prompts(prompts_path: str, workspace_root: Path) -> List[Dict[str, str]]:
    """Load prompts from JSON, resolve repo paths."""
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
            prompts.append({"id": f"turn-{i+1}", "prompt": item})
        elif isinstance(item, dict) and "prompt" in item:
            # Replace {{WORKSPACE_ROOT}} placeholder if present
            prompt_text = item["prompt"]
            if "{{WORKSPACE_ROOT}}" in prompt_text:
                prompt_text = prompt_text.replace("{{WORKSPACE_ROOT}}", str(workspace_root))
            prompts.append({
                "id": item.get("id", f"turn-{i+1}"),
                "prompt": prompt_text,
            })
        else:
            raise ValueError(f"Invalid prompt entry at index {i}")

    if not prompts:
        raise ValueError("No prompts found")

    return prompts


def resolve_auth_token(args: argparse.Namespace) -> str:
    """Resolve Bearer token: --auth-token, or login with --username/--password.

    Env fallbacks: QWENPAW_AUTH_TOKEN, QWENPAW_USERNAME, QWENPAW_PASSWORD.
    Returns "" when no auth is configured (server auth disabled).
    """
    token = args.auth_token or os.environ.get("QWENPAW_AUTH_TOKEN", "")
    if token:
        return token

    username = args.username or os.environ.get("QWENPAW_USERNAME", "")
    password = args.password or os.environ.get("QWENPAW_PASSWORD", "")
    if username and password:
        print(f"[sccs-bench] logging in as {username}...")
        return get_auth_token(args.base_url, username, password)

    return ""


def run_bench(args: argparse.Namespace) -> None:
    """Run benchmark with multiple turns."""
    workspace_root = resolve_workspace_root(args.workspace_root)
    prompts = load_prompts(args.prompts, workspace_root)
    auth_token = resolve_auth_token(args)

    started_at = datetime.now().isoformat()
    rows = []
    prev_prompt_tokens = 0

    print(f"[sccs-bench] started at {started_at}")
    print(f"[sccs-bench] prompts: {args.prompts} ({len(prompts)} turns)")
    print(f"[sccs-bench] target: agent={args.agent} session={args.session_id}")
    print(f"[sccs-bench] workspace: {workspace_root}")
    print(f"[sccs-bench] auth: {'bearer token' if auth_token else 'none'}")

    for i, turn in enumerate(prompts):
        turn_num = i + 1
        print(f"[sccs-bench] turn {turn_num}/{len(prompts)} ({turn['id']})...", end=" ", flush=True)

        result = run_single_turn(
            args.base_url,
            args.agent,
            args.session_id,
            turn["prompt"],
            args.timeout_sec,
            auth_token=auth_token,
        )

        # Detect likely compaction: prompt (context) tokens drop sharply vs
        # the previous turn despite accumulating history. On /console/chat we
        # have no context_usage_ratio, so promptTokens is the observable proxy
        # for tool-result / context pruning. Threshold: >30% drop.
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

        row = {
            "turn": turn_num,
            "turnId": turn["id"],
            "prompt": turn["prompt"][:100],  # truncate for CSV
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
        }
        rows.append(row)

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
            "agent": args.agent,
            "sessionId": args.session_id,
            "baseUrl": args.base_url,
            "workspaceRoot": str(workspace_root),
            "turnsExecuted": len(rows),
        },
        "summary": summary,
        "rows": rows,
    }

    # Write outputs
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    base_name = f"{args.label}-{stamp}"
    json_path = out_dir / f"{base_name}.json"
    csv_path = out_dir / f"{base_name}.csv"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    write_csv(rows, csv_path)

    print(f"[sccs-bench] finished at {ended_at}")
    print(f"[sccs-bench] report json: {json_path}")
    print(f"[sccs-bench] report csv : {csv_path}")
    print(
        f"[sccs-bench] totals: prompt={summary['totals']['promptTokens']} "
        f"completion={summary['totals']['completionTokens']} "
        f"total={summary['totals']['totalTokens']}"
    )


# ──────────────────────────────────────────────────────────────────────
# Summary computation
# ──────────────────────────────────────────────────────────────────────
def percentile(sorted_values: List[float], p: float) -> float:
    """Compute percentile from sorted list."""
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
    """Compute aggregated metrics."""
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
        "compactionTriggeredTurns": sum(1 for r in rows if r.get("compactionCountDelta", 0) > 0),
    }


# ──────────────────────────────────────────────────────────────────────
# CSV output
# ──────────────────────────────────────────────────────────────────────
def write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    """Write rows to CSV."""
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
# Compare command
# ──────────────────────────────────────────────────────────────────────
def compare_reports(args: argparse.Namespace) -> None:
    """Compare two JSON reports."""
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

    out_path = Path(args.out).resolve() if args.out else cand_path.parent / f"compare-{int(time.time())}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"[sccs-bench] compare report: {out_path}")
    for row in rows:
        pct_text = "n/a" if row["diffPercent"] is None else f"{row['diffPercent']:.2f}%"
        print(
            f"[sccs-bench] {row['metric']}: "
            f"baseline={row['baseline']} candidate={row['candidate']} "
            f"diff={row['diff']} ({pct_text})"
        )


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="QwenPaw SCCS Bench — toolresult compression regression"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run command
    run_parser = subparsers.add_parser("run", help="Run benchmark")
    run_parser.add_argument("--prompts", required=True, help="Prompts JSON file")
    run_parser.add_argument("--label", required=True, help="Run label for output files")
    run_parser.add_argument("--session-id", required=True, help="QwenPaw session ID")
    run_parser.add_argument("--agent", default="default", help="QwenPaw agent ID")
    run_parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="QwenPaw API base URL")
    run_parser.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC, help="Per-turn timeout")
    run_parser.add_argument("--out-dir", default="bench-results", help="Output directory")
    run_parser.add_argument("--workspace-root", help="Workspace root (or use BENCH_WORKSPACE_ROOT env)")
    run_parser.add_argument("--continue-on-error", action="store_true", help="Continue on turn failures")
    run_parser.add_argument("--auth-token", default="", help="Bearer token (or QWENPAW_AUTH_TOKEN env); skips login")
    run_parser.add_argument("--username", default="", help="Login username (or QWENPAW_USERNAME env)")
    run_parser.add_argument("--password", default="", help="Login password (or QWENPAW_PASSWORD env)")

    # compare command
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
