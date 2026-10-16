"""Download external model assets, never vendor their code or media."""
import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen

COMMIT = "47534e27c9851bb1128ccc0102f1145e27f23f98"
ROOT = f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{COMMIT}/models/text_detection_ppocr/"
MODEL = "text_detection_en_ppocrv3_2023may.onnx"
SHA256 = "03f550c6b406fda8bf54bd8327815f6c7e2edd98cea02348c93d879254366587"
LICENSE_URL = f"https://raw.githubusercontent.com/opencv/opencv_zoo/{COMMIT}/models/text_detection_ppocr/LICENSE"


def download(output):
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(ROOT + MODEL, timeout=90) as response:
        data = response.read(4_000_000)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("Model checksum mismatch")
    with urlopen(LICENSE_URL, timeout=30) as response:
        license_text = response.read(40_000)
    target.write_bytes(data)
    target.with_suffix(".LICENSE").write_bytes(license_text)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="models/text.onnx")
    print(download(parser.parse_args().output))
