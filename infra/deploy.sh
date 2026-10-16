#!/usr/bin/env bash
# Run on the Mac with the owner's existing AWS CLI login. Heavy work stays on pilot.
set -euo pipefail
export AWS_REGION="${AWS_REGION:-ap-southeast-2}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PAGER=''
stack_name="${STACK_NAME:-shorts-qc}"
pilot_host="${BUILD_HOST:-pilot}"
if [[ ! "$stack_name" =~ ^[a-z][a-z0-9-]{0,19}$ ]]; then
  echo 'STACK_NAME must be 1-20 lowercase letters/digits/hyphens, starting with a letter.' >&2
  exit 1
fi
for tool in aws ssh rsync python3; do command -v "$tool" >/dev/null; done
cd "$(dirname "$0")/.."
account="$(aws sts get-caller-identity --query Account --output text)"
aws freetier get-account-plan-state --region us-east-1 --output json | python3 -c '
import json,sys
p=json.load(sys.stdin)
assert p["accountPlanType"] == "FREE", "This deployment is approved only for the AWS Free plan"
assert float(p["accountPlanRemainingCredits"]["amount"]) > 10, "Keep at least $10 credit headroom"
print("Free plan confirmed; credits remaining:", p["accountPlanRemainingCredits"]["amount"])
'
account_concurrency="$(aws lambda get-account-settings --query AccountLimit.ConcurrentExecutions --output text)"
if (( account_concurrency < 105 )); then
  echo 'API (4) plus worker (1) need at least 105 account concurrency units.' >&2
  exit 1
fi
artifact_bucket="$stack_name-$account-$AWS_REGION-artifacts"
if ! aws s3api head-bucket --bucket "$artifact_bucket" 2>/dev/null; then
  aws s3api create-bucket --bucket "$artifact_bucket" --create-bucket-configuration "LocationConstraint=$AWS_REGION" >/dev/null
fi
aws s3api put-public-access-block --bucket "$artifact_bucket" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "$artifact_bucket" --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
if ! aws ecr describe-repositories --repository-names "$stack_name" >/dev/null 2>&1; then
  aws ecr create-repository --repository-name "$stack_name" --image-tag-mutability IMMUTABLE >/dev/null
fi
aws ecr put-lifecycle-policy --repository-name "$stack_name" --lifecycle-policy-text '{"rules":[{"rulePriority":1,"description":"Keep last two deploy images","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":2},"action":{"type":"expire"}}]}' >/dev/null
registry="$account.dkr.ecr.$AWS_REGION.amazonaws.com"
build_tag="aws-$(date -u +%Y%m%d%H%M%S)"
remote_dir='workspace/hackathons/opencv'
rsync -az --delete --exclude node_modules --exclude .git --exclude .venv --exclude models --exclude work ./ "$pilot_host:$remote_dir/"
ssh "$pilot_host" "cd $remote_dir && SHORTS_QC_TEXT_MODEL=models/text.onnx .venv/bin/python -m pytest -q && ${CFN_LINT:-cfn-lint} infra/template.yaml"
image_uri="${IMAGE_URI:-}"
if [[ -z "$image_uri" ]]; then
  ssh "$pilot_host" "cd $remote_dir && docker build --provenance=false --platform linux/amd64 -f infra/Dockerfile -t $stack_name:$build_tag ."
  # Only a short-lived registry token crosses stdin. Docker's auth config stays in RAM.
  # Neither Mac session credentials nor its AWS login cache are copied to pilot.
  aws ecr get-login-password | ssh "$pilot_host" "set -eu
export DOCKER_CONFIG=\$(mktemp -d /dev/shm/shorts-qc-docker.XXXXXX)
trap 'rm -rf \"\$DOCKER_CONFIG\"' EXIT
docker login --username AWS --password-stdin $registry >/dev/null
docker tag $stack_name:$build_tag $registry/$stack_name:$build_tag
docker push $registry/$stack_name:$build_tag"
  image_uri="$registry/$stack_name:$build_tag"
fi
mkdir -p .aws-sam
python3 - <<'PY'
from pathlib import Path
import zipfile
with zipfile.ZipFile('.aws-sam/api.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    z.write('infra/api.py', 'api.py')
    for p in Path('shorts_qc/web').iterdir():
        if p.is_file(): z.write(p, 'web/' + p.name)
PY
zip_hash="$(shasum -a 256 .aws-sam/api.zip | cut -d ' ' -f 1)"
zip_key="api/$zip_hash.zip"
aws s3 cp .aws-sam/api.zip "s3://$artifact_bucket/$zip_key" --only-show-errors
aws cloudformation deploy --stack-name "$stack_name" --template-file infra/template.yaml \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides "ImageUri=$image_uri" "ArtifactBucket=$artifact_bucket" "ApiZipKey=$zip_key"
output() {
  aws cloudformation describe-stacks --stack-name "$stack_name" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}
aws s3 cp media/demo.mp4 "s3://$(output UploadBucket)/demo/demo.mp4" --content-type video/mp4 --only-show-errors
# The default report is populated by scripts/aws-demo.py only after an actual AWS run.
echo "Viewer: $(output ViewerURL)"
echo "Upload API: $(output UploadEndpoint)"
echo "Jobs table: $(output JobsTable)"
echo 'Monthly console budget: $5 (gross cost; no email subscriber). Reports are shareable.'
