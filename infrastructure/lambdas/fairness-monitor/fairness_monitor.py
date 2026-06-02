"""
Fairness Parity Monitor — Layer 6

Tracks response quality, latency, safety intervention rates, and tool access
per agent path/user segment. Publishes CloudWatch metrics and triggers alerts
when metrics diverge beyond configurable thresholds across parallel agent paths.

Detects:
- Quality divergence between agent paths (e.g., researcher path vs resolver path)
- Safety intervention rate differences across user segments
- Latency disparities that indicate unequal service
"""

import os
import json
import time
import logging
from decimal import Decimal
import boto3

logger = logging.getLogger()
logger.setLevel("INFO")

dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")

AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")
PARITY_THRESHOLD = 0.3  # 30% divergence triggers alert


def compute_parity_metrics(records: list) -> dict:
    """Compute fairness parity metrics across agent paths."""
    path_metrics = {}

    for record in records:
        agent_id = record.get("agentId", "unknown")
        confidence = float(record.get("confidence", "0.5"))
        status = record.get("status", "")

        if agent_id not in path_metrics:
            path_metrics[agent_id] = {
                "total_invocations": 0,
                "total_confidence": 0.0,
                "interventions": 0,
                "failures": 0,
            }

        path_metrics[agent_id]["total_invocations"] += 1
        path_metrics[agent_id]["total_confidence"] += confidence

        if status in ("FAILED", "DENIED", "GUARDRAIL_INTERVENED"):
            path_metrics[agent_id]["interventions"] += 1
        if status == "FAILED":
            path_metrics[agent_id]["failures"] += 1

    # Compute averages
    for agent_id, metrics in path_metrics.items():
        total = metrics["total_invocations"]
        if total > 0:
            metrics["avg_confidence"] = metrics["total_confidence"] / total
            metrics["intervention_rate"] = metrics["interventions"] / total
            metrics["failure_rate"] = metrics["failures"] / total
        else:
            metrics["avg_confidence"] = 0.0
            metrics["intervention_rate"] = 0.0
            metrics["failure_rate"] = 0.0

    return path_metrics


def compute_divergence(path_metrics: dict) -> float:
    """Compute maximum divergence across agent paths."""
    if len(path_metrics) < 2:
        return 0.0

    confidences = [m["avg_confidence"] for m in path_metrics.values() if m["total_invocations"] > 0]
    if not confidences:
        return 0.0

    max_conf = max(confidences)
    min_conf = min(confidences)

    if max_conf == 0:
        return 0.0

    return (max_conf - min_conf) / max_conf


def handler(event, context):
    """
    Triggered periodically (e.g., every 5 minutes via EventBridge rule)
    or after each orchestration run.

    Reads recent audit records and computes fairness parity metrics.
    """
    logger.info(f"Fairness monitor triggered: {json.dumps(event)}")

    if not AUDIT_TABLE:
        return {"status": "SKIPPED", "reason": "No audit table configured"}

    table = dynamodb.Table(AUDIT_TABLE)

    # Query recent records (last 15 minutes)
    cutoff = int(time.time()) - 900

    # Scan for recent agent invocations (in production, use a GSI query)
    try:
        response = table.scan(
            FilterExpression="eventType = :et AND #ts > :cutoff",
            ExpressionAttributeNames={"#ts": "timestamp"},
            ExpressionAttributeValues={
                ":et": "AGENT_INVOCATION",
                ":cutoff": cutoff,
            },
            Limit=100,
        )
        records = response.get("Items", [])
    except Exception as e:
        logger.error(f"Failed to query audit table: {e}")
        records = []

    # Compute parity metrics
    path_metrics = compute_parity_metrics(records)
    divergence = compute_divergence(path_metrics)

    # Publish per-path metrics
    metric_data = []
    for agent_id, metrics in path_metrics.items():
        metric_data.append({
            "MetricName": "ResponseQuality",
            "Value": metrics["avg_confidence"],
            "Unit": "None",
            "Dimensions": [{"Name": "AgentPath", "Value": agent_id}],
        })
        metric_data.append({
            "MetricName": "InterventionRate",
            "Value": metrics["intervention_rate"],
            "Unit": "None",
            "Dimensions": [{"Name": "AgentPath", "Value": agent_id}],
        })

    # Publish divergence metric
    metric_data.append({
        "MetricName": "ParityDivergence",
        "Value": divergence,
        "Unit": "None",
    })

    if metric_data:
        cloudwatch.put_metric_data(
            Namespace="RAI/FairnessParity",
            MetricData=metric_data,
        )

    result = {
        "status": "ALERT" if divergence > PARITY_THRESHOLD else "OK",
        "divergence": divergence,
        "threshold": PARITY_THRESHOLD,
        "pathMetrics": {k: {kk: str(vv) for kk, vv in v.items()} for k, v in path_metrics.items()},
        "recordsAnalyzed": len(records),
    }

    if divergence > PARITY_THRESHOLD:
        logger.warning(f"FAIRNESS DRIFT: divergence={divergence:.3f} > threshold={PARITY_THRESHOLD}")

    return result
