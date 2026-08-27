"""Stage 3A canonical US macro feature engine."""

from app.features.config import load_feature_catalog
from app.features.service import (
    FeatureEngine,
    compute_feature,
    compute_feature_set,
    compute_indicator_features,
)

__all__ = [
    "FeatureEngine",
    "compute_feature",
    "compute_feature_set",
    "compute_indicator_features",
    "load_feature_catalog",
]
