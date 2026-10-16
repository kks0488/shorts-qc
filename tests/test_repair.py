import hashlib
import json
import subprocess

import cv2 as cv
import numpy as np
import pytest

from shorts_qc.repair import repair, retained_intervals
from shorts_qc.synthetic import _scene


def test_intervals_merge_overlap():
    report = {"source": {"duration": 5, "fps": 12}, "issues": [
        {"type": "black", "start": 1, "end": 2},
        {"type": "frozen", "start": 1.5, "end": 3},
        {"type": "dark", "start": 4, "end": 5}]}
    assert retained_intervals(report, {"black", "frozen"}) == [(0., 1), (3, 5)]


@pytest.fixture
def audio_clip(tmp_path):
    source = tmp_path/"source.mp4"
    silent = tmp_path/"silent.mp4"
    writer = cv.VideoWriter(str(silent), cv.VideoWriter_fourcc(*"mp4v"), 12, (324, 576))
    for i in range(36):
        frame = _scene(810, i)
        if 12 <= i < 18:
            frame[:] = 0
        writer.write(frame)
    writer.release()
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(silent), "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", "-c:v", "copy", "-c:a", "aac", "-shortest", str(source)], check=True, timeout=30)
    return source


def test_real_repair_trims_and_reverifies_audio(audio_clip, tmp_path):
    original_hash = hashlib.sha256(audio_clip.read_bytes()).hexdigest()
    trace = repair(audio_clip, tmp_path/"fixed")
    before = json.loads((tmp_path/"fixed/before/report.json").read_text())
    after = json.loads((tmp_path/"fixed/after/report.json").read_text())
    assert "black" in before["summary"]["types"]
    assert "black" not in after["summary"]["types"]
    assert after["source"]["duration"] == pytest.approx(2.5, abs=.09)
    assert after["source"]["width"]/after["source"]["height"] == 9/16
    assert hashlib.sha256(audio_clip.read_bytes()).hexdigest() == original_hash
    assert trace["original_preserved"]
    assert trace["steps"][-1]["tool"] == "opencv.analyze"
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json",
                                     str(tmp_path/"fixed/repaired.mp4")], check=True, capture_output=True, text=True).stdout)
    assert len(probe["streams"]) == 2
    durations = [float(s["duration"]) for s in probe["streams"]]
    assert abs(durations[0]-durations[1]) < .1
    with pytest.raises(ValueError, match="already exists"):
        repair(audio_clip, tmp_path/"fixed")


def test_repair_refuses_empty_candidate(tmp_path):
    source = tmp_path/"black.mp4"
    writer = cv.VideoWriter(str(source), cv.VideoWriter_fourcc(*"mp4v"), 12, (324, 576))
    for _ in range(12):
        writer.write(np.zeros((576, 324, 3), np.uint8))
    writer.release()
    with pytest.raises(ValueError, match="no video remains"):
        repair(source, tmp_path/"empty")


def test_reference_restores_flagged_spans_without_changing_timing(audio_clip, tmp_path):
    reference = tmp_path/"reference.mp4"
    writer = cv.VideoWriter(str(reference), cv.VideoWriter_fourcc(*"mp4v"), 12, (324, 576))
    for i in range(36):
        writer.write(_scene(810, i))
    writer.release()
    source_hash = hashlib.sha256(audio_clip.read_bytes()).hexdigest()
    trace = repair(audio_clip, tmp_path/"restored", reference=reference)
    after = json.loads((tmp_path/"restored/after/report.json").read_text())
    assert "black" not in after["summary"]["types"]
    assert after["source"]["frames"] == 36
    assert after["source"]["duration"] == 3
    assert trace["reference_sha256"] == hashlib.sha256(reference.read_bytes()).hexdigest()
    assert hashlib.sha256(audio_clip.read_bytes()).hexdigest() == source_hash
    streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json",
        str(tmp_path/"restored/repaired.mp4")], check=True, capture_output=True, text=True).stdout)["streams"]
    assert {s["codec_type"] for s in streams} == {"audio", "video"}
    assert abs(float(streams[0]["duration"])-float(streams[1]["duration"])) < .1
    with pytest.raises(ValueError, match="already exists"):
        repair(audio_clip, tmp_path/"restored", reference=reference)


def test_reference_must_match_frame_count(audio_clip, tmp_path):
    reference = tmp_path/"short-reference.mp4"
    writer = cv.VideoWriter(str(reference), cv.VideoWriter_fourcc(*"mp4v"), 12, (324, 576))
    for i in range(24):
        writer.write(_scene(810, i))
    writer.release()
    with pytest.raises(ValueError, match="match dimensions, frame rate and frame count"):
        repair(audio_clip, tmp_path/"refused", reference=reference)
    assert not (tmp_path/"refused/repaired.mp4").exists()


def test_live_gate_rejects_empty_incomplete_or_unrepaired_datasets():
    import runpy
    from pathlib import Path

    module = runpy.run_path(str(Path(__file__).parents[1]/"scripts/realism-live.py"))
    coverage = module["gate_coverage"]
    assert not all(coverage({"clips": []}, [], [], True).values())
    damaged = [dict(file=f"damaged-{i}.mp4", defects={k: [[1, 2]] for k in module["KINDS"]}) for i in range(3)]
    manifest = {"clips": [*damaged, dict(file="clean.mp4", defects={})]}
    results = [*manifest["clips"], *[dict(repaired_from=c["file"]) for c in damaged]]
    checks = [dict(pass_gate=True) for _ in results]
    assert all(coverage(manifest, results, checks, True).values())
    assert not all(coverage(manifest, results[:-1], checks[:-1], True).values())
    assert not all(coverage(manifest, results, checks, False).values())
    checks[-1]["pass_gate"] = False
    assert not all(coverage(manifest, results, checks, True).values())
