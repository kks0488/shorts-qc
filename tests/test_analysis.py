import csv
import json
import os
from pathlib import Path

import cv2 as cv
import pytest

from shorts_qc.analysis import TextDetector, analyze, caption_flags
from shorts_qc.config import Config
from shorts_qc.evaluate import evaluate_dataset
from shorts_qc.synthetic import DEFECTS, _caption, _frame, generate_dataset, generate_demo


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    path = tmp_path_factory.mktemp("dataset")
    manifest = generate_dataset(path, seed=711)
    return path, manifest


@pytest.fixture(scope="module")
def measured(dataset, tmp_path_factory):
    path, _ = dataset
    out = tmp_path_factory.mktemp("evaluation")
    return evaluate_dataset(path/"manifest.json", out), out


def test_seeded_splits_are_distinct(dataset):
    path, manifest = dataset
    assert len(manifest["clips"]) == 22
    assert len({clip["seed"] for clip in manifest["clips"]}) == 22
    assert all((path/clip["file"]).stat().st_size > 100 for clip in manifest["clips"])
    assert {clip["injected"] for clip in manifest["clips"]} == {"clean", *DEFECTS}


@pytest.mark.parametrize("kind", ["blur", "black", "dark", "frozen", "flicker", "scene_cut", "letterbox", "aspect_ratio"])
def test_injected_defects_and_negative_controls(measured, kind):
    result, _ = measured
    score = result["metrics"][kind]
    assert score["recall"] is not None and score["recall"] >= .90, (kind, score)
    assert score["precision"] is not None and score["precision"] >= .90, (kind, score)


def test_clean_negative_and_artifacts(measured):
    result, out = measured
    clean = next(clip for clip in result["clip_results"] if clip["injected"] == "clean")
    assert clean["detected_types"] == []
    report_path = out/clean["report"]
    report = json.loads(report_path.read_text())
    assert report["engine"]["version"].startswith("5.")
    assert report["caption_detection"]["status"] == "not run"
    assert report["summary"]["status"] == "needs_review"
    assert cv.imread(str(report_path.parent/"contact-sheet.jpg")).shape == (620, 960, 3)
    with (report_path.parent/"metrics.csv").open() as file:
        assert len(list(csv.DictReader(file))) == 24
    assert result["metrics"]["caption_clipped"]["status"].startswith("not evaluated")


def test_caption_geometry():
    cfg = Config()
    assert caption_flags([{"x": .10, "y": .20, "w": .5, "h": .1}], cfg) == (False, False)
    assert caption_flags([{"x": .10, "y": .85, "w": .5, "h": .1}], cfg) == (True, False)
    assert caption_flags([{"x": 0., "y": .2, "w": .5, "h": .1}], cfg) == (True, True)


def test_actual_dnn_caption_detection(dataset, tmp_path):
    model = os.environ.get("SHORTS_QC_TEXT_MODEL")
    if not model:
        pytest.skip("Set SHORTS_QC_TEXT_MODEL to run actual OpenCV DNN integration")
    detector = TextDetector(model)
    safe = _caption(_frame("clean", 510, 0, 24), "safe")
    assert detector.detect(safe), "Real DB network must find the original rendered text"
    assert caption_flags(detector.detect(safe), Config()) == (False, False)
    path, manifest = dataset
    for kind in ("caption_safe_zone", "caption_clipped"):
        clip = next(c for c in manifest["clips"] if c["split"] == "heldout" and c["injected"] == kind)
        report = analyze(path/clip["file"], tmp_path/kind, text_model=model)
        assert kind in report["summary"]["types"]
        assert report["caption_detection"]["status"] == "active"


def test_invalid_input_and_resource_limits(dataset, tmp_path):
    path, _ = dataset
    broken = tmp_path/"broken.mp4"
    broken.write_bytes(b"not a video")
    with pytest.raises(ValueError, match="cannot be decoded"):
        analyze(broken, tmp_path/"broken-report")
    with pytest.raises(ValueError, match="local video"):
        analyze(tmp_path/"missing.mp4", tmp_path/"missing-report")
    for cfg in (Config(max_frames=10), Config(max_seconds=.5)):
        with pytest.raises(ValueError, match="exceeds configured"):
            analyze(path/"heldout-clean.mp4", tmp_path/"limited", config=cfg)


@pytest.mark.parametrize("settings", [{"max_frames": 0}, {"max_seconds": float("nan")},
                                      {"safe_left": .9}, {"freeze_diff": -1}])
def test_invalid_configuration(settings):
    with pytest.raises(ValueError):
        Config(**settings)


def test_demo_is_twelve_seconds(tmp_path):
    path = tmp_path/"demo.mp4"
    manifest = generate_demo(path)
    cap = cv.VideoCapture(str(path))
    try:
        assert cap.get(cv.CAP_PROP_FRAME_COUNT) == 144
        assert manifest["duration"] == 12
    finally:
        cap.release()
