#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Setup Foundation Script
# Bootstraps and deploys the Responsible AI Multi-Agent solution
# =============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=============================================="
echo " Responsible AI Multi-Agent — Setup Foundation"
echo "=============================================="
echo ""

# --- Prerequisites Check ---
echo "Checking prerequisites..."

check_command() {
    if ! command -v "$1" &> /dev/null; then
        echo -e "${RED}✗ $1 is not installed${NC}"
        echo "  Install: $2"
        exit 1
    else
        echo -e "${GREEN}✓ $1 found${NC}"
    fi
}

check_command "aws" "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
check_command "cdk" "npm install -g aws-cdk"
check_command "docker" "https://docs.docker.com/get-docker/"
check_command "node" "https://nodejs.org/"

# Check Node.js version (need 18+)
NODE_VERSION=$(node -v | sed 's/v//' | cut -d. -f1)
if [ "$NODE_VERSION" -lt 18 ]; then
    echo -e "${RED}✗ Node.js 18+ required (found v${NODE_VERSION})${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Node.js v${NODE_VERSION} (18+ required)${NC}"

# Check Docker is running
if ! docker info &> /dev/null 2>&1; then
    echo -e "${RED}✗ Docker is not running${NC}"
    echo "  Please start Docker Desktop or the Docker daemon"
    exit 1
fi
echo -e "${GREEN}✓ Docker is running${NC}"

# Check AWS credentials
if ! aws sts get-caller-identity &> /dev/null 2>&1; then
    echo -e "${RED}✗ AWS credentials not configured${NC}"
    echo "  Run: aws configure"
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=${AWS_DEFAULT_REGION:-${CDK_DEFAULT_REGION:-us-east-1}}
echo -e "${GREEN}✓ AWS credentials configured (Account: ${ACCOUNT_ID}, Region: ${REGION})${NC}"

echo ""
echo "All prerequisites met!"
echo ""

# --- CDK Bootstrap ---
echo -e "${YELLOW}Bootstrapping CDK in ${ACCOUNT_ID}/${REGION}...${NC}"
cd "$(dirname "$0")/../infrastructure"

cdk bootstrap "aws://${ACCOUNT_ID}/${REGION}"
echo -e "${GREEN}✓ CDK bootstrapped${NC}"
echo ""

# --- Install Dependencies ---
echo -e "${YELLOW}Installing dependencies...${NC}"
npm install
echo -e "${GREEN}✓ Dependencies installed${NC}"
echo ""

# --- Deploy ---
echo -e "${YELLOW}Deploying stack...${NC}"
npx cdk deploy --require-approval never --outputs-file ../cdk-outputs.json
echo -e "${GREEN}✓ Stack deployed${NC}"
echo ""

# --- Print Outputs ---
echo "=============================================="
echo " Deployed Resource ARNs"
echo "=============================================="

if [ -f ../cdk-outputs.json ]; then
    cat ../cdk-outputs.json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for stack, outputs in data.items():
    for key, value in outputs.items():
        print(f'  {key}: {value}')
"
else
    echo "  (outputs file not found — check AWS Console for resource ARNs)"
fi

echo ""
echo -e "${GREEN}Setup complete!${NC}"
echo ""
echo "Next steps:"
echo "  1. Subscribe to SNS alerts: aws sns subscribe --topic-arn <AlertsTopicArn> --protocol email --notification-endpoint your@email.com"
echo "  2. Test the delegation validator: aws lambda invoke --function-name rai-delegation-validator --payload '{...}' /tmp/out.json"
echo "  3. View the dashboard: open the DashboardUrl from outputs above"
