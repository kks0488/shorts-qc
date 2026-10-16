"""Cloud boundary tests use an in-memory AWS double; never need AWS credentials."""
from io import BytesIO
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

spec = importlib.util.spec_from_file_location("cloud_handler", Path(__file__).resolve().parents[1] / "infra" / "handler.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Table:
    def __init__(self):
        self.rows = {}

    def put_item(self, Item, **kwargs):
        previous = self.rows.get(Item["job_id"])
        if previous and (previous["status"] == "COMPLETE" or previous["lease_until"] >= kwargs["ExpressionAttributeValues"][":now"]):
            raise ConditionalFailure()
        self.rows[Item["job_id"]] = dict(Item)

    def update_item(self, Key, ExpressionAttributeValues, **kwargs):
        row = self.rows[Key["job_id"]]
        row["status"] = ExpressionAttributeValues[":s"]
        if ":l" in ExpressionAttributeValues:
            row["lease_until"] = ExpressionAttributeValues[":l"]


class Body(BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class S3:
    def __init__(self):
        self.uploads = {}
        self.size = 10
        self.etag = '"abc"'

    def head_object(self, **kwargs):
        return {"ETag": self.etag, "ContentLength": self.size}

    def get_object(self, **kwargs):
        assert kwargs["IfMatch"] == self.etag
        return {"Body": Body(b"own-video")}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        assert bucket == "reports"
        self.uploads[key] = Path(filename).read_bytes()


@pytest.fixture
def setup_cloud(monkeypatch):
    s3, table = S3(), Table()
    monkeypatch.setenv("UPLOAD_BUCKET", "uploads")
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setattr(cloud, "_clients", lambda: (s3, table))
    monkeypatch.setattr(cloud, "_validate_video", lambda path: None)

    def analyze(source, output):
        (output / "metrics.csv").write_text("frame,blur\n0,2\n")
        (output / "contact-sheet.jpg").write_bytes(b"fake-jpeg")
        return {"issues": [], "mode": "demo", "source": {"name": "input.mp4"}}

    monkeypatch.setattr(cloud, "_analyze", analyze)
    event = {"Records": [{"eventSource": "aws:s3", "s3": {
        "bucket": {"name": "uploads"}, "object": {"key": "incoming/our+clip.mp4", "eTag": "abc"}}}]}
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: 540000)
    return s3, table, event, context


def test_success_publishes_artifacts_and_deduplicates(setup_cloud):
    s3, table, event, context = setup_cloud
    first = cloud.handler(event, context)["jobs"][0]
    assert first["status"] == "complete"
    assert len(s3.uploads) == 3
    report = json.loads(s3.uploads[first["report_key"]])
    assert report["cloud"]["job_id"] == first["job_id"]
    assert report["mode"] == "aws-analysis"
    assert report["cloud_status"] == "deployed"
    assert table.rows[first["job_id"]]["upload_key"] == "incoming/our clip.mp4"
    assert cloud.handler(event, context)["jobs"][0]["status"] == "duplicate"


def test_failed_processing_is_retryable(setup_cloud, monkeypatch):
    s3, table, event, context = setup_cloud
    original = cloud._analyze
    monkeypatch.setattr(cloud, "_analyze", lambda *_: (_ for _ in ()).throw(RuntimeError("transient")))
    with pytest.raises(RuntimeError):
        cloud.handler(event, context)
    assert next(iter(table.rows.values()))["status"] == "FAILED"
    assert not s3.uploads
    monkeypatch.setattr(cloud, "_analyze", original)
    assert cloud.handler(event, context)["jobs"][0]["status"] == "complete"


def test_upload_size_is_checked_before_download(setup_cloud, monkeypatch):
    s3, table, event, context = setup_cloud
    s3.size = 104857601
    monkeypatch.setattr(s3, "get_object", lambda **_: pytest.fail("oversized object downloaded"))
    assert cloud.handler(event, context)["jobs"][0]["status"] == "failed"
    assert not s3.uploads


def test_overwritten_object_is_never_analyzed(setup_cloud):
    s3, table, event, context = setup_cloud
    s3.etag = '"new-content"'
    assert cloud.handler(event, context)["jobs"][0]["status"] == "superseded"
    assert not s3.uploads


def test_rejects_unexpected_bucket(setup_cloud):
    _, _, event, context = setup_cloud
    event["Records"][0]["s3"]["bucket"]["name"] = "someone-else"
    with pytest.raises(ValueError, match="Unexpected"):
        cloud.handler(event, context)


def test_download_stream_is_also_bounded(setup_cloud, monkeypatch):
    s3, _, event, context = setup_cloud
    s3.size = 1
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "3")
    assert cloud.handler(event, context)["jobs"][0]["status"] == "failed"
    assert not s3.uploads


def test_expired_processing_lease_can_recover(setup_cloud):
    _, table, event, context = setup_cloud
    job = cloud.handler(event, context)["jobs"][0]["job_id"]
    table.rows[job].update(status="PROCESSING", lease_until=0)
    assert cloud.handler(event, context)["jobs"][0]["status"] == "complete"


@pytest.mark.parametrize("fps,frames,pixels", [
    (0, 100, 100), (30, 0, 100), (61, 100, 100),
    (30, 3630, 100), (60, 7201, 100), (30, 100, 4_200_001),
    (float("nan"), 100, 100), (30, float("inf"), 100),
    (30, 100, float("nan")), (30, 100.5, 100),
])
def test_video_cloud_limits_release_decoder(monkeypatch, fps, frames, pixels):
    released = []
    values = {1: float(fps), 2: float(frames), 3: float(pixels), 4: 1.0}
    cap = SimpleNamespace(isOpened=lambda: True, get=values.get, release=lambda: released.append(True))
    fake_cv2 = SimpleNamespace(VideoCapture=lambda _: cap,
        CAP_PROP_FPS=1, CAP_PROP_FRAME_COUNT=2, CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4)
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    with pytest.raises(ValueError):
        cloud._validate_video("own.mp4")
    assert released == [True]


api_spec = importlib.util.spec_from_file_location("public_api", Path(__file__).resolve().parents[1] / "infra" / "api.py")
api = importlib.util.module_from_spec(api_spec)
api_spec.loader.exec_module(api)


class ApiTable:
    def __init__(self):
        self.used = 0
        self.rows = {}

    def update_item(self, **kwargs):
        if kwargs["Key"]["job_id"] != "UPLOAD_BUDGET":
            self.rows[kwargs["Key"]["job_id"]]["analysis_job"] = kwargs["ExpressionAttributeValues"][":job"]
            return
        if self.used >= 200:
            raise ConditionalFailure()
        self.used += 1

    def put_item(self, Item):
        self.rows[Item["job_id"]] = Item

    def get_item(self, Key, **kwargs):
        return {"Item": self.rows[Key["job_id"]]} if Key["job_id"] in self.rows else {}


@pytest.fixture
def public_api(monkeypatch):
    table = ApiTable()
    calls = []
    s3 = SimpleNamespace(generate_presigned_url=lambda *args, **kwargs: calls.append((args, kwargs)) or "https://s3.example/signed",
                         head_object=lambda **kwargs: {"ETag": '"abc"'})
    monkeypatch.setattr(api, "_clients", lambda: (s3, table))
    monkeypatch.setenv("UPLOAD_BUCKET", "uploads")
    monkeypatch.setenv("REPORT_BUCKET", "reports")
    monkeypatch.setattr(api.time, "time", lambda: 1792137600)
    return table, calls


def api_request(method, path, body=None):
    return api.handler({"rawPath": path, "requestContext": {"http": {"method": method}},
                        "body": json.dumps(body) if body is not None else None}, SimpleNamespace(aws_request_id="test"))


def test_public_upload_signs_checksum_size_and_single_write(public_api):
    table, calls = public_api
    result = api_request("POST", "/api/uploads", {"filename": "my.mp4", "size": 42, "sha256": "ab" * 32})
    assert result["statusCode"] == 201
    body = json.loads(result["body"])
    params = calls[0][1]["Params"]
    assert params["ContentLength"] == 42 and params["IfNoneMatch"] == "*"
    assert params["ChecksumSHA256"] == body["headers"]["x-amz-checksum-sha256"]
    assert body["headers"]["if-none-match"] == "*"
    assert calls[0][1]["ExpiresIn"] == 180
    assert table.used == 1
    assert api_request("GET", body["status_url"])["statusCode"] == 200
    assert json.loads(api_request("GET", body["status_url"])["body"])["status"] == "QUEUED"


@pytest.mark.parametrize("patch", [{"size": 0}, {"size": True}, {"size": 26214401},
    {"filename": "script.html"}, {"sha256": "oops"}])
def test_public_upload_validation_precedes_quota(public_api, patch):
    table, _ = public_api
    result = api_request("POST", "/api/uploads", {"filename": "my.mp4", "size": 42, "sha256": "ab" * 32, **patch})
    assert result["statusCode"] == 400
    assert table.used == 0


def test_lifetime_cap_is_atomic_and_not_reset_by_job_expiry(public_api):
    table, calls = public_api
    table.used = 200
    result = api_request("POST", "/api/uploads", {"filename": "my.mp4", "size": 42, "sha256": "ab" * 32})
    assert result["statusCode"] == 429
    assert not calls


def test_upload_window_closes_after_judging(public_api, monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: api.DEMO_END)
    assert api_request("POST", "/api/uploads", {})["statusCode"] == 403
    assert public_api[0].used == 0


def test_unknown_job_and_artifact_paths_are_not_exposed(public_api):
    assert api_request("GET", "/api/jobs/" + "a" * 32)["statusCode"] == 404
    assert api_request("GET", "/reports/../../secret")["statusCode"] == 404


def test_presigner_includes_required_headers(monkeypatch):
    import boto3
    from botocore.config import Config
    from urllib.parse import urlparse, parse_qs
    s3 = boto3.client("s3", region_name="ap-southeast-2", aws_access_key_id="test", aws_secret_access_key="test",
                      config=Config(signature_version="s3v4"))
    url = s3.generate_presigned_url("put_object", Params={"Bucket": "test-bucket", "Key": "incoming/a.mp4",
        "ContentLength": 42, "ContentType": "application/octet-stream", "IfNoneMatch": "*",
        "ChecksumSHA256": "abc="}, ExpiresIn=180)
    signed = parse_qs(urlparse(url).query)["X-Amz-SignedHeaders"][0].split(";")
    assert {"content-length", "if-none-match", "x-amz-checksum-sha256", "content-type"}.issubset(signed)


def test_status_survives_source_expiry_and_exposes_stopped_workers(public_api, monkeypatch):
    table, _ = public_api
    token, job = "b" * 32, "c" * 64
    table.rows[f"upload:{token}"] = {"analysis_job": job}
    table.rows[job] = {"status": "PROCESSING", "lease_until": 1792137500}
    assert json.loads(api_request("GET", f"/api/jobs/{token}")["body"])["status"] == "RETRY_PENDING"
    monkeypatch.setattr(api.time, "time", lambda: 1792141201)
    assert json.loads(api_request("GET", f"/api/jobs/{token}")["body"])["status"] == "TIMED_OUT"
    table.rows[job]["status"] = "COMPLETE"
    assert json.loads(api_request("GET", f"/api/jobs/{token}")["body"])["status"] == "COMPLETE"


def test_public_api_presigns_regional_s3_without_preflight_redirect(monkeypatch):
    from urllib.parse import urlparse
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.setenv("JOBS_TABLE", "jobs")
    s3, _ = api._clients()
    url = s3.generate_presigned_url("put_object", Params={"Bucket": "test-bucket", "Key": "clip.mp4"})
    assert urlparse(url).hostname == "test-bucket.s3.ap-southeast-2.amazonaws.com"
