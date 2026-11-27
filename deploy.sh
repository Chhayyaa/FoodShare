#!/usr/bin/env bash
# Deploy FoodShare to AWS: infrastructure + API + frontend.
# Requirements: AWS CLI (configured), AWS SAM CLI, Python 3.12.
#   DB_PASSWORD='Your-Strong-Pass-123' ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

STACK="${STACK_NAME:-food-waste-system}"
REGION="${AWS_REGION:-ap-south-1}"
: "${DB_PASSWORD:?Set DB_PASSWORD (12+ chars; no / @ \" or spaces)}"

sam build
sam deploy \
  --stack-name "$STACK" --region "$REGION" \
  --resolve-s3 --capabilities CAPABILITY_IAM \
  --no-confirm-changeset --no-fail-on-empty-changeset \
  --parameter-overrides "DBPassword=$DB_PASSWORD" "AlertEmail=${ALERT_EMAIL:-}"

out() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
API_URL=$(out ApiUrl); CLIENT_ID=$(out UserPoolClientId); BUCKET=$(out FrontendBucketName)
DIST_ID=$(out DistributionId); SITE=$(out WebsiteUrl)

mkdir -p build
cat > build/config.js <<EOF
window.APP_CONFIG = { mode: "cloud", apiUrl: "$API_URL", region: "$REGION", clientId: "$CLIENT_ID" };
EOF

aws s3 sync frontend "s3://$BUCKET" --delete --exclude config.js --region "$REGION"
aws s3 cp build/config.js "s3://$BUCKET/config.js" --region "$REGION"
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" >/dev/null

echo
echo "Deployed. Open: $SITE"
echo "To create an admin: register in the app, then run:"
echo "  aws cognito-idp admin-add-user-to-group --user-pool-id $(out UserPoolId) --username <email> --group-name admin --region $REGION"
