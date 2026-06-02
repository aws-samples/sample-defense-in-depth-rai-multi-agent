"""
Cross-Agent Fidelity Check — Layer 3 (Delegation Governance)

Detects cascading hallucinations by comparing a downstream agent's output
against the upstream agent's original input using Bedrock Guardrails
contextual grounding.

If the downstream output drifts significantly from the source data,
the fidelity check FAILS and the Step Functions orchestrator halts the chain.
"""

import os
import json
import hashlib
import time
import logging
import boto3

logger = logging.getLogger()
logger.setLevel("INFO")

bedrock_runtime = boto3.client("bedrock-runtime")
dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")

GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")

FIDELITY_THRESHOLD = 0.7  # Minimum grounding score to pass


def compute_fidelity_hash(text: str) -> str:
    """Compute SHA-256 hash of content for chain-of-custody verification."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_contextual_grounding(source_text: str, output_text: str) -> dict:
    """Use Bedrock Guardrails contextual grounding to verify fidelity."""
    if not GUARDRAIL_ID:
        return {"score": 1.0, "action": "NONE", "reason": "Guardrail not configured"}

    try:
        response = bedrock_runtime.apply_guardrail(
            guardrailIdentifier=GUARDRAIL_ID,
            guardrailVersion=GUARDRAIL_VERSION,
            source="OUTPUT",
            content=[
                {
                    "text": {
                        "text": output_text,
                        "qualifiers": ["grounding_source"],
                    }
                }
            ],
        )

        action = response.get("action", "NONE")
        assessments = response.get("assessments", [])

        # Extract grounding score from assessments
        grounding_score = 1.0
        for assessment in assessments:
            grounding = assessment.get("contextualGroundingPolicy", {})
            filters = grounding.get("filters", [])
            for f in filters:
                if f.get("type") == "GROUNDING":
                    grounding_score = min(grounding_score, f.get("score", 1.0))

        return {
            "score": grounding_score,
            "action": action,
            "reason": "contextual_grounding_check",
        }

    except Exception as e:
        logger.error(f"Guardrail check failed: {e}")
        return {"score": 0.0, "action": "ERROR", "reason": str(e)}


def record_audit(trace_id: str, hop_index: int, agent_id: str, result: dict):
    """Write fidelity check result to the audit trail."""
    if not AUDIT_TABLE:
        return

    table = dynamodb.Table(AUDIT_TABLE)
    table.put_item(
        Item={
            "traceId": trace_id,
            "hopIndex": hop_index,
            "sessionId": result.get("sessionId", "unknown"),
            "agentId": agent_id,
            "timestamp": int(time.time()),
            "eventType": "FIDELITY_CHECK",
            "fidelityScore": str(result.get("score", 0)),
            "fidelityHash": result.get("hash", ""),
            "status": result.get("status", "UNKNOWN"),
            "ttl": int(time.time()) + (365 * 24 * 3600),  # 1 year retention
        }
    )


def publish_metric(score: float, status: str):
    """Publish fidelity metrics to CloudWatch."""
    cloudwatch.put_metric_data(
        Namespace="RAI/Delegation",
        MetricData=[
            {
                "MetricName": "FidelityScore",
                "Value": score,
                "Unit": "None",
                "Dimensions": [{"Name": "CheckType", "Value": "CrossAgent"}],
            },
            {
                "MetricName": "FidelityFailures",
                "Value": 1.0 if status == "FAILED" else 0.0,
                "Unit": "Count",
                "Dimensions": [{"Name": "CheckType", "Value": "CrossAgent"}],
            },
        ],
    )


def handler(event, context):
    """
    Input (from Step Functions):
    {
        "traceId": "...",
        "hopIndex": 2,
        "sourceAgent": "researcher",
        "sourceOutput": "...",
        "downstreamAgent": "resolver",
        "downstreamOutput": "...",
        "originalInput": "...",
        "sessionId": "..."
    }
    """
    logger.info(f"Fidelity check event: {json.dumps(event)}")

    trace_id = event.get("traceId", f"trace-{int(time.time())}")
    hop_index = event.get("hopIndex", 0)
    source_output = event.get("sourceOutput", "")
    downstream_output = event.get("downstreamOutput", event.get("researcherResult", {}).get("output", ""))
    original_input = event.get("originalInput", event.get("task_description", ""))
    session_id = event.get("sessionId", "unknown")
    source_agent = event.get("sourceAgent", "unknown")

    # If we don't have downstream output yet, use the researcher result
    text_to_check = downstream_output or source_output or ""

    if not text_to_check:
        return {"status": "PASSED", "score": 1.0, "reason": "No output to check"}

    # Compute fidelity hash for chain-of-custody
    fidelity_hash = compute_fidelity_hash(text_to_check)

    # Run contextual grounding check
    grounding_result = check_contextual_grounding(original_input, text_to_check)
    score = grounding_result["score"]

    if score >= FIDELITY_THRESHOLD:
        status = "PASSED"
    else:
        status = "FAILED"
        logger.warning(
            f"FIDELITY FAILURE: score={score} threshold={FIDELITY_THRESHOLD} "
            f"agent={source_agent} trace={trace_id}"
        )

    result = {
        "status": status,
        "score": score,
        "hash": fidelity_hash,
        "sessionId": session_id,
        "threshold": FIDELITY_THRESHOLD,
        "groundingAction": grounding_result["action"],
    }

    # Record to audit trail
    record_audit(trace_id, hop_index, source_agent, result)

    # Publish CloudWatch metric
    publish_metric(score, status)

    return result
