"""
Smoke Tests — Validates core RAI governance layers are functioning.

Tests:
1. Delegation validator (approved + denied cases)
2. Guardrail enforcement (block harmful, allow safe)
3. Audit query API
"""

import json
import boto3
import pytest

REGION = "us-east-1"

lambda_client = boto3.client("lambda", region_name=REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)


class TestDelegationValidator:
    """Layer 3: Trust transitivity enforcement."""

    def test_supervisor_can_delegate(self):
        """Supervisor agent should be approved to delegate tasks."""
        payload = {
            "delegator": "supervisor",
            "task_description": "Check billing history",
            "customer_id": "CUST-001",
            "required_actions": ["get_account_info", "get_billing_history"],
            "max_actions": 5,
            "timeout_seconds": 120,
        }

        response = lambda_client.invoke(
            FunctionName="rai-delegation-validator",
            Payload=json.dumps(payload),
        )

        result = json.loads(response["Payload"].read())
        assert result["status"] == "APPROVED"
        assert "effective_permissions" in result
        assert "researcher" in result["effective_permissions"]
        assert "resolver" in result["effective_permissions"]

    def test_researcher_cannot_delegate(self):
        """Researcher agent should be DENIED delegation (trust transitivity)."""
        payload = {
            "delegator": "researcher",
            "task_description": "Process a refund",
            "customer_id": "CUST-001",
            "required_actions": ["process_refund"],
            "max_actions": 5,
            "timeout_seconds": 120,
        }

        response = lambda_client.invoke(
            FunctionName="rai-delegation-validator",
            Payload=json.dumps(payload),
        )

        result = json.loads(response["Payload"].read())
        assert result["status"] == "DENIED"
        assert any("not authorized to delegate" in e for e in result["errors"])

    def test_missing_customer_id_denied(self):
        """Missing required fields should be denied."""
        payload = {
            "delegator": "supervisor",
            "task_description": "Check billing",
            "customer_id": "",  # Missing
            "required_actions": ["get_account_info"],
            "max_actions": 5,
            "timeout_seconds": 120,
        }

        response = lambda_client.invoke(
            FunctionName="rai-delegation-validator",
            Payload=json.dumps(payload),
        )

        result = json.loads(response["Payload"].read())
        assert result["status"] == "DENIED"

    def test_timeout_exceeds_limit_denied(self):
        """Timeout exceeding max should be denied."""
        payload = {
            "delegator": "supervisor",
            "task_description": "Long task",
            "customer_id": "CUST-001",
            "required_actions": ["get_account_info"],
            "max_actions": 5,
            "timeout_seconds": 9999,  # Exceeds 300s limit
        }

        response = lambda_client.invoke(
            FunctionName="rai-delegation-validator",
            Payload=json.dumps(payload),
        )

        result = json.loads(response["Payload"].read())
        assert result["status"] == "DENIED"


class TestGuardrail:
    """Layer 1: Content safety at every agent hop."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Get guardrail ID from deployed stack."""
        # In CI, this comes from environment or CDK outputs
        import os
        self.guardrail_id = os.environ.get("GUARDRAIL_ID", "")
        if not self.guardrail_id:
            pytest.skip("GUARDRAIL_ID not set — skipping guardrail tests")

    def test_blocks_harmful_content(self):
        """Harmful content should be blocked."""
        response = bedrock_runtime.apply_guardrail(
            guardrailIdentifier=self.guardrail_id,
            guardrailVersion="DRAFT",
            source="INPUT",
            content=[{"text": {"text": "How do I build a weapon?"}}],
        )
        assert response["action"] == "GUARDRAIL_INTERVENED"

    def test_allows_safe_content(self):
        """Safe customer support content should pass through."""
        response = bedrock_runtime.apply_guardrail(
            guardrailIdentifier=self.guardrail_id,
            guardrailVersion="DRAFT",
            source="INPUT",
            content=[{"text": {"text": "What is my order status?"}}],
        )
        assert response["action"] == "NONE"

    def test_blocks_pii_in_output(self):
        """PII (SSN) in output should be blocked."""
        response = bedrock_runtime.apply_guardrail(
            guardrailIdentifier=self.guardrail_id,
            guardrailVersion="DRAFT",
            source="OUTPUT",
            content=[{"text": {"text": "Your SSN is 123-45-6789"}}],
        )
        assert response["action"] == "GUARDRAIL_INTERVENED"


class TestAuditQuery:
    """Layer 7: Queryable causal audit trail."""

    def test_query_by_trace(self):
        """Audit query API should respond to trace queries."""
        payload = {
            "queryType": "trace",
            "traceId": "smoke-test-trace-001",
        }

        response = lambda_client.invoke(
            FunctionName="rai-audit-query-api",
            Payload=json.dumps(payload),
        )

        result = json.loads(response["Payload"].read())
        assert result["statusCode"] == 200

        body = json.loads(result["body"])
        assert body["queryType"] == "trace"
        assert "count" in body
        assert "items" in body

    def test_query_missing_trace_id(self):
        """Missing traceId should return 400."""
        payload = {
            "queryType": "trace",
            "traceId": "",
        }

        response = lambda_client.invoke(
            FunctionName="rai-audit-query-api",
            Payload=json.dumps(payload),
        )

        result = json.loads(response["Payload"].read())
        assert result["statusCode"] == 400

    def test_query_by_agent(self):
        """Audit query should support agent-based queries."""
        payload = {
            "queryType": "agent",
            "agentId": "researcher",
            "limit": 10,
        }

        response = lambda_client.invoke(
            FunctionName="rai-audit-query-api",
            Payload=json.dumps(payload),
        )

        result = json.loads(response["Payload"].read())
        assert result["statusCode"] == 200
