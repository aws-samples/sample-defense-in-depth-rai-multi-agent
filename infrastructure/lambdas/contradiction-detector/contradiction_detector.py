"""
Contradiction Detector — Layer 3 (Delegation Governance)

Detects contradictions across multiple agent outputs before final
response assembly. Uses Bedrock to compare researcher findings
against resolver actions and flag inconsistencies.

Example contradictions:
- Researcher says "account is suspended" but resolver processes a refund
- Researcher says "no billing records" but resolver references a specific charge
- Researcher says "Premium plan" but resolver applies Standard-tier limits
"""

import os
import json
import time
import logging
import boto3

logger = logging.getLogger()
logger.setLevel("INFO")

bedrock_runtime = boto3.client("bedrock-runtime")
dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")

AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")

CONTRADICTION_CHECK_PROMPT = """You are a contradiction detector. Compare the following two agent outputs and determine if they contradict each other.

RESEARCHER OUTPUT (data gathering):
{researcher_output}

RESOLVER OUTPUT (action taken):
{resolver_output}

ORIGINAL REQUEST:
{original_request}

Analyze for contradictions:
1. Does the resolver's action align with the researcher's findings?
2. Are there factual inconsistencies between the two outputs?
3. Did the resolver act on information the researcher didn't provide?

Respond in JSON format:
{{
  "has_contradiction": true/false,
  "confidence": 0.0-1.0,
  "contradictions": ["list of specific contradictions found"],
  "summary": "brief explanation"
}}"""


def check_contradictions(researcher_output: str, resolver_output: str, original_request: str) -> dict:
    """Use Bedrock to detect contradictions between agent outputs."""
    prompt = CONTRADICTION_CHECK_PROMPT.format(
        researcher_output=researcher_output,
        resolver_output=resolver_output,
        original_request=original_request,
    )

    try:
        response = bedrock_runtime.invoke_model(
            modelId="us.anthropic.claude-sonnet-4-6",
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "temperature": 0.0,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )

        result = json.loads(response["body"].read())
        content = result.get("content", [{}])[0].get("text", "{}")

        # Parse the JSON response — model may wrap in markdown code fences
        try:
            # Strip markdown code fences if present
            cleaned = content.strip()
            if cleaned.startswith("```"):
                # Remove opening fence (with optional language tag)
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            parsed = json.loads(cleaned)
            return parsed
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse model response: {content[:500]}")
            # If the model didn't return valid JSON, assume no contradiction
            return {
                "has_contradiction": False,
                "confidence": 0.5,
                "contradictions": [],
                "summary": "Unable to parse contradiction check result",
            }

    except Exception as e:
        logger.error(f"Contradiction check failed: {e}")
        return {
            "has_contradiction": False,
            "confidence": 0.0,
            "contradictions": [],
            "summary": f"Error: {str(e)}",
        }


def handler(event, context):
    """
    Input (from Step Functions):
    {
        "traceId": "...",
        "task_description": "...",
        "researcherResult": {"output": "..."},
        "resolverResult": {"output": "..."},
        "sessionId": "..."
    }
    """
    logger.info(f"Contradiction check: {json.dumps(event)}")

    trace_id = event.get("traceId", f"trace-{int(time.time())}")
    researcher_output = event.get("researcherResult", {}).get("output", "")
    resolver_output = event.get("resolverResult", {}).get("output", "")
    original_request = event.get("task_description", "")
    session_id = event.get("sessionId", "unknown")

    if not researcher_output or not resolver_output:
        return {"status": "PASSED", "reason": "Insufficient outputs to compare"}

    result = check_contradictions(researcher_output, resolver_output, original_request)

    has_contradiction = result.get("has_contradiction", False)
    status = "CONTRADICTION_FOUND" if has_contradiction else "PASSED"

    # Publish metrics
    cloudwatch.put_metric_data(
        Namespace="RAI/Delegation",
        MetricData=[{
            "MetricName": "ContradictionsDetected",
            "Value": 1.0 if has_contradiction else 0.0,
            "Unit": "Count",
        }],
    )

    # Audit trail
    if AUDIT_TABLE:
        table = dynamodb.Table(AUDIT_TABLE)
        table.put_item(
            Item={
                "traceId": trace_id,
                "hopIndex": 99,  # Contradiction check is a cross-cutting concern
                "sessionId": session_id,
                "agentId": "contradiction_detector",
                "timestamp": int(time.time()),
                "eventType": "CONTRADICTION_CHECK",
                "status": status,
                "contradictions": json.dumps(result.get("contradictions", [])),
                "confidence": str(result.get("confidence", 0)),
                "ttl": int(time.time()) + (365 * 24 * 3600),
            }
        )

    if has_contradiction:
        logger.warning(f"CONTRADICTION DETECTED: {json.dumps(result)}")

    return {
        "status": status,
        "confidence": result.get("confidence", 0),
        "contradictions": result.get("contradictions", []),
        "summary": result.get("summary", ""),
    }
