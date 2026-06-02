"""
Audit Query API — Layer 7 (Observability)

Queryable causal audit trail: "Show me every decision chain that led to this output"

Supports queries:
- By trace ID: full delegation chain for a specific request
- By session ID: all activity in a user session
- By agent ID: all invocations of a specific agent
- By time range: all activity in a time window
"""

import os
import json
import logging
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel("INFO")

dynamodb = boto3.resource("dynamodb")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE", "")


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def query_by_trace(table, trace_id: str) -> list:
    """Get the full delegation chain for a trace."""
    response = table.query(
        KeyConditionExpression=Key("traceId").eq(trace_id),
    )
    items = response.get("Items", [])
    return sorted(items, key=lambda x: x.get("hopIndex", 0))


def query_by_session(table, session_id: str) -> list:
    """Get all activity in a session."""
    response = table.query(
        IndexName="by-session",
        KeyConditionExpression=Key("sessionId").eq(session_id),
    )
    return response.get("Items", [])


def query_by_agent(table, agent_id: str, limit: int = 50) -> list:
    """Get recent invocations of a specific agent."""
    response = table.query(
        IndexName="by-agent",
        KeyConditionExpression=Key("agentId").eq(agent_id),
        Limit=limit,
        ScanIndexForward=False,  # Most recent first
    )
    return response.get("Items", [])


def handler(event, context):
    """
    Input (via API Gateway or direct invocation):
    {
        "queryType": "trace" | "session" | "agent",
        "traceId": "...",       // for queryType=trace
        "sessionId": "...",     // for queryType=session
        "agentId": "...",       // for queryType=agent
        "limit": 50             // optional
    }
    """
    logger.info(f"Audit query: {json.dumps(event)}")

    if not AUDIT_TABLE:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Audit table not configured"}),
        }

    table = dynamodb.Table(AUDIT_TABLE)
    query_type = event.get("queryType", "trace")
    limit = event.get("limit", 50)

    if query_type == "trace":
        trace_id = event.get("traceId", "")
        if not trace_id:
            return {"statusCode": 400, "body": json.dumps({"error": "traceId required"})}
        items = query_by_trace(table, trace_id)

    elif query_type == "session":
        session_id = event.get("sessionId", "")
        if not session_id:
            return {"statusCode": 400, "body": json.dumps({"error": "sessionId required"})}
        items = query_by_session(table, session_id)

    elif query_type == "agent":
        agent_id = event.get("agentId", "")
        if not agent_id:
            return {"statusCode": 400, "body": json.dumps({"error": "agentId required"})}
        items = query_by_agent(table, agent_id, limit)

    else:
        return {"statusCode": 400, "body": json.dumps({"error": f"Unknown queryType: {query_type}"})}

    return {
        "statusCode": 200,
        "body": json.dumps({
            "queryType": query_type,
            "count": len(items),
            "items": items,
        }, cls=DecimalEncoder),
    }
