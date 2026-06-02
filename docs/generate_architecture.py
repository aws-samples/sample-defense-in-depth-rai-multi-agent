"""
Architecture diagram — Left-to-Right workflow showing the exact request path:
Customer → Auth → Gateway → Guardrail → Supervisor Runtime → Step Functions → Sub-Agents → Response
"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.ml import Bedrock
from diagrams.aws.compute import Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.integration import StepFunctions, SimpleNotificationServiceSns as SNS
from diagrams.aws.security import Cognito, IAM
from diagrams.aws.management import Cloudwatch, CloudwatchAlarm
from diagrams.aws.general import User
import os

output_path = os.path.join(os.path.dirname(__file__), "screenshots", "architecture")

graph_attr = {
    "fontsize": "12",
    "bgcolor": "white",
    "pad": "0.8",
    "nodesep": "0.5",
    "ranksep": "1.0",
    "dpi": "150",
    "label": "",
}

with Diagram(
    "Defense-in-Depth RAI — Multi-Agent Request Flow",
    filename=output_path,
    show=False,
    direction="LR",  # LEFT TO RIGHT
    graph_attr=graph_attr,
):

    # ===== STEP 1: Customer =====
    customer = User("Customer")

    with Cluster("AWS Cloud"):

        # ===== STEP 2: Authentication =====
        with Cluster("① Authenticate"):
            cognito = Cognito("Cognito\nOAuth 2.0\n(JWT + scopes)")

        # ===== STEP 3: Gateway + Policy =====
        with Cluster("② Authorize (Gateway)"):
            gateway = Bedrock("AgentCore\nGateway")
            req_int = Lambda("Request\nInterceptor\n• Validate JWT\n• Check scopes\n• Redact PII")
            cedar = Bedrock("Cedar Policy\n• Trust transitivity\n• Scope enforcement\n• Refund limit")

        # ===== STEP 4: Content Safety =====
        with Cluster("③ Content Safety"):
            guardrail = Bedrock("Bedrock\nGuardrail\n• Hate/Violence\n• PII block\n• Denied topics\n• Prompt attack")

        # ===== STEP 5: Supervisor Agent =====
        with Cluster("④ Supervisor Agent"):
            supervisor = Bedrock("Supervisor\nRuntime\n(MicroVM)\n• Routes request\n• Creates contract")

        # ===== STEP 6: Orchestration =====
        with Cluster("⑤ Delegation Governance (Step Functions)"):
            sfn = StepFunctions("Orchestrator")

            with Cluster("Governance Gates"):
                delegation_val = Lambda("Validate\nDelegation\n• perms = own ∩\n  delegator's")
                fidelity = Lambda("Fidelity\nCheck\n• Grounding\n• Hash chain\n• HALT if drift")
                contradiction = Lambda("Contradiction\nDetector\n• Cross-agent\n  consistency")

            with Cluster("Sub-Agents (MicroVMs)"):
                researcher = Bedrock("Researcher\n• get_account\n• get_billing\n• get_history")
                resolver = Bedrock("Resolver\n• process_refund\n• create_ticket\n• update_status")
                reviewer = Bedrock("Reviewer\n• Validate output\n• Check safety\n• Check tone")

        # ===== STEP 7: Response =====
        with Cluster("⑥ Response"):
            resp_int = Lambda("Response\nInterceptor\n• Filter tools\n• Redact PII\n• Add audit")

        # ===== STEP 8: Observability =====
        with Cluster("⑦ Observability (continuous)"):
            dynamo = Dynamodb("DynamoDB\nAudit Trail\n(every hop)")
            dashboard = Cloudwatch("Dashboard\n+ 4 Alarms")
            sns = SNS("SNS\nAlerts")
            fairness = Lambda("Fairness\nMonitor")

    # ===== WORKFLOW EDGES (Left to Right) =====

    # Customer → Auth
    customer >> Edge(label="1. Login", color="darkgreen", style="bold") >> cognito

    # Auth → Gateway
    cognito >> Edge(label="2. JWT token", color="darkgreen") >> gateway

    # Gateway → Interceptor → Cedar
    gateway >> Edge(label="3. Validate", color="orange") >> req_int
    req_int >> Edge(label="4. Enforce\npolicy", color="orange") >> cedar

    # Cedar → Guardrail
    cedar >> Edge(label="5. Authorized\nrequest", color="blue", style="bold") >> guardrail

    # Guardrail → Supervisor
    guardrail >> Edge(label="6. Safe\ncontent", color="blue", style="bold") >> supervisor

    # Supervisor → Step Functions
    supervisor >> Edge(label="7. Delegation\ncontract", color="purple", style="bold") >> sfn

    # Step Functions → Governance Gates → Sub-Agents
    sfn >> Edge(label="8. Validate", color="red") >> delegation_val
    delegation_val >> Edge(label="9. APPROVED", color="green") >> researcher
    researcher >> Edge(label="10. Output", color="purple") >> fidelity
    fidelity >> Edge(label="11. PASSED", color="green") >> resolver
    resolver >> Edge(label="12. Output", color="purple") >> contradiction
    contradiction >> Edge(label="13. No conflicts", color="green") >> reviewer

    # Reviewer → Response
    reviewer >> Edge(label="14. Validated\nresponse", color="blue", style="bold") >> resp_int
    resp_int >> Edge(label="15. Clean\nresponse", color="blue", style="bold") >> customer

    # Audit (every governance Lambda writes)
    delegation_val >> Edge(style="dotted", color="gray") >> dynamo
    fidelity >> Edge(style="dotted", color="gray") >> dynamo
    researcher >> Edge(style="dotted", color="gray") >> dynamo
    contradiction >> Edge(style="dotted", color="gray") >> dynamo

    # Monitoring
    fairness >> Edge(style="dotted", color="teal") >> dashboard
    dashboard >> Edge(color="red") >> sns
