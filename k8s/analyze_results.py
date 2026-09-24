#!/usr/bin/env python3
"""
analyze_results.py

Implements Steps 20-24 of the research plan against the results/ folder
produced by collect-run-data.sh and the k6 --summary-export files.

Usage:
    python3 analyze_results.py <results_dir>

Expects each run to live under <results_dir>/<RUN_ID>/ with:
    - k6_summary.json (or any *.json matching scenario-*-*.json copied in)
    - cpu_usage.json, memory_usage.json, replica_count.json,
      cpu_utilization_pct.json  (Prometheus query_range responses)
    - deployment_events.json, pod_timestamps.json
    - hpa_status.json / phpa_status.json (one will be empty)

Outputs:
    <results_dir>/summary.csv       - one row per run (Step 23)
    <results_dir>/comparison.csv    - mean/median/stddev per scenario+autoscaler (Step 24)
    Prints both tables to stdout as well.

ASSUMPTIONS (Step 22 - stated explicitly per the plan's own requirement that
cost is comparative, not an actual bill):
    ASSUMED_CPU_RATE_PER_CORE_HOUR = 0.024   (USD, illustrative)
    ASSUMED_MEM_RATE_PER_GB_HOUR   = 0.003   (USD, illustrative)
"""

import json
import sys
import re
import statistics
from pathlib import Path
from datetime import datetime, timezone

ASSUMED_CPU_RATE_PER_CORE_HOUR = 0.024
ASSUMED_MEM_RATE_PER_GB_HOUR = 0.003


def load_json(path):
    if not path.exists() or path.stat().st_size == 0:
        return None
    with open(path) as f:
        return json.load(f)


def parse_run_id(run_id):
    """Extract scenario, autoscaler, repetition from a run_id, tolerating
    arbitrary infixes between components (e.g. '_long_', '_ext_') as used by
    the primary set ('20260721_A_hpa_r1'), the long-duration confirmatory set
    ('20260816_A_hpa_long_r1'), and the extended Scenario-C set
    ('20260817_C_ext_hpa_r1')."""
    scenario_m = re.search(r'_([ABC])_', run_id)
    autoscaler_m = re.search(r'(hpa|phpa)', run_id)
    rep_m = re.search(r'r(\d+)$', run_id)

    scenario = scenario_m.group(1) if scenario_m else "?"
    autoscaler = autoscaler_m.group(1) if autoscaler_m else "?"
    rep = int(rep_m.group(1)) if rep_m else "?"
    return {"scenario": scenario, "autoscaler": autoscaler, "rep": rep}


def find_k6_summary(run_dir):
    """k6 summary may be named k6_summary.json or copied in with its
    original scenario-*.json name - find whichever exists."""
    candidates = list(run_dir.glob("k6_summary.json")) + \
        list(run_dir.glob("scenario-*.json"))
    return load_json(candidates[0]) if candidates else None


def parse_k6(k6_data):
    if not k6_data:
        return {}
    m = k6_data.get("metrics", {})
    dur = m.get("http_req_duration", {})
    reqs = m.get("http_reqs", {})
    failed = m.get("http_req_failed", {})
    total = reqs.get("count", 0)
    # http_req_failed is a k6 Rate metric: its "value" field is already the
    # correctly-computed failure rate (0.0-1.0). Its "passes"/"fails" counters
    # are inverted relative to normal intuition for a boolean rate metric
    # ("passes" = count where the failed-condition was true; "fails" = count
    # where false) - so we use "value" directly rather than deriving from
    # passes/fails, to avoid exactly this kind of sign confusion.
    error_rate_pct = failed.get("value", 0) * 100
    actual_failed_requests = failed.get("passes", 0)
    return {
        "p95_latency_ms": dur.get("p(95)"),
        "avg_latency_ms": dur.get("avg"),
        "throughput_rps": reqs.get("rate"),
        "total_requests": total,
        "failed_requests": actual_failed_requests,
        "error_rate_pct": error_rate_pct,
    }


def parse_range_series(data):
    """Flatten a Prometheus query_range response into a list of
    (timestamp, value, pod) tuples across all returned series."""
    out = []
    if not data or data.get("status") != "success":
        return out
    for series in data["data"]["result"]:
        pod = series["metric"].get("pod", "unknown")
        for ts, val in series["values"]:
            out.append((float(ts), float(val), pod))
    return out


def mean_cpu_utilization(cpu_util_data):
    points = parse_range_series(cpu_util_data)
    if not points:
        return None
    return statistics.mean(v for _, v, _ in points)


def compute_replica_minutes(replica_count_data, window_start, window_end):
    """Step 20: sum(replicas_i * interval_seconds / 60) using the actual
    sampled intervals from the Prometheus range query."""
    points = parse_range_series(replica_count_data)
    if not points:
        return None
    points = sorted(set((ts, v) for ts, v, _ in points))
    total = 0.0
    for i in range(len(points) - 1):
        ts, replicas = points[i]
        next_ts = points[i + 1][0]
        interval = next_ts - ts
        total += replicas * (interval / 60.0)
    return total


def compute_cpu_waste_pct(cpu_usage_data, requested_cpu_cores, window_start, window_end):
    """Step 21: (requested CPU time - used CPU time) / requested CPU time * 100
    Approximated using average per-pod CPU usage (cores) over the window,
    multiplied by window duration and pod-count, vs requested cores * pods *
    duration."""
    points = parse_range_series(cpu_usage_data)
    if not points:
        return None
    duration = window_end - window_start
    pods = set(p for _, _, p in points)
    n_pods_samples = {}
    for ts, val, pod in points:
        n_pods_samples.setdefault(pod, []).append(val)
    used_core_seconds = 0.0
    requested_core_seconds = 0.0
    for pod, vals in n_pods_samples.items():
        avg_usage = statistics.mean(vals)
        used_core_seconds += avg_usage * duration
        requested_core_seconds += requested_cpu_cores * duration
    if requested_core_seconds == 0:
        return None
    waste = (requested_core_seconds - used_core_seconds) / requested_core_seconds * 100
    return waste


def compute_estimated_cost(replica_minutes, requested_cpu_cores, requested_mem_gb):
    """Step 22: CPU requested core-hours * rate + memory requested GB-hours * rate.
    replica-minutes already captures the pod-time-weighted average, so:
      core-hours = replica_minutes/60 * requested_cpu_cores
      gb-hours   = replica_minutes/60 * requested_mem_gb
    """
    if replica_minutes is None:
        return None
    core_hours = (replica_minutes / 60.0) * requested_cpu_cores
    gb_hours = (replica_minutes / 60.0) * requested_mem_gb
    cost = core_hours * ASSUMED_CPU_RATE_PER_CORE_HOUR + gb_hours * ASSUMED_MEM_RATE_PER_GB_HOUR
    return cost


def parse_scaling_events(events_data):
    """Count scale-up events and return their timestamps."""
    if not events_data:
        return [], []
    items = events_data.get("items", [])
    scale_ups = []
    scale_downs = []
    for item in items:
        msg = item.get("message", "")
        ts = item.get("lastTimestamp") or item.get("eventTime")
        if "Scaled up" in msg:
            scale_ups.append(ts)
        elif "Scaled down" in msg:
            scale_downs.append(ts)
    return scale_ups, scale_downs


def to_unix(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def compute_scaling_delay(events_data, pod_timestamps_data):
    """Step 19: for each scale-up event, find the next pod's Ready timestamp
    after that event and compute provisioning delay. Returns mean provisioning
    delay in seconds across all scale-up events found for this run, and the
    scale-up event count."""
    scale_ups, _ = parse_scaling_events(events_data)
    if not scale_ups or not pod_timestamps_data:
        return None, len(scale_ups)

    ready_times = []
    for pod in pod_timestamps_data:
        cond = pod.get("readyCondition", {})
        if cond.get("status") == "True":
            rt = to_unix(cond.get("lastTransitionTime"))
            if rt:
                ready_times.append(rt)
    ready_times.sort()

    delays = []
    for su_ts_str in scale_ups:
        su_ts = to_unix(su_ts_str)
        if su_ts is None:
            continue
        candidates = [rt for rt in ready_times if rt >= su_ts]
        if candidates:
            delays.append(min(candidates) - su_ts)

    if not delays:
        return None, len(scale_ups)
    return statistics.mean(delays), len(scale_ups)


def parse_continuous_pod_snapshots_full(run_dir):
    """Like parse_continuous_pod_snapshots, but returns both creation and
    ready timestamps per pod, needed to filter candidates to genuinely new
    pods for a given scale-up event."""
    path = run_dir / "pod_snapshots_continuous.jsonl"
    if not path.exists():
        return None

    info = {}
    raw = path.read_text()
    blocks = raw.split("---SNAPSHOT-BOUNDARY---")
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for item in data.get("items", []):
            name = item["metadata"]["name"]
            if name not in info:
                info[name] = {"created": to_unix(item["metadata"].get("creationTimestamp")), "ready": None}
            for cond in item.get("status", {}).get("conditions", []):
                if cond.get("type") == "Ready" and cond.get("status") == "True":
                    rt = to_unix(cond.get("lastTransitionTime"))
                    if rt and (info[name]["ready"] is None or rt < info[name]["ready"]):
                        info[name]["ready"] = rt
    return info


def parse_continuous_pod_snapshots(run_dir):
    """Parses pod_snapshots_continuous.jsonl (from watch-pods-during-run.sh)
    into a dict of {pod_name: earliest_ready_transition_timestamp}, covering
    every pod that existed at any point during the run - including ones that
    were later deleted, which a single post-run snapshot would miss."""
    full = parse_continuous_pod_snapshots_full(run_dir)
    if not full:
        return None
    return {name: v["ready"] for name, v in full.items() if v["ready"] is not None}


def compute_scaling_delay_v2(events_data, run_dir, window_start=None, window_end=None):
    """Corrected Step 19 implementation using continuous pod snapshots
    (see parse_continuous_pod_snapshots) instead of a single post-run
    snapshot, so pods that were created and deleted within the same run are
    no longer missed. Only pods actually CREATED at/after a scale-up event
    are considered candidates for that event, to avoid mismatching against
    an already-existing pod that happens to transition to Ready around the
    same time for an unrelated reason (e.g. a restart).

    Critically, scale-up events are also filtered to those falling within
    [window_start, window_end] - kubectl get events returns ALL retained
    events for the Deployment regardless of when the current run happened,
    so without this filter, events left over from an earlier run in the same
    session get mixed in and matched against the wrong run's pods, producing
    wildly inflated delay values."""
    scale_ups, _ = parse_scaling_events(events_data)

    if window_start is not None and window_end is not None:
        # small buffer on both sides to tolerate clock/collection skew
        scale_ups = [
            su for su in scale_ups
            if su and (to_unix(su) is not None)
            and (window_start - 10) <= to_unix(su) <= (window_end + 10)
        ]

    snapshot_data = parse_continuous_pod_snapshots_full(run_dir)

    if not scale_ups:
        return None, 0
    if not snapshot_data:
        return None, len(scale_ups)

    delays = []
    for su_ts_str in scale_ups:
        su_ts = to_unix(su_ts_str)
        if su_ts is None:
            continue
        # candidates: pods created at/after this event (with a small buffer
        # to tolerate clock skew), that have a recorded ready time
        candidates = [
            info["ready"] for info in snapshot_data.values()
            if info["created"] is not None and info["ready"] is not None
            and info["created"] >= su_ts - 2
        ]
        if candidates:
            delays.append(min(candidates) - su_ts)

    if not delays:
        return None, len(scale_ups)
    return statistics.mean(delays), len(scale_ups)


def analyze_run(run_dir, requested_cpu_cores=0.1, requested_mem_gb=0.0625):
    run_id = run_dir.name
    ids = parse_run_id(run_id)

    k6_data = find_k6_summary(run_dir)
    k6 = parse_k6(k6_data)

    cpu_util_data = load_json(run_dir / "cpu_utilization_pct.json")
    cpu_usage_data = load_json(run_dir / "cpu_usage.json")
    replica_data = load_json(run_dir / "replica_count.json")
    events_data = load_json(run_dir / "deployment_events.json")
    pod_ts_data = load_json(run_dir / "pod_timestamps.json")

    # Determine window from the replica_count series itself (min/max timestamp)
    points = parse_range_series(replica_data)
    if points:
        window_start = min(p[0] for p in points)
        window_end = max(p[0] for p in points)
    else:
        window_start = window_end = 0

    mean_cpu_pct = mean_cpu_utilization(cpu_util_data)
    replica_minutes = compute_replica_minutes(replica_data, window_start, window_end)
    waste_pct = compute_cpu_waste_pct(cpu_usage_data, requested_cpu_cores, window_start, window_end)
    est_cost = compute_estimated_cost(replica_minutes, requested_cpu_cores, requested_mem_gb)
    successful_requests = k6.get("total_requests", 0) - k6.get("failed_requests", 0)
    cost_per_success = (est_cost / successful_requests) if est_cost and successful_requests else None
    scale_delay, scale_event_count = compute_scaling_delay_v2(events_data, run_dir, window_start, window_end)
    if scale_delay is None and (run_dir / "pod_timestamps.json").exists():
        # fall back for older runs collected before this fix existed
        scale_delay, scale_event_count = compute_scaling_delay(events_data, pod_ts_data)

    return {
        "run_id": run_id,
        "scenario": ids["scenario"],
        "autoscaler": ids["autoscaler"],
        "rep": ids["rep"],
        "mean_cpu_utilization_pct": round(mean_cpu_pct, 2) if mean_cpu_pct is not None else None,
        "p95_latency_ms": k6.get("p95_latency_ms"),
        "throughput_rps": k6.get("throughput_rps"),
        "error_rate_pct": k6.get("error_rate_pct"),
        "scale_up_events": scale_event_count,
        "mean_scaling_delay_s": round(scale_delay, 2) if scale_delay is not None else None,
        "replica_minutes": round(replica_minutes, 3) if replica_minutes is not None else None,
        "cpu_waste_pct": round(waste_pct, 2) if waste_pct is not None else None,
        "estimated_cost_usd": round(est_cost, 6) if est_cost is not None else None,
        "cost_per_successful_request_usd": round(cost_per_success, 8) if cost_per_success is not None else None,
    }


def write_csv(rows, path, fieldnames):
    import csv
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def print_table(rows, fieldnames):
    widths = {fn: max(len(fn), *(len(str(r.get(fn, ""))) for r in rows)) for fn in fieldnames}
    header = " | ".join(fn.ljust(widths[fn]) for fn in fieldnames)
    print(header)
    print("-" * len(header))
    for r in rows:
        print(" | ".join(str(r.get(fn, "")).ljust(widths[fn]) for fn in fieldnames))


def build_comparison(summary_rows):
    """Step 24: mean/median/stddev per scenario+autoscaler, plus % difference
    between autoscalers within each scenario."""
    groups = {}
    for r in summary_rows:
        key = (r["scenario"], r["autoscaler"])
        groups.setdefault(key, []).append(r)

    metrics = ["p95_latency_ms", "throughput_rps", "error_rate_pct",
               "mean_scaling_delay_s", "replica_minutes", "cpu_waste_pct",
               "estimated_cost_usd"]

    comparison_rows = []
    for (scenario, autoscaler), rs in sorted(groups.items()):
        row = {"scenario": scenario, "autoscaler": autoscaler, "n": len(rs)}
        for m in metrics:
            vals = [r[m] for r in rs if r.get(m) is not None]
            if vals:
                row[f"{m}_mean"] = round(statistics.mean(vals), 4)
                row[f"{m}_median"] = round(statistics.median(vals), 4)
                row[f"{m}_stddev"] = round(statistics.stdev(vals), 4) if len(vals) > 1 else 0.0
            else:
                row[f"{m}_mean"] = row[f"{m}_median"] = row[f"{m}_stddev"] = None
        comparison_rows.append(row)

    # percentage difference HPA vs PHPA within each scenario
    pct_diff_rows = []
    for scenario in sorted(set(r["scenario"] for r in comparison_rows)):
        hpa = next((r for r in comparison_rows if r["scenario"] == scenario and r["autoscaler"] == "hpa"), None)
        phpa = next((r for r in comparison_rows if r["scenario"] == scenario and r["autoscaler"] == "phpa"), None)
        if not hpa or not phpa:
            continue
        diff_row = {"scenario": scenario}
        for m in metrics:
            h = hpa.get(f"{m}_mean")
            p = phpa.get(f"{m}_mean")
            if h and p is not None and h != 0:
                diff_row[f"{m}_pct_diff(phpa_vs_hpa)"] = round((p - h) / h * 100, 2)
            else:
                diff_row[f"{m}_pct_diff(phpa_vs_hpa)"] = None
        pct_diff_rows.append(diff_row)

    return comparison_rows, pct_diff_rows


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 analyze_results.py <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    run_dirs = sorted(d for d in results_dir.iterdir() if d.is_dir())

    if not run_dirs:
        print(f"No run directories found under {results_dir}")
        sys.exit(1)

    summary_rows = [analyze_run(d) for d in run_dirs]

    summary_fields = ["run_id", "scenario", "autoscaler", "rep",
                       "mean_cpu_utilization_pct", "p95_latency_ms",
                       "throughput_rps", "error_rate_pct", "scale_up_events",
                       "mean_scaling_delay_s", "replica_minutes",
                       "cpu_waste_pct", "estimated_cost_usd",
                       "cost_per_successful_request_usd"]

    print("\n=== Step 23: Per-run summary ===\n")
    print_table(summary_rows, summary_fields)
    write_csv(summary_rows, results_dir / "summary.csv", summary_fields)

    comparison_rows, pct_diff_rows = build_comparison(summary_rows)

    print("\n=== Step 24: Comparison (mean/median/stddev per scenario+autoscaler) ===\n")
    comp_fields = list(comparison_rows[0].keys()) if comparison_rows else []
    print_table(comparison_rows, comp_fields)
    write_csv(comparison_rows, results_dir / "comparison.csv", comp_fields)

    print("\n=== Percentage difference (PHPA vs HPA) per scenario ===\n")
    if pct_diff_rows:
        diff_fields = list(pct_diff_rows[0].keys())
        print_table(pct_diff_rows, diff_fields)
        write_csv(pct_diff_rows, results_dir / "pct_diff.csv", diff_fields)

    print(f"\nCSV files written to {results_dir}/summary.csv, comparison.csv, pct_diff.csv")


if __name__ == "__main__":
    main()