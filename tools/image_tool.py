# tools/image_tool.py

import base64
import json
import os
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv
from mistralai import Mistral

from prompt import IMAGE_ANALYSIS_PROMPT

# Load environment variables (.env)
load_dotenv()

VISION_MODEL_NAME = os.getenv("MISTRAL_VISION_MODEL", "pixtral-12b-2409")


def _get_mistral_client() -> Mistral:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise EnvironmentError("MISTRAL_API_KEY environment variable is not set.")
    return Mistral(api_key=api_key)


def _parse_items_from_model_text(text: str) -> List[Dict[str, Any]]:
    """Try to parse the vision model output into the expected JSON format."""
    # First attempt: assume the model returned pure JSON
    try:
        parsed = json.loads(text)
    except Exception:
        # Second attempt: extract the first {...} block
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            json_str = text[start : end + 1]
            try:
                parsed = json.loads(json_str)
            except Exception:
                return []
        else:
            return []

    items = parsed.get("items", [])
    cleaned: List[Dict[str, Any]] = []
    for item in items:
        name = str(item.get("name", "")).strip()
        try:
            mass_g = float(item.get("mass_g", 0.0))
        except Exception:
            mass_g = 0.0
        if not name or mass_g <= 0:
            continue
        cleaned.append({"name": name, "mass_g": mass_g})
    return cleaned


def analyze_meal_image_with_usage(image_bytes: bytes) -> Tuple[List[Dict[str, Any]], Any]:
    """Analyze a meal image and return (items, raw_response).

    - items: list of {name, mass_g}
    - raw_response: the Mistral SDK response (so the caller can read response.usage)
    """
    client = _get_mistral_client()

    # Encode image as data URL
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64}"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": IMAGE_ANALYSIS_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]

    response = client.chat.complete(
        model=VISION_MODEL_NAME,
        messages=messages,
    )

    text = response.choices[0].message.content or ""
    items = _parse_items_from_model_text(text)
    return items, response


def analyze_meal_image(image_bytes: bytes) -> List[Dict[str, Any]]:
    """Backward-compatible helper returning only the detected items."""
    items, _ = analyze_meal_image_with_usage(image_bytes=image_bytes)
    return items
