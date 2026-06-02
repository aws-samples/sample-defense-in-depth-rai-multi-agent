"""
Agent Invoker Lambda — Invokes sub-agents via AgentCore Runtime
and records the delegation hop in the audit trail.

Each invocation:
1. Scopes the request to the effective permissions from the delegation contract
2. Invokes the target agent runtime
3. Records the hop (input, output, confidence, fidelity hash) in DynamoDB
4. Returns the agent's output for the next step in the chain
"""

import os
import json
import time
import hashlib
import logging
import boto3

logger = logging.getLogger()
logger.setLevel("INFO")

agentcore = boto3.client("bedrock-agentcore")
dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")

TARGET_AGENT = os.environ.get("TARGET_AGENT", "")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")


def compute_fidelity_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_confidence(output: str) -> float:
    """Extract confidence score from agent output."""
    for line in output.split("\n"):
        if line.strip().startswith("CONFIDENCE:"):
            try:
                return float(line.split(":")[1].strip())
            except (ValueError, IndexError):
                pass
    return 0.5  # Default confidence if not specified


def handler(event, context):
    """
    Input (from Step Functions):
    {
        "traceId": "...",
        "delegator": "supervisor",
        "task_description": "...",
        "customer_id": "CUST-001",
        "required_actions": [...],
        "effective_permissions": {...},
        "researcherResult": {...},  // Present if researcher already ran
        "sessionId": "..."
    }
    """
    logger.info(f"Agent invoker [{TARGET_AGENT}]: {json.dumps(event)}")

    trace_id = event.get("traceId", f"trace-{int(time.time())}")
    task_description = event.get("task_description", "")
    customer_id = event.get("customer_id", "")
    session_id = event.get("sessionId", f"session-{int(time.time())}")
    effective_perms = event.get("effective_permissions", {}).get(TARGET_AGENT, {})

    # Build the prompt for the target agent
    if TARGET_AGENT == "researcher":
        prompt = (
            f"Research the following for customer {customer_id}:\n"
            f"{task_description}\n\n"
            f"You are authorized to use these tools: {effective_perms.get('allowed_actions', [])}"
        )
        hop_index = 1
    elif TARGET_AGENT == "resolver":
        researcher_output = event.get("researcherResult", {}).get("output", "")
        prompt = (
            f"Based on the following research findings, take appropriate action for customer {customer_id}:\n\n"
            f"RESEARCH FINDINGS:\n{researcher_output}\n\n"
            f"ORIGINAL REQUEST:\n{task_description}\n\n"
            f"You are authorized to use these tools: {effective_perms.get('allowed_actions', [])}\n"
            f"Maximum refund amount: ${effective_perms.get('max_refund', 0)}"
        )
        hop_index = 3
    elif TARGET_AGENT == "reviewer":
        researcher_output = event.get("researcherResult", {}).get("output", "")
        resolver_output = event.get("resolverResult", {}).get("output", "")
        prompt = (
            f"Review the following agent chain output before delivery to the user:\n\n"
            f"ORIGINAL REQUEST:\n{task_description}\n\n"
            f"RESEARCHER OUTPUT:\n{researcher_output}\n\n"
            f"RESOLVER OUTPUT:\n{resolver_output}\n\n"
            f"Validate for factual consistency, safety, completeness, and tone."
        )
        hop_index = 5
    else:
        return {"output": f"Unknown target agent: {TARGET_AGENT}", "status": "ERROR"}

    # For this reference implementation, we simulate the agent invocation
    # In production, this would call agentcore.invoke_agent_runtime()
    # with the actual runtime ARN
    output = f"[{TARGET_AGENT}] Processed request for {customer_id}: {task_description[:100]}..."
    confidence = 0.85

    # Compute fidelity hash
    fidelity_hash = compute_fidelity_hash(output)

    # Record to audit trail
    if AUDIT_TABLE:
        table = dynamodb.Table(AUDIT_TABLE)
        table.put_item(
            Item={
                "traceId": trace_id,
                "hopIndex": hop_index,
                "sessionId": session_id,
                "agentId": TARGET_AGENT,
                "timestamp": int(time.time()),
                "eventType": "AGENT_INVOCATION",
                "input": prompt[:1000],  # Truncate for storage
                "output": output[:2000],
                "confidence": str(confidence),
                "fidelityHash": fidelity_hash,
                "effectivePermissions": json.dumps(effective_perms),
                "ttl": int(time.time()) + (365 * 24 * 3600),
            }
        )

    # Publish metrics
    cloudwatch.put_metric_data(
        Namespace="RAI/Delegation",
        MetricData=[{
            "MetricName": "AgentInvocations",
            "Value": 1.0,
            "Unit": "Count",
            "Dimensions": [{"Name": "Agent", "Value": TARGET_AGENT}],
        }],
    )

    return {
        "output": output,
        "confidence": confidence,
        "fidelityHash": fidelity_hash,
        "agentId": TARGET_AGENT,
        "hopIndex": hop_index,
        # Pass through context for downstream steps
        "traceId": trace_id,
        "task_description": task_description,
        "customer_id": customer_id,
        "sessionId": session_id,
        "effective_permissions": event.get("effective_permissions", {}),
    }
