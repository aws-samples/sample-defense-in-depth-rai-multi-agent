import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as sns from "aws-cdk-lib/aws-sns";
import * as kms from "aws-cdk-lib/aws-kms";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatch_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as logs from "aws-cdk-lib/aws-logs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as path from "path";
import * as fs from "fs";
import { Construct } from "constructs";
import * as bedrock from "@aws-cdk/aws-bedrock-alpha";
import * as agentcore from "@aws-cdk/aws-bedrock-agentcore-alpha";
import {
  aws_bedrockagentcore as bedrockagentcore,
} from "aws-cdk-lib";

export class ResponsibleAiMultiAgentStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ================================================================
    // Configuration — all tunables come from cdk.json → context.config
    // Edit cdk.json to change prefix, thresholds, region, etc.
    // ================================================================
    const config = this.node.tryGetContext("config") || {};
    const prefix = config.prefix || "rai";
    const env = config.environment || "dev";
    const p = `${prefix}-${env}`; // Resource name prefix, e.g. "rai-dev"

    const guardrailConfig = config.guardrail || {};
    const groundingThreshold = guardrailConfig.groundingThreshold ?? 0.7;
    const relevanceThreshold = guardrailConfig.relevanceThreshold ?? 0.7;

    const delegationConfig = config.delegation || {};
    const fidelityThreshold = delegationConfig.fidelityThreshold ?? 0.7;

    const fairnessConfig = config.fairness || {};
    const parityDivergenceThreshold = fairnessConfig.parityDivergenceThreshold ?? 0.3;

    const alarmsConfig = config.alarms || {};
    const guardrailInterventionThreshold = alarmsConfig.guardrailInterventionThreshold ?? 50;
    const fidelityFailureThreshold = alarmsConfig.fidelityFailureThreshold ?? 1;
    const trustViolationThreshold = alarmsConfig.trustViolationThreshold ?? 1;

    // ================================================================
    // Shared KMS key for encryption at rest (DynamoDB, Logs, SNS)
    // Addresses cfn-nag W74, W84, W47
    // ================================================================
    const encryptionKey = new kms.Key(this, "RaiEncryptionKey", {
      alias: `${p}-encryption-key`,
      description: "Encryption key for RAI multi-agent governance resources",
      enableKeyRotation: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Grant CloudWatch Logs permission to use the KMS key
    encryptionKey.addToResourcePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal(`logs.${this.region}.amazonaws.com`)],
        actions: [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*",
        ],
        resources: ["*"],
        conditions: {
          ArnLike: {
            "kms:EncryptionContext:aws:logs:arn": `arn:aws:logs:${this.region}:${this.account}:log-group:/${p}/*`,
          },
        },
      })
    );

    // ================================================================
    // LAYER 1: Content Safety — Amazon Bedrock Guardrails
    // Applied at every agent hop (supervisor + each sub-agent)
    // ================================================================

    const guardrail = new bedrock.Guardrail(this, "MultiAgentGuardrail", {
      guardrailName: `${p}-guardrail`,
      description:
        "Content safety guardrail applied at every agent hop in the multi-agent chain",
      blockedInputMessaging:
        "This request falls outside safety guidelines and has been blocked.",
      blockedOutputsMessaging:
        "This response falls outside safety guidelines and has been blocked.",

      contentFilters: [
        {
          type: bedrock.ContentFilterType.HATE,
          inputStrength: bedrock.ContentFilterStrength.HIGH,
          outputStrength: bedrock.ContentFilterStrength.HIGH,
        },
        {
          type: bedrock.ContentFilterType.VIOLENCE,
          inputStrength: bedrock.ContentFilterStrength.HIGH,
          outputStrength: bedrock.ContentFilterStrength.HIGH,
        },
        {
          type: bedrock.ContentFilterType.SEXUAL,
          inputStrength: bedrock.ContentFilterStrength.HIGH,
          outputStrength: bedrock.ContentFilterStrength.HIGH,
        },
        {
          type: bedrock.ContentFilterType.MISCONDUCT,
          inputStrength: bedrock.ContentFilterStrength.HIGH,
          outputStrength: bedrock.ContentFilterStrength.HIGH,
        },
        {
          type: bedrock.ContentFilterType.INSULTS,
          inputStrength: bedrock.ContentFilterStrength.HIGH,
          outputStrength: bedrock.ContentFilterStrength.HIGH,
        },
        {
          type: bedrock.ContentFilterType.PROMPT_ATTACK,
          inputStrength: bedrock.ContentFilterStrength.HIGH,
          outputStrength: bedrock.ContentFilterStrength.NONE,
        },
      ],

      deniedTopics: [
        bedrock.Topic.custom({
          name: "medical-advice",
          definition: "Providing specific medical diagnoses or treatment plans",
          examples: ["You should take ibuprofen for your headache"],
        }),
        bedrock.Topic.custom({
          name: "financial-investment-advice",
          definition: "Providing specific investment recommendations",
          examples: ["You should invest in stock X"],
        }),
      ],

      piiFilters: [
        { type: bedrock.USASpecificPIIType.US_SOCIAL_SECURITY_NUMBER, action: bedrock.GuardrailAction.BLOCK },
        { type: bedrock.FinancePIIType.CREDIT_DEBIT_CARD_NUMBER, action: bedrock.GuardrailAction.ANONYMIZE },
        { type: bedrock.GeneralPIIType.EMAIL, action: bedrock.GuardrailAction.ANONYMIZE },
        { type: bedrock.GeneralPIIType.PHONE, action: bedrock.GuardrailAction.ANONYMIZE },
        { type: bedrock.GeneralPIIType.NAME, action: bedrock.GuardrailAction.ANONYMIZE },
      ],

      contextualGroundingFilters: [
        { type: bedrock.ContextualGroundingFilterType.GROUNDING, threshold: groundingThreshold },
        { type: bedrock.ContextualGroundingFilterType.RELEVANCE, threshold: relevanceThreshold },
      ],

      managedWordListFilters: [{ type: bedrock.ManagedWordFilterType.PROFANITY }],
    });

    const guardrailVersion = guardrail.createVersion("v1-production");

    // ================================================================
    // LAYER 2: Agent Runtimes — Supervisor + 3 Specialist Sub-Agents
    // Each agent runs in isolated MicroVM with scoped permissions
    // ================================================================

    // --- Supervisor Agent ---
    const supervisorArtifact = agentcore.AgentRuntimeArtifact.fromAsset(
      path.join(__dirname, "..", "..", "agents", "supervisor")
    );
    const supervisorRuntime = new agentcore.Runtime(this, "SupervisorRuntime", {
      runtimeName: `${prefix}_${env}_supervisor`,
      agentRuntimeArtifact: supervisorArtifact,
      description: "Supervisor agent — routes requests, enforces delegation contracts",
      environmentVariables: {
        GUARDRAIL_ID: guardrail.guardrailId,
        GUARDRAIL_VERSION: guardrailVersion,
        AGENT_ROLE: "supervisor",
      },
    });

    // --- Researcher Sub-Agent ---
    const researcherArtifact = agentcore.AgentRuntimeArtifact.fromAsset(
      path.join(__dirname, "..", "..", "agents", "researcher")
    );
    const researcherRuntime = new agentcore.Runtime(this, "ResearcherRuntime", {
      runtimeName: `${prefix}_${env}_researcher`,
      agentRuntimeArtifact: researcherArtifact,
      description: "Researcher sub-agent — retrieves and summarizes customer data",
      environmentVariables: {
        GUARDRAIL_ID: guardrail.guardrailId,
        GUARDRAIL_VERSION: guardrailVersion,
        AGENT_ROLE: "researcher",
      },
    });

    // --- Resolver Sub-Agent ---
    const resolverArtifact = agentcore.AgentRuntimeArtifact.fromAsset(
      path.join(__dirname, "..", "..", "agents", "resolver")
    );
    const resolverRuntime = new agentcore.Runtime(this, "ResolverRuntime", {
      runtimeName: `${prefix}_${env}_resolver`,
      agentRuntimeArtifact: resolverArtifact,
      description: "Resolver sub-agent — executes actions (refunds, tickets)",
      environmentVariables: {
        GUARDRAIL_ID: guardrail.guardrailId,
        GUARDRAIL_VERSION: guardrailVersion,
        AGENT_ROLE: "resolver",
      },
    });

    // --- Reviewer Sub-Agent ---
    const reviewerArtifact = agentcore.AgentRuntimeArtifact.fromAsset(
      path.join(__dirname, "..", "..", "agents", "reviewer")
    );
    const reviewerRuntime = new agentcore.Runtime(this, "ReviewerRuntime", {
      runtimeName: `${prefix}_${env}_reviewer`,
      agentRuntimeArtifact: reviewerArtifact,
      description: "Reviewer sub-agent — validates outputs for accuracy and safety before delivery",
      environmentVariables: {
        GUARDRAIL_ID: guardrail.guardrailId,
        GUARDRAIL_VERSION: guardrailVersion,
        AGENT_ROLE: "reviewer",
      },
    });

    // Grant Bedrock permissions to all runtimes
    for (const rt of [supervisorRuntime, researcherRuntime, resolverRuntime, reviewerRuntime]) {
      rt.addToRolePolicy(
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
          resources: [
            `arn:aws:bedrock:*::foundation-model/*`,
            `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
          ],
        })
      );
      guardrail.grantApply(rt);
    }

    // ================================================================
    // LAYER 3: Delegation Governance — Step Functions + Cedar Policies
    // Trust transitivity: sub-agent perms = own ∩ delegator's perms
    // ================================================================

    // --- Causal Audit Trail Table (also used by Layer 6) ---
    const auditTable = new dynamodb.Table(this, "DelegationAuditTable", {
      tableName: `${p}-audit`,
      partitionKey: { name: "traceId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "hopIndex", type: dynamodb.AttributeType.NUMBER },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true, // Immutable audit — regulatory retention
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      timeToLiveAttribute: "ttl",
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: encryptionKey,
    });
    auditTable.addGlobalSecondaryIndex({
      indexName: "by-session",
      partitionKey: { name: "sessionId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "timestamp", type: dynamodb.AttributeType.NUMBER },
    });
    auditTable.addGlobalSecondaryIndex({
      indexName: "by-agent",
      partitionKey: { name: "agentId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "timestamp", type: dynamodb.AttributeType.NUMBER },
    });

    // --- Cross-Agent Fidelity Check Lambda ---
    const fidelityCheckLambda = new lambda.Function(this, "FidelityCheckLambda", {
      functionName: `${p}-fidelity-check`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "fidelity_check.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "fidelity-check")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      description:
        "Cross-agent contextual grounding — detects cascading hallucinations " +
        "by comparing downstream output against upstream input",
      environment: {
        GUARDRAIL_ID: guardrail.guardrailId,
        GUARDRAIL_VERSION: guardrailVersion,
        AUDIT_TABLE: auditTable.tableName,
      },
    });
    fidelityCheckLambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["bedrock:ApplyGuardrail"],
        resources: [guardrail.guardrailArn],
      })
    );
    fidelityCheckLambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"], // CloudWatch PutMetricData does not support resource-level permissions
      })
    );
    auditTable.grantWriteData(fidelityCheckLambda);

    // --- Delegation Contract Validator Lambda ---
    const delegationValidatorLambda = new lambda.Function(this, "DelegationValidatorLambda", {
      functionName: `${p}-delegation-validator`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "delegation_validator.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "delegation-validator")),
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      description:
        "Validates delegation contracts — enforces trust transitivity " +
        "(effective perms = agent's own ∩ delegator's perms)",
      environment: {
        AUDIT_TABLE: auditTable.tableName,
      },
    });
    auditTable.grantWriteData(delegationValidatorLambda);
    delegationValidatorLambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"], // CloudWatch PutMetricData does not support resource-level permissions
      })
    );
    // cfn-nag suppression: CloudWatch PutMetricData requires * resource
    const delegationValidatorPolicy = delegationValidatorLambda.node.findChild("ServiceRole").node.findChild("DefaultPolicy").node.defaultChild as cdk.CfnResource;
    delegationValidatorPolicy.addMetadata("cfn_nag", { rules_to_suppress: [{ id: "W12", reason: "CloudWatch PutMetricData does not support resource-level permissions" }] });

    // --- Contradiction Detection Lambda ---
    const contradictionDetectorLambda = new lambda.Function(this, "ContradictionDetectorLambda", {
      functionName: `${p}-contradiction-detector`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "contradiction_detector.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "contradiction-detector")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      description:
        "Detects contradictions across multiple agent outputs before final assembly",
      environment: {
        AUDIT_TABLE: auditTable.tableName,
      },
    });
    contradictionDetectorLambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["bedrock:InvokeModel"],
        resources: [
          `arn:aws:bedrock:*::foundation-model/*`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/*`,
        ],
      })
    );
    contradictionDetectorLambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"], // CloudWatch PutMetricData does not support resource-level permissions
      })
    );
    auditTable.grantReadWriteData(contradictionDetectorLambda);

    // --- Step Functions: Multi-Agent Orchestration with Delegation Contracts ---
    const validateDelegation = new tasks.LambdaInvoke(this, "ValidateDelegation", {
      lambdaFunction: delegationValidatorLambda,
      resultPath: "$.delegationResult",
      payloadResponseOnly: true,
    });

    // --- Agent Invoker Lambdas (need DynamoDB + CloudWatch access) ---
    const researcherInvokerLambda = new lambda.Function(this, "ResearcherInvokerLambda", {
      functionName: `${p}-invoke-researcher`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "agent_invoker.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "agent-invoker")),
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      environment: {
        TARGET_AGENT: "researcher",
        AUDIT_TABLE: auditTable.tableName,
      },
    });
    auditTable.grantWriteData(researcherInvokerLambda);
    researcherInvokerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"], // CloudWatch PutMetricData does not support resource-level permissions
      })
    );

    const resolverInvokerLambda = new lambda.Function(this, "ResolverInvokerLambda", {
      functionName: `${p}-invoke-resolver`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "agent_invoker.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "agent-invoker")),
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      environment: {
        TARGET_AGENT: "resolver",
        AUDIT_TABLE: auditTable.tableName,
      },
    });
    auditTable.grantWriteData(resolverInvokerLambda);
    resolverInvokerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"], // CloudWatch PutMetricData does not support resource-level permissions
      })
    );

    const reviewerInvokerLambda = new lambda.Function(this, "ReviewerInvokerLambda", {
      functionName: `${p}-invoke-reviewer`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "agent_invoker.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "agent-invoker")),
      timeout: cdk.Duration.seconds(60),
      memorySize: 512,
      environment: {
        TARGET_AGENT: "reviewer",
        AUDIT_TABLE: auditTable.tableName,
      },
    });
    auditTable.grantWriteData(reviewerInvokerLambda);
    reviewerInvokerLambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"], // CloudWatch PutMetricData does not support resource-level permissions
      })
    );

    const invokeResearcher = new tasks.LambdaInvoke(this, "InvokeResearcher", {
      lambdaFunction: researcherInvokerLambda,
      resultPath: "$.researcherResult",
      payloadResponseOnly: true,
    });

    const fidelityCheckAfterResearch = new tasks.LambdaInvoke(this, "FidelityCheckResearcher", {
      lambdaFunction: fidelityCheckLambda,
      resultPath: "$.fidelityResult",
      payloadResponseOnly: true,
    });

    const invokeResolver = new tasks.LambdaInvoke(this, "InvokeResolver", {
      lambdaFunction: resolverInvokerLambda,
      resultPath: "$.resolverResult",
      payloadResponseOnly: true,
    });

    const fidelityCheckAfterResolver = new tasks.LambdaInvoke(this, "FidelityCheckResolver", {
      lambdaFunction: fidelityCheckLambda,
      resultPath: "$.fidelityResult",
      payloadResponseOnly: true,
    });

    const contradictionCheck = new tasks.LambdaInvoke(this, "ContradictionCheck", {
      lambdaFunction: contradictionDetectorLambda,
      resultPath: "$.contradictionResult",
      payloadResponseOnly: true,
    });

    const invokeReviewer = new tasks.LambdaInvoke(this, "InvokeReviewer", {
      lambdaFunction: reviewerInvokerLambda,
      resultPath: "$.reviewerResult",
      payloadResponseOnly: true,
    });

    // Fidelity failure path — halt and alert
    const fidelityFailed = new sfn.Fail(this, "FidelityFailed", {
      cause: "Cross-agent fidelity check failed — cascading hallucination detected",
      error: "FIDELITY_FAILURE",
    });

    const contradictionFailed = new sfn.Fail(this, "ContradictionFailed", {
      cause: "Contradiction detected across agent outputs",
      error: "CONTRADICTION_DETECTED",
    });

    const delegationDenied = new sfn.Fail(this, "DelegationDenied", {
      cause: "Delegation contract validation failed — trust transitivity violation",
      error: "DELEGATION_DENIED",
    });

    // Build the state machine
    const definition = validateDelegation
      .next(
        new sfn.Choice(this, "DelegationValid?")
          .when(sfn.Condition.stringEquals("$.delegationResult.status", "DENIED"), delegationDenied)
          .otherwise(
            invokeResearcher.next(
              fidelityCheckAfterResearch.next(
                new sfn.Choice(this, "ResearcherFidelityOk?")
                  .when(sfn.Condition.stringEquals("$.fidelityResult.status", "FAILED"), fidelityFailed)
                  .otherwise(
                    invokeResolver.next(
                      fidelityCheckAfterResolver.next(
                        new sfn.Choice(this, "ResolverFidelityOk?")
                          .when(sfn.Condition.stringEquals("$.fidelityResult.status", "FAILED"), fidelityFailed)
                          .otherwise(
                            contradictionCheck.next(
                              new sfn.Choice(this, "NoContradictions?")
                                .when(sfn.Condition.stringEquals("$.contradictionResult.status", "CONTRADICTION_FOUND"), contradictionFailed)
                                .otherwise(invokeReviewer)
                            )
                          )
                      )
                    )
                  )
              )
            )
          )
      );

    const orchestrator = new sfn.StateMachine(this, "MultiAgentOrchestrator", {
      stateMachineName: `${p}-orchestrator`,
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      timeout: cdk.Duration.minutes(5),
      tracingEnabled: true, // X-Ray distributed tracing
      logs: {
        destination: new logs.LogGroup(this, "OrchestratorLogs", {
          logGroupName: `/${p}/orchestrator`,
          retention: logs.RetentionDays.ONE_YEAR,
          removalPolicy: cdk.RemovalPolicy.DESTROY,
          encryptionKey: encryptionKey,
        }),
        level: sfn.LogLevel.ALL,
        includeExecutionData: true,
      },
    });

    // ================================================================
    // LAYER 4: Identity & Trust — Cognito + Workload Identity
    // ================================================================

    const userPool = new cognito.UserPool(this, "MultiAgentUserPool", {
      userPoolName: `${p}-user-pool`,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 12,
        requireUppercase: true,
        requireLowercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const resourceServer = userPool.addResourceServer("AgentResourceServer", {
      identifier: "agent",
      userPoolResourceServerName: `${p}-scopes`,
      scopes: [
        new cognito.ResourceServerScope({ scopeName: "read", scopeDescription: "Read customer data" }),
        new cognito.ResourceServerScope({ scopeName: "write", scopeDescription: "Write operations" }),
        new cognito.ResourceServerScope({ scopeName: "escalate", scopeDescription: "Escalation operations" }),
      ],
    });

    const userPoolClient = userPool.addClient("AgentAppClient", {
      userPoolClientName: `${p}-client`,
      generateSecret: true,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.PROFILE,
          cognito.OAuthScope.custom("agent/read"),
          cognito.OAuthScope.custom("agent/write"),
          cognito.OAuthScope.custom("agent/escalate"),
        ],
        callbackUrls: ["https://localhost:3000/callback"],
      },
      authFlows: { userSrp: true },
    });
    userPoolClient.node.addDependency(resourceServer);

    userPool.addDomain("AgentAuthDomain", {
      cognitoDomain: { domainPrefix: `${p}-${cdk.Aws.ACCOUNT_ID}` },
    });

    // ================================================================
    // LAYER 5: Action Governance — Gateway + Cedar Policy + Interceptors
    // Cedar enforces trust transitivity at the gateway boundary
    // ================================================================

    const gatewayRole = new iam.Role(this, "GatewayRole", {
      roleName: `${p}-gateway-role`,
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    });

    const requestInterceptor = new lambda.Function(this, "RequestInterceptor", {
      functionName: `${p}-request-interceptor`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "request_interceptor.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "gateway-interceptors")),
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      description: "JWT validation, scope-based access control, delegation scope enforcement",
      environment: { LOG_LEVEL: "INFO" },
    });

    const responseInterceptor = new lambda.Function(this, "ResponseInterceptor", {
      functionName: `${p}-response-interceptor`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "response_interceptor.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "gateway-interceptors")),
      timeout: cdk.Duration.seconds(10),
      memorySize: 256,
      description: "Dynamic tool filtering, PII redaction, audit metadata injection",
      environment: { LOG_LEVEL: "INFO" },
    });

    requestInterceptor.grantInvoke(gatewayRole);
    responseInterceptor.grantInvoke(gatewayRole);

    const gateway = new bedrockagentcore.CfnGateway(this, "MultiAgentGateway", {
      name: `${p}-gateway`,
      protocolType: "MCP",
      authorizerType: "CUSTOM_JWT",
      roleArn: gatewayRole.roleArn,
      description: "Multi-agent gateway with Cedar trust transitivity enforcement",
      authorizerConfiguration: {
        customJwtAuthorizer: {
          discoveryUrl: `https://cognito-idp.${this.region}.amazonaws.com/${cdk.Lazy.string({ produce: () => userPool.userPoolId })}/.well-known/openid-configuration`,
          allowedScopes: ["agent/read", "agent/write", "agent/escalate"],
        },
      },
      interceptorConfigurations: [
        {
          interceptionPoints: ["REQUEST"],
          interceptor: { lambda: { arn: requestInterceptor.functionArn } },
        },
        {
          interceptionPoints: ["RESPONSE"],
          interceptor: { lambda: { arn: responseInterceptor.functionArn } },
        },
      ],
    });

    // --- Cedar Policy Engine with Trust Transitivity ---
    const policyEngine = new bedrockagentcore.CfnPolicyEngine(this, "PolicyEngine", {
      name: `${prefix}_${env}_policy_engine`,
    });

    const policyFiles = [
      "trust-transitivity.cedar",
      "delegation-scope.cedar",
      "refund-limit.cedar",
      "read-write-separation.cedar",
    ];

    let policyIndex = 0;
    for (const file of policyFiles) {
      const cedarText = fs.readFileSync(
        path.join(__dirname, "..", "policies", file), "utf-8"
      );
      const statements = cedarText
        .split(/(?=^(?:permit|forbid)\s*\()/m)
        .map((s) => s.trim())
        .filter((s) => s.startsWith("permit") || s.startsWith("forbid"));

      for (const stmt of statements) {
        policyIndex++;
        const baseName = file.replace(".cedar", "").replace(/-/g, "_");
        // Bind generic "resource is AgentCore::Gateway" to the specific gateway ARN
        const boundStmt = stmt.replace(
          /resource is AgentCore::Gateway/g,
          `resource == AgentCore::Gateway::"${gateway.attrGatewayArn}"`
        );
        new bedrockagentcore.CfnPolicy(this, `Policy_${baseName}_${policyIndex}`, {
          name: `${baseName}_${policyIndex}`,
          policyEngineId: policyEngine.attrPolicyEngineId,
          definition: { cedar: { statement: boundStmt } },
        });
      }
    }

    // ================================================================
    // LAYER 6: Fairness Parity Monitoring
    // Tracks response quality across parallel agent paths
    // ================================================================

    const fairnessMetricsLambda = new lambda.Function(this, "FairnessMetricsLambda", {
      functionName: `${p}-fairness-monitor`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "fairness_monitor.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "fairness-monitor")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      description:
        "Publishes fairness parity metrics per agent path/user segment to CloudWatch",
      environment: {
        AUDIT_TABLE: auditTable.tableName,
      },
    });
    auditTable.grantReadData(fairnessMetricsLambda);
    fairnessMetricsLambda.addToRolePolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"], // CloudWatch PutMetricData does not support resource-level permissions
      })
    );

    // ================================================================
    // LAYER 7: Observability & Audit — CloudWatch + SNS + CloudTrail
    // Full causal audit trail across the delegation chain
    // ================================================================

    const raiAlertsTopic = new sns.Topic(this, "RaiAlertsTopic", {
      topicName: `${p}-alerts`,
      displayName: "Responsible AI Multi-Agent Alerts",
      masterKey: encryptionKey,
    });

    // --- Audit Query API Lambda ---
    const auditQueryLambda = new lambda.Function(this, "AuditQueryLambda", {
      functionName: `${p}-audit-query-api`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "audit_query.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambdas", "audit-query")),
      timeout: cdk.Duration.seconds(15),
      memorySize: 256,
      description:
        "Queryable audit API — trace any output back to its full delegation chain",
      environment: {
        AUDIT_TABLE: auditTable.tableName,
      },
    });
    auditTable.grantReadData(auditQueryLambda);

    // --- CloudWatch Dashboard ---
    const dashboard = new cloudwatch.Dashboard(this, "MultiAgentDashboard", {
      dashboardName: `${p}-dashboard`,
    });

    const guardrailInvocations = guardrail.metricInvocations({ period: cdk.Duration.minutes(5) });
    const guardrailIntervened = guardrail.metricInvocationsIntervened({ period: cdk.Duration.minutes(5) });

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Guardrail Activity (All Agents)",
        left: [guardrailInvocations, guardrailIntervened],
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        title: "Guardrail Latency",
        left: [guardrail.metricInvocationLatency({ period: cdk.Duration.minutes(5) })],
        width: 12,
      })
    );

    // Fairness parity metrics
    const fairnessMetricResearcher = new cloudwatch.Metric({
      namespace: "RAI/FairnessParity",
      metricName: "ResponseQuality",
      dimensionsMap: { AgentPath: "researcher" },
      period: cdk.Duration.minutes(5),
    });
    const fairnessMetricResolver = new cloudwatch.Metric({
      namespace: "RAI/FairnessParity",
      metricName: "ResponseQuality",
      dimensionsMap: { AgentPath: "resolver" },
      period: cdk.Duration.minutes(5),
    });

    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: "Fairness Parity — Response Quality by Agent Path",
        left: [fairnessMetricResearcher, fairnessMetricResolver],
        width: 12,
      }),
      new cloudwatch.GraphWidget({
        title: "Delegation Chain Health",
        left: [
          new cloudwatch.Metric({
            namespace: "RAI/Delegation",
            metricName: "FidelityScore",
            period: cdk.Duration.minutes(5),
          }),
          new cloudwatch.Metric({
            namespace: "RAI/Delegation",
            metricName: "ContradictionsDetected",
            period: cdk.Duration.minutes(5),
          }),
        ],
        width: 12,
      })
    );

    dashboard.addWidgets(
      new cloudwatch.SingleValueWidget({
        title: "Trust Transitivity Violations (Last Hour)",
        metrics: [
          new cloudwatch.Metric({
            namespace: "RAI/Delegation",
            metricName: "TrustViolations",
            period: cdk.Duration.hours(1),
            statistic: "Sum",
          }),
        ],
        width: 8,
      }),
      new cloudwatch.SingleValueWidget({
        title: "Cascading Hallucinations Caught",
        metrics: [
          new cloudwatch.Metric({
            namespace: "RAI/Delegation",
            metricName: "FidelityFailures",
            period: cdk.Duration.hours(1),
            statistic: "Sum",
          }),
        ],
        width: 8,
      }),
      new cloudwatch.SingleValueWidget({
        title: "Delegation Denials",
        metrics: [
          new cloudwatch.Metric({
            namespace: "RAI/Delegation",
            metricName: "DelegationDenials",
            period: cdk.Duration.hours(1),
            statistic: "Sum",
          }),
        ],
        width: 8,
      })
    );

    // --- Alarms ---
    const fidelityAlarm = new cloudwatch.Alarm(this, "FidelityFailureAlarm", {
      alarmName: `${p}-fidelity-failure`,
      alarmDescription: "Cascading hallucination detected — cross-agent fidelity check failed",
      metric: new cloudwatch.Metric({
        namespace: "RAI/Delegation",
        metricName: "FidelityFailures",
        period: cdk.Duration.minutes(5),
        statistic: "Sum",
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    });
    fidelityAlarm.addAlarmAction(new cloudwatch_actions.SnsAction(raiAlertsTopic));

    const trustViolationAlarm = new cloudwatch.Alarm(this, "TrustViolationAlarm", {
      alarmName: `${p}-trust-violation`,
      alarmDescription: "Trust transitivity violation — sub-agent attempted to exceed delegator scope",
      metric: new cloudwatch.Metric({
        namespace: "RAI/Delegation",
        metricName: "TrustViolations",
        period: cdk.Duration.minutes(5),
        statistic: "Sum",
      }),
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    });
    trustViolationAlarm.addAlarmAction(new cloudwatch_actions.SnsAction(raiAlertsTopic));

    const fairnessDriftAlarm = new cloudwatch.Alarm(this, "FairnessDriftAlarm", {
      alarmName: `${p}-fairness-drift`,
      alarmDescription: "Fairness parity drift — response quality diverging across agent paths",
      metric: new cloudwatch.Metric({
        namespace: "RAI/FairnessParity",
        metricName: "ParityDivergence",
        period: cdk.Duration.minutes(15),
        statistic: "Maximum",
      }),
      threshold: parityDivergenceThreshold,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
    });
    fairnessDriftAlarm.addAlarmAction(new cloudwatch_actions.SnsAction(raiAlertsTopic));

    const guardrailInterventionAlarm = new cloudwatch.Alarm(this, "GuardrailInterventionAlarm", {
      alarmName: `${p}-guardrail-spike`,
      alarmDescription: "Guardrail interventions spiking across agent chain",
      metric: guardrailIntervened,
      threshold: 50,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    guardrailInterventionAlarm.addAlarmAction(new cloudwatch_actions.SnsAction(raiAlertsTopic));

    // --- Log Groups ---
    new logs.LogGroup(this, "AuditLogGroup", {
      logGroupName: `/${p}/audit`,
      retention: logs.RetentionDays.ONE_YEAR,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      encryptionKey: encryptionKey,
    });

    new logs.LogGroup(this, "DelegationLogGroup", {
      logGroupName: `/${p}/delegation-decisions`,
      retention: logs.RetentionDays.ONE_YEAR,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      encryptionKey: encryptionKey,
    });

    // ================================================================
    // cfn-nag suppressions for acknowledged warnings
    // ================================================================

    // W89: Lambda not in VPC — acknowledged limitation for reference implementation
    // W92: Lambda missing ReservedConcurrentExecutions — not needed for reference impl
    // W58: Lambda CloudWatch Logs — handled by AWSLambdaBasicExecutionRole
    const allLambdas = [
      fidelityCheckLambda, delegationValidatorLambda, contradictionDetectorLambda,
      researcherInvokerLambda, resolverInvokerLambda, reviewerInvokerLambda,
      requestInterceptor, responseInterceptor, fairnessMetricsLambda, auditQueryLambda,
    ];
    for (const fn of allLambdas) {
      const cfnFn = fn.node.defaultChild as cdk.CfnResource;
      cfnFn.addMetadata("cfn_nag", {
        rules_to_suppress: [
          { id: "W89", reason: "VPC deployment is not required for this reference implementation. Add VPC for production hardening." },
          { id: "W92", reason: "ReservedConcurrentExecutions not needed for reference implementation with low traffic." },
          { id: "W58", reason: "Lambda has AWSLambdaBasicExecutionRole which grants CloudWatch Logs permissions." },
        ],
      });
    }

    // W28: Explicit names — intentional for user-friendly resource identification
    const namedResources = [
      fidelityAlarm, trustViolationAlarm, fairnessDriftAlarm, guardrailInterventionAlarm,
    ];
    for (const res of namedResources) {
      const cfnRes = res.node.defaultChild as cdk.CfnResource;
      cfnRes.addMetadata("cfn_nag", {
        rules_to_suppress: [
          { id: "W28", reason: "Explicit alarm names are intentional for CloudWatch dashboard references and operational runbooks." },
        ],
      });
    }

    // W28 on DynamoDB table and Gateway role
    (auditTable.node.defaultChild as cdk.CfnResource).addMetadata("cfn_nag", {
      rules_to_suppress: [
        { id: "W28", reason: "Explicit table name required for cross-stack references and audit query API." },
      ],
    });
    (gatewayRole.node.defaultChild as cdk.CfnResource).addMetadata("cfn_nag", {
      rules_to_suppress: [
        { id: "W28", reason: "Explicit role name required for AgentCore Gateway trust policy." },
      ],
    });

    // W12 on policies that use * for CloudWatch PutMetricData (no resource-level support)
    const policiesWithWildcard = [
      fidelityCheckLambda, contradictionDetectorLambda,
      researcherInvokerLambda, resolverInvokerLambda, reviewerInvokerLambda,
      fairnessMetricsLambda,
    ];
    for (const fn of policiesWithWildcard) {
      try {
        const policy = fn.node.findChild("ServiceRole").node.findChild("DefaultPolicy").node.defaultChild as cdk.CfnResource;
        policy.addMetadata("cfn_nag", {
          rules_to_suppress: [
            { id: "W12", reason: "CloudWatch PutMetricData does not support resource-level permissions — * is required." },
          ],
        });
      } catch (e) { /* policy may not exist */ }
    }

    // W76 on runtime execution roles — agent runtimes need broad Bedrock permissions
    for (const rt of [supervisorRuntime, researcherRuntime, resolverRuntime, reviewerRuntime]) {
      try {
        const policy = rt.node.findChild("ExecutionRole").node.findChild("DefaultPolicy").node.defaultChild as cdk.CfnResource;
        policy.addMetadata("cfn_nag", {
          rules_to_suppress: [
            { id: "W12", reason: "Bedrock InvokeModel requires foundation-model/* ARN pattern." },
            { id: "W76", reason: "Agent runtime execution roles require multiple Bedrock permissions for model invocation and guardrail application." },
          ],
        });
      } catch (e) { /* structure may differ */ }
    }

    // W76/W12 on orchestrator role
    try {
      const orchPolicy = orchestrator.node.findChild("Role").node.findChild("DefaultPolicy").node.defaultChild as cdk.CfnResource;
      orchPolicy.addMetadata("cfn_nag", {
        rules_to_suppress: [
          { id: "W12", reason: "Step Functions orchestrator invokes multiple Lambda functions requiring broad resource access." },
          { id: "W76", reason: "Orchestrator role requires permissions to invoke all governance Lambda functions in the chain." },
        ],
      });
    } catch (e) { /* structure may differ */ }

    // ================================================================
    // Outputs
    // ================================================================

    new cdk.CfnOutput(this, "GuardrailId", { value: guardrail.guardrailId });
    new cdk.CfnOutput(this, "SupervisorRuntimeArn", { value: supervisorRuntime.agentRuntimeArn });
    new cdk.CfnOutput(this, "ResearcherRuntimeArn", { value: researcherRuntime.agentRuntimeArn });
    new cdk.CfnOutput(this, "ResolverRuntimeArn", { value: resolverRuntime.agentRuntimeArn });
    new cdk.CfnOutput(this, "ReviewerRuntimeArn", { value: reviewerRuntime.agentRuntimeArn });
    new cdk.CfnOutput(this, "OrchestratorArn", { value: orchestrator.stateMachineArn });
    new cdk.CfnOutput(this, "GatewayId", { value: gateway.attrGatewayIdentifier });
    new cdk.CfnOutput(this, "AuditTableName", { value: auditTable.tableName });
    new cdk.CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
    new cdk.CfnOutput(this, "AlertsTopicArn", { value: raiAlertsTopic.topicArn });
    new cdk.CfnOutput(this, "PolicyEngineId", { value: policyEngine.attrPolicyEngineId });
    new cdk.CfnOutput(this, "DashboardUrl", {
      value: `https://${this.region}.console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards:name=${p}-dashboard`,
    });
  }
}
