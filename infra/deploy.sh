#!/usr/bin/env bash
# Run only after the owner has supplied AWS credentials and approved AWS charges.
set -euo pipefail
: "${AWS_PROFILE:?Set AWS_PROFILE to your configured, owner-approved AWS CLI profile}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
# Lambda accepts a single-architecture image, without a Buildx attestation index.
export BUILDX_NO_DEFAULT_ATTESTATIONS=1
stack_name="${STACK_NAME:-shorts-qc}"
if [[ ! "$stack_name" =~ ^[a-z][a-z0-9-]{0,19}$ ]]; then
  echo 'STACK_NAME must be 1-20 lowercase letters/digits/hyphens, starting with a letter.' >&2
  exit 1
fi
for tool in aws sam docker; do command -v "$tool" >/dev/null; done
cd "$(dirname "$0")/.."
aws sts get-caller-identity --query Account --output text
account_concurrency="$(aws lambda get-account-settings --query AccountLimit.ConcurrentExecutions --output text)"
if (( account_concurrency < 102 )); then
  echo 'Lambda needs an account concurrency quota of at least 102 to reserve 2 workers (AWS keeps 100 unreserved).' >&2
  echo 'Request a free Lambda concurrency quota increase before deployment; also verify 3072 MiB function memory is allowed.' >&2
  exit 1
fi
sam validate --template-file infra/template.yaml --lint
sam build --template-file infra/template.yaml
sam deploy --stack-name "$stack_name" --template-file .aws-sam/build/template.yaml \
  --capabilities CAPABILITY_IAM --resolve-image-repos --resolve-s3 \
  --no-confirm-changeset --no-fail-on-empty-changeset
output() {
  aws cloudformation describe-stacks --stack-name "$stack_name" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}
report_bucket="$(output ReportBucket)"
aws s3 sync shorts_qc/web/ "s3://$report_bucket/viewer/" --cache-control no-store
echo "Viewer: $(output ViewerURL)"
echo "Upload: aws s3 cp clip.mp4 s3://$(output UploadBucket)/incoming/clip.mp4"
echo "Jobs table: $(output JobsTable)"
echo 'CloudFront reports are shareable to anyone with the URL; upload only your own demo clips.'
