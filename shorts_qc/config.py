from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class Config:
    blur_laplacian: float = 65.0
    blur_fft: float = 0.012
    black_mean: float = 9.0
    black_fraction: float = 0.96
    dark_mean: float = 43.0
    freeze_diff: float = 0.65
    freeze_seconds: float = 0.6
    cut_diff: float = 32.0
    cut_ssim: float = 0.48
    flicker_delta: float = 22.0
    flicker_structure_diff: float = 12.0
    min_issue_seconds: float = 0.16
    text_interval: float = 0.5
    safe_left: float = 0.05
    safe_right: float = 0.82
    safe_top: float = 0.10
    safe_bottom: float = 0.78
    border_fraction: float = 0.02
    aspect_tolerance: float = 0.025
    max_seconds: float = 180.0
    max_frames: int = 9000
    max_dimension: int = 640

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid threshold: {name}")
        if not 0 <= self.safe_left < self.safe_right <= 1 or not 0 <= self.safe_top < self.safe_bottom <= 1:
            raise ValueError("Safe-zone bounds must be ordered fractions")
        if self.max_dimension < 64 or self.max_frames < 1 or self.max_seconds <= 0:
            raise ValueError("Analysis limits must be positive; max_dimension >= 64")
        if not isinstance(self.max_frames, int) or not isinstance(self.max_dimension, int):
            raise ValueError("Frame and dimension limits must be integers")

    def to_dict(self):
        return asdict(self)
