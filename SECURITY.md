# Security

## Reporting Security Issues

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.

## Security Architecture

This solution implements defense-in-depth security across 7 governance layers for multi-agent AI systems.

### Trust Transitivity Enforcement via Cedar Policies

Cedar policies enforce that a delegated agent's effective permissions are the **intersection** of its own permissions and the delegating agent's permissions. This prevents privilege escalation through delegation chains:

- Sub-agents can never exceed the delegator's scope
- Circular delegation is prevented by role-based delegation rules
- All delegation decisions are recorded in the immutable audit trail

### PII Redaction at Every Agent Hop

PII is redacted at multiple enforcement points:

1. **Bedrock Guardrails** — SSN blocked, credit cards/emails/phones/names anonymized at every agent hop
2. **Gateway Request Interceptor** — PII patterns redacted from inbound tool arguments
3. **Gateway Response Interceptor** — PII patterns redacted from outbound responses
4. **Agent System Prompts** — Agents instructed to use `[REDACTED]` placeholders

### Least-Privilege Delegation (Permission Intersection)

The delegation validator Lambda enforces:

- `effective_permissions = delegator_permissions ∩ delegate_own_permissions`
- Maximum action counts per delegation contract
- Timeout bounds on delegated tasks
- Required fields validation before any delegation proceeds

### IAM Permissions Boundaries on Agent Runtimes

Each AgentCore Runtime operates in an isolated MicroVM with:

- Scoped IAM role (only `bedrock:InvokeModel` and guardrail apply)
- No cross-agent network access (agents communicate only via Step Functions)
- Environment-variable-based configuration (no secrets in code)

### Cognito OAuth 2.0 with Custom Scopes

- Custom resource server with `agent/read`, `agent/write`, `agent/escalate` scopes
- Authorization code grant flow (no implicit grants)
- Strong password policy (12+ chars, mixed case, digits, symbols)
- Gateway validates JWT and enforces scope-based access at the Cedar policy layer

### DynamoDB Audit Trail with PITR for Regulatory Retention

- Point-in-Time Recovery enabled for immutable audit
- TTL set to 1 year for regulatory retention
- GSIs for query by session, agent, or trace
- Every delegation decision, agent invocation, fidelity check, and contradiction detection is recorded

### Accepted Security Debt (Demo Context)

The following are known limitations accepted for this reference implementation:

| Item | Risk | Mitigation Path for Production |
|------|------|-------------------------------|
| Agent invoker simulates runtime calls | No actual MicroVM invocation in demo | Replace with `agentcore.invoke_agent_runtime()` |
| Cognito callback URL is localhost | Only works for local testing | Update to production domain |
| No WAF on Gateway | Gateway exposed without rate limiting | Add AWS WAF with rate-based rules |
| No VPC endpoints | Lambda calls traverse public internet | Deploy in VPC with PrivateLink endpoints |
| SNS topic has no subscription | Alerts are published but not delivered | Add email/PagerDuty subscription |
| Cedar policies use broad resource patterns | Policies reference gateway ARN but not specific tools | Narrow to specific tool ARNs in production |
| No secrets rotation | Static OAuth client secret | Enable Secrets Manager rotation |
| DynamoDB table uses PAY_PER_REQUEST | No provisioned capacity limits | Add auto-scaling or provisioned mode with limits |
| Lambda handlers log full event payloads | Sensitive data may appear in CloudWatch Logs | Use selective field logging; exclude PII fields from log statements |
| Bedrock IAM policies use wildcard resources | `bedrock:InvokeModel` on `foundation-model/*` | Scope to specific model ARNs used by the solution |
| SNS topic missing KMS encryption | Messages at rest use default encryption | Add `masterKey` property to SNS construct |
| CloudWatch Logs use default encryption | Log data uses AWS-managed keys | Add `encryptionKey` with customer-managed KMS key |

## Dependencies

This project uses the following security-relevant dependencies:

- **AWS CDK** — Infrastructure as Code (pinned version)
- **Amazon Bedrock Guardrails** — Content safety enforcement
- **Amazon Bedrock AgentCore** — Isolated agent runtimes
- **Cedar Policy Language** — Fine-grained authorization
- **AWS Step Functions** — Deterministic orchestration with halt-on-failure
- **Amazon Cognito** — OAuth 2.0 identity provider
- **Amazon DynamoDB** — Immutable audit trail with PITR
