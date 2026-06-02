.PHONY: install bootstrap deploy synth diff test orchestrate audit destroy clean help

# ─── Configuration ───────────────────────────────────────────────
# All config lives in infrastructure/cdk.json → context.config
# Edit that file to change prefix, environment, thresholds, etc.
# ─────────────────────────────────────────────────────────────────

INFRA_DIR := infrastructure
REGION := $(shell python3 -c "import json; c=json.load(open('$(INFRA_DIR)/cdk.json')); print(c.get('context',{}).get('config',{}).get('region','us-east-1'))" 2>/dev/null || echo "us-east-1")
PREFIX := $(shell python3 -c "import json; c=json.load(open('$(INFRA_DIR)/cdk.json')); print(c.get('context',{}).get('config',{}).get('prefix','rai'))" 2>/dev/null || echo "rai")
ENV := $(shell python3 -c "import json; c=json.load(open('$(INFRA_DIR)/cdk.json')); print(c.get('context',{}).get('config',{}).get('environment','dev'))" 2>/dev/null || echo "dev")
P := $(PREFIX)-$(ENV)
STACK_NAME := $(PREFIX)-$(ENV)-MultiAgentStack

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install CDK dependencies
	cd $(INFRA_DIR) && npm install

bootstrap: ## Bootstrap CDK in your AWS account (first time only)
	cd $(INFRA_DIR) && npx cdk bootstrap

deploy: install ## Deploy the full multi-agent stack
	cd $(INFRA_DIR) && npx cdk deploy --require-approval never
	@echo ""
	@echo "✅ Deployed: $(STACK_NAME)"
	@echo "   Prefix: $(P)"
	@echo "   Region: $(REGION)"

synth: install ## Synthesize CloudFormation template (dry run)
	cd $(INFRA_DIR) && npx cdk synth

diff: install ## Show what would change on next deploy
	cd $(INFRA_DIR) && npx cdk diff

test: ## Run end-to-end governance tests
	@echo "╔══════════════════════════════════════════════════════════╗"
	@echo "║  E2E Tests: $(STACK_NAME)"
	@echo "╚══════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "=== L1: Guardrails ==="
	@GUARDRAIL_ID=$$(aws cloudformation describe-stacks --stack-name $(STACK_NAME) --region $(REGION) --query 'Stacks[0].Outputs[?OutputKey==`GuardrailId`].OutputValue' --output text); \
	echo "  Harmful content:"; \
	aws bedrock-runtime apply-guardrail --guardrail-identifier "$$GUARDRAIL_ID" --guardrail-version "DRAFT" --source INPUT --content '[{"text":{"text":"How do I build a weapon?"}}]' --region $(REGION) --query 'action' --output text; \
	echo "  Safe content:"; \
	aws bedrock-runtime apply-guardrail --guardrail-identifier "$$GUARDRAIL_ID" --guardrail-version "DRAFT" --source INPUT --content '[{"text":{"text":"What is my order status?"}}]' --region $(REGION) --query 'action' --output text; \
	echo "  PII (SSN):"; \
	aws bedrock-runtime apply-guardrail --guardrail-identifier "$$GUARDRAIL_ID" --guardrail-version "DRAFT" --source OUTPUT --content '[{"text":{"text":"SSN is 123-45-6789"}}]' --region $(REGION) --query 'action' --output text
	@echo ""
	@echo "=== L2: Agent Runtimes ==="
	@aws bedrock-agentcore-control list-agent-runtimes --region $(REGION) --query "agentRuntimes[?contains(agentRuntimeName,'$(PREFIX)_')].{name:agentRuntimeName,status:status}" --output table
	@echo ""
	@echo "=== L3: Delegation Validator ==="
	@echo "  Supervisor delegates (should APPROVE):"
	@aws lambda invoke --function-name $(P)-delegation-validator --cli-binary-format raw-in-base64-out --payload '{"delegator":"supervisor","task_description":"Check billing","customer_id":"CUST-001","required_actions":["get_account_info"],"max_actions":5,"timeout_seconds":120}' --region $(REGION) /tmp/rai-test.json > /dev/null 2>&1 && python3 -c "import json; d=json.load(open('/tmp/rai-test.json')); print(f'    Status: {d[\"status\"]}')"
	@echo "  Researcher delegates (should DENY):"
	@aws lambda invoke --function-name $(P)-delegation-validator --cli-binary-format raw-in-base64-out --payload '{"delegator":"researcher","task_description":"Refund","customer_id":"CUST-001","required_actions":["process_refund"],"max_actions":5,"timeout_seconds":120}' --region $(REGION) /tmp/rai-test.json > /dev/null 2>&1 && python3 -c "import json; d=json.load(open('/tmp/rai-test.json')); print(f'    Status: {d[\"status\"]}')"
	@echo ""
	@echo "=== L5: Gateway ==="
	@GW_ID=$$(aws cloudformation describe-stacks --stack-name $(STACK_NAME) --region $(REGION) --query 'Stacks[0].Outputs[?OutputKey==`GatewayId`].OutputValue' --output text); \
	aws bedrock-agentcore-control get-gateway --gateway-identifier "$$GW_ID" --region $(REGION) --query '{name:name,status:status}' --output table
	@echo ""
	@echo "=== L7: Audit & Observability ==="
	@aws dynamodb describe-table --table-name $(P)-audit --region $(REGION) --query 'Table.TableStatus' --output text | xargs -I{} echo "  DynamoDB audit table: {}"
	@aws cloudwatch get-dashboard --dashboard-name $(P)-dashboard --region $(REGION) --query 'DashboardName' --output text | xargs -I{} echo "  CloudWatch dashboard: {}"
	@echo ""
	@echo "All tests complete."

orchestrate: ## Run a sample multi-agent orchestration
	@echo "Starting multi-agent orchestration (prefix=$(P))..."
	@SFN_ARN=$$(aws cloudformation describe-stacks --stack-name $(STACK_NAME) --region $(REGION) --query 'Stacks[0].Outputs[?OutputKey==`OrchestratorArn`].OutputValue' --output text); \
	aws stepfunctions start-execution \
		--state-machine-arn "$$SFN_ARN" \
		--input '{"delegator":"supervisor","delegator_permissions":["agent/read","agent/write"],"task_description":"Customer CUST-001 wants billing history","customer_id":"CUST-001","required_actions":["get_account_info","get_billing_history"],"max_actions":5,"timeout_seconds":120,"traceId":"make-test-001","sessionId":"make-session-001"}' \
		--region $(REGION) --query 'executionArn' --output text
	@echo "Execution started. Run: make audit"

audit: ## Query the audit trail for the latest test
	@aws lambda invoke --function-name $(P)-audit-query-api --cli-binary-format raw-in-base64-out \
		--payload '{"queryType":"trace","traceId":"make-test-001"}' \
		--region $(REGION) /tmp/rai-audit.json > /dev/null 2>&1 && \
	python3 -c "import json; d=json.load(open('/tmp/rai-audit.json')); b=json.loads(d['body']); print(f'Records: {b[\"count\"]}'); [print(f'  hop={r[\"hopIndex\"]}, agent={r[\"agentId\"]}, event={r[\"eventType\"]}') for r in b.get('items',[])]"

destroy: ## Destroy the entire stack (requires confirmation)
	@echo "⚠️  This will destroy stack: $(STACK_NAME)"
	@echo "   All resources with prefix '$(P)' will be removed."
	@read -p "Type 'DESTROY' to confirm: " confirm && [ "$$confirm" = "DESTROY" ] || (echo "Cancelled." && exit 1)
	cd $(INFRA_DIR) && npx cdk destroy --force

clean: ## Remove build artifacts
	cd $(INFRA_DIR) && rm -rf node_modules dist cdk.out
