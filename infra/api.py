"""Public, credit-bounded demo API and dependency-free HTTPS report viewer."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

MAX_BYTES = 25 * 1024 * 1024
DEMO_END = 1794355200  # 2026-11-11 00:00 UTC; no new demo uploads after judging.


def _clients():
    import boto3
    from botocore.config import Config
    region = os.environ.get("AWS_REGION", "ap-southeast-2")
    return (boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com",
                config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"})),
            boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"]))


def response(status, body, mime="application/json", binary=False):
    return {"statusCode": status, "headers": {"content-type": mime,
        "cache-control": "no-store", "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer"}, "isBase64Encoded": binary,
        "body": base64.b64encode(body).decode() if binary else
            (json.dumps(body) if mime == "application/json" else body)}


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    try:
        if method == "GET" and path in ("/", "/index.html", "/app.js", "/style.css"):
            name = "index.html" if path == "/" else path[1:]
            mime = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript", "style.css": "text/css"}[name]
            return response(200, (Path(__file__).parent / "web" / name).read_text(), mime)
        s3, table = _clients()
        if method == "GET" and path == "/api/health":
            return response(200, {"mode": "aws-live", "max_upload_bytes": MAX_BYTES,
                "upload_limit": 200, "uploads_close_at": DEMO_END})
        if method == "POST" and path == "/api/uploads":
            if time.time() >= DEMO_END:
                return response(403, {"error": "Judging upload window has ended. Existing reports remain available."})
            raw = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                raw = base64.b64decode(raw).decode()
            data = json.loads(raw)
            name, size, checksum = data.get("filename", ""), data.get("size"), data.get("sha256", "")
            suffix = Path(name).suffix.lower()
            if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"} or type(size) is not int or not 0 < size <= MAX_BYTES:
                return response(400, {"error": "Use a video file up to 25 MiB, 120 seconds, 60 fps and 4.2 megapixels."})
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                return response(400, {"error": "Provide the file SHA-256 as 64 lowercase hex characters."})
            token = uuid.uuid4().hex
            key = f"incoming/{token}{suffix}"
            # A lifetime cap, intentionally without TTL: deleting job TTLs must not reset it.
            try:
                table.update_item(Key={"job_id": "UPLOAD_BUDGET"},
                    UpdateExpression="SET used = if_not_exists(used, :zero) + :one",
                    ConditionExpression="attribute_not_exists(used) OR used < :maximum",
                    ExpressionAttributeValues={":zero": 0, ":one": 1, ":maximum": 200})
            except Exception as exc:
                if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    return response(429, {"error": "This public demo has reached its 200-upload credit limit."})
                raise
            checksum64 = base64.b64encode(bytes.fromhex(checksum)).decode()
            url = s3.generate_presigned_url("put_object", Params={"Bucket": os.environ["UPLOAD_BUCKET"],
                "Key": key, "ContentLength": size, "ContentType": "application/octet-stream", "IfNoneMatch": "*",
                "ChecksumSHA256": checksum64}, ExpiresIn=180)
            table.put_item(Item={"job_id": f"upload:{token}", "key": key, "expires_at": int(time.time()) + 30 * 86400})
            return response(201, {"upload_id": token, "upload_url": url,
                "headers": {"content-type": "application/octet-stream", "x-amz-checksum-sha256": checksum64, "if-none-match": "*"},
                "status_url": f"/api/jobs/{token}", "expires_in": 180})
        if method == "GET" and re.fullmatch(r"/api/jobs/[a-f0-9]{32}", path):
            token = path.rsplit("/", 1)[1]
            upload = table.get_item(Key={"job_id": f"upload:{token}"}, ConsistentRead=True).get("Item")
            if not upload:
                return response(404, {"error": "Unknown upload"})
            job = upload.get("analysis_job")
            if not job:
                try:
                    head = s3.head_object(Bucket=os.environ["UPLOAD_BUCKET"], Key=upload["key"])
                except Exception as exc:
                    if getattr(exc, "response", {}).get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
                        return response(200, {"status": "AWAITING_UPLOAD"})
                    raise
                etag = head["ETag"].strip('"')
                job = hashlib.sha256(f'{os.environ["UPLOAD_BUCKET"]}\n{upload["key"]}\n{etag}'.encode()).hexdigest()
                table.update_item(Key={"job_id": f"upload:{token}"}, UpdateExpression="SET analysis_job = :job",
                    ExpressionAttributeValues={":job": job})
            row = table.get_item(Key={"job_id": job}, ConsistentRead=True).get("Item", {})
            status = row.get("status", "QUEUED")
            if status == "PROCESSING" and row.get("lease_until", time.time()) < time.time():
                # A hard worker timeout cannot enter its exception handler. Reflect the
                # bounded asynchronous retry window without falsely claiming completion.
                status = "TIMED_OUT" if time.time() > row["lease_until"] + 3600 else "RETRY_PENDING"
            return response(200, {"job_id": job, "status": status,
                "error_type": row.get("error_type"),
                "report_url": f"/reports/{job}/report.json" if status == "COMPLETE" else None})
        if method == "GET" and path == "/demo.mp4":
            url = s3.generate_presigned_url("get_object", Params={"Bucket": os.environ["UPLOAD_BUCKET"],
                "Key": "demo/demo.mp4"}, ExpiresIn=300)
            return {"statusCode": 302, "headers": {"location": url, "cache-control": "no-store"}, "body": ""}
        if method == "GET" and (path == "/report.json" or re.fullmatch(r"/reports/[a-f0-9]{64}/(report.json|metrics.csv|contact-sheet.jpg)", path)):
            key = "demo/report.json" if path == "/report.json" else path[1:]
            obj = s3.get_object(Bucket=os.environ["REPORT_BUCKET"], Key=key)
            try:
                content = obj["Body"].read(4 * 1024 * 1024 + 1)
            finally:
                obj["Body"].close()
            if len(content) > 4 * 1024 * 1024:
                return response(413, {"error": "Artifact exceeds public viewer limit"})
            return response(200, content, obj.get("ContentType", "application/octet-stream"), binary=True)
        return response(404, {"error": "Not found"})
    except (ValueError, TypeError, AttributeError):
        return response(400, {"error": "Invalid request"})
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404"}:
            return response(404, {"error": "Report is not available yet"})
        print(json.dumps({"request_id": getattr(context, "aws_request_id", ""), "error_type": type(exc).__name__, "aws_code": code}))
        return response(503, {"error": "Service temporarily unavailable"})
