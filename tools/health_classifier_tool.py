# tools/health_classifier_tool.py

import json
import os
from typing import Any, Dict, List

import joblib
import numpy as np


# Path to the trained classifier created by train_health_classifier.ipynb
DEFAULT_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models",
    "health_classifier.joblib",
)

_model_bundle: Dict[str, Any] | None = None


def _load_model_bundle(model_path: str = DEFAULT_MODEL_PATH) -> Dict[str, Any]:
    """Load the trained health classifier bundle (pipeline + metadata)."""
    global _model_bundle
    if _model_bundle is None:
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Health classifier model file not found at {model_path}. "
                "Please run train_health_classifier.ipynb to create it."
            )
        _model_bundle = joblib.load(model_path)
    return _model_bundle


def _build_explanation(
    features: Dict[str, float],
    is_healthy: bool,
) -> Dict[str, Any]:
    """Build a simple human-readable explanation based on nutrient thresholds.

    The goal is not to be medically perfect, but to give intuitive reasons like
    "low fiber" or "high sugar" that the LLM can reuse in its answer.
    """
    strengths: List[str] = []
    weaknesses: List[str] = []

    calories = features.get("calories", 0.0)
    protein_g = features.get("protein_g", 0.0)
    carbs_g = features.get("carbs_g", 0.0)
    fat_g = features.get("fat_g", 0.0)
    fiber_g = features.get("fiber_g", 0.0)
    sugar_g = features.get("sugar_g", 0.0)
    sodium_mg = features.get("sodium_mg", 0.0)

    # Energy
    if calories > 900:
        weaknesses.append("High energy (calorie-dense meal).")
    elif calories < 250:
        weaknesses.append("Very low energy; might not be satiating.")
    else:
        strengths.append("Energy content in a reasonable range for a single meal.")

    # Protein
    if protein_g >= 20:
        strengths.append("Good protein intake.")
    elif protein_g < 10:
        weaknesses.append("Low protein content.")

    # Fiber
    if fiber_g >= 5:
        strengths.append("Good fiber intake.")
    else:
        weaknesses.append("Low fiber content.")

    # Sugar
    if sugar_g > 30:
        weaknesses.append("High sugar content.")
    elif sugar_g <= 15:
        strengths.append("Moderate sugar level.")

    # Fat
    if fat_g > 35:
        weaknesses.append("High fat content.")
    elif fat_g <= 20:
        strengths.append("Moderate fat content.")

    # Sodium
    if sodium_mg > 800:
        weaknesses.append("High sodium (salt) content.")
    elif sodium_mg <= 600:
        strengths.append("Moderate sodium level.")

    if is_healthy:
        summary = (
            "This meal is classified as rather healthy according to the classifier. "
            "It has more strengths than weaknesses overall."
        )
    else:
        summary = (
            "This meal is classified as rather unhealthy according to the classifier. "
            "The weaknesses listed above explain why."
        )

    return {
        "strengths": strengths,
        "weaknesses": weaknesses,
        "summary": summary,
    }


def evaluate_meal_healthiness(payload: str) -> str:
    """Tool entry point used by the LLM.

    Input (payload, as JSON string) must describe a SINGLE MEAL, not the whole day.

    Expected JSON structure:
        {
          "meal_label": "lunch",
          "calories": 650,
          "protein_g": 25,
          "carbs_g": 60,
          "fat_g": 30,
          "fiber_g": 5,
          "sugar_g": 10,
          "sodium_mg": 800
        }

    Output (JSON string):
        {
          "meal_label": "lunch",
          "features": { ... },
          "prediction": {
            "is_healthy": true,
            "probability_healthy": 0.78,
            "decision_threshold": 0.5
          },
          "analysis": {
            "strengths": [...],
            "weaknesses": [...],
            "summary": "..."
          }
        }
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return json.dumps(
            {
                "error": "Invalid JSON payload for evaluate_meal_healthiness.",
                "raw_payload": payload,
            }
        )

    bundle = _load_model_bundle()
    pipeline = bundle["pipeline"]
    feature_columns: List[str] = bundle.get(
        "feature_columns",
        ["calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg"],
    )
    threshold: float = float(bundle.get("decision_threshold", 0.5))

    # Collect features from payload, defaulting missing fields to 0.0
    features: Dict[str, float] = {}
    for col in feature_columns:
        raw_val = data.get(col, 0.0)
        try:
            features[col] = float(raw_val)
        except (TypeError, ValueError):
            features[col] = 0.0

    X = np.array([[features[col] for col in feature_columns]], dtype=float)

    proba_healthy = float(pipeline.predict_proba(X)[0, 1])
    is_healthy = proba_healthy >= threshold

    meal_label = str(data.get("meal_label", ""))

    analysis = _build_explanation(features, is_healthy)

    result = {
        "meal_label": meal_label,
        "features": features,
        "prediction": {
            "is_healthy": is_healthy,
            "probability_healthy": proba_healthy,
            "decision_threshold": threshold,
        },
        "analysis": analysis,
    }

    return json.dumps(result)
