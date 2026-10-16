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
        return {"issues": [], "mode": "demo"}

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
    with pytest.raises(ValueError, match="100 MiB"):
        cloud.handler(event, context)
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
    with pytest.raises(ValueError, match="during download"):
        cloud.handler(event, context)
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
