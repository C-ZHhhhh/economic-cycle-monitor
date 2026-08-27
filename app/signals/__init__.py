"""Stage 3B macro direction and normalization domain."""

from app.signals.config import load_signal_catalog
from app.signals.service import (
    SignalEngine,
    compute_feature_signals,
    compute_signal,
    compute_signal_set,
)

__all__ = [
    "SignalEngine",
    "compute_feature_signals",
    "compute_signal",
    "compute_signal_set",
    "load_signal_catalog",
]
