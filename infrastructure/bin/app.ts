#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { ResponsibleAiMultiAgentStack } from "../lib/responsible-ai-multi-agent-stack";

const app = new cdk.App();

// Stack name is derived from config prefix + environment
const config = app.node.tryGetContext("config") || {};
const prefix = config.prefix || "rai";
const env = config.environment || "dev";
const stackName = `${prefix}-${env}-MultiAgentStack`;

new ResponsibleAiMultiAgentStack(app, stackName, {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: config.region || process.env.CDK_DEFAULT_REGION || "us-east-1",
  },
  description:
    "Unified Defense-in-Depth Responsible AI for Multi-Agent Collaboration — " +
    "7 governance layers covering single-agent safety + inter-agent delegation governance",
});
