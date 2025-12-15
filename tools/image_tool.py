# tools/image_tool.py

import base64
import json
import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from mistralai import Mistral
from langsmith import traceable 

from prompt import IMAGE_ANALYSIS_PROMPT

# Load environment variables
load_dotenv()

VISION_MODEL_NAME = os.getenv("MISTRAL_VISION_MODEL", "pixtral-12b-2409")


def _get_mistral_client() -> Mistral:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise EnvironmentError("MISTRAL_API_KEY environment variable is not set.")
    return Mistral(api_key=api_key)

@traceable
def analyze_meal_image(image_bytes: bytes) -> List[Dict[str, Any]]:
    """
    Analyze a meal image using a vision-capable Mistral model (for example pixtral-12b-2409).

    Returns a list of items with fields:
      - name (string)
      - mass_g (float)

    If the model output cannot be parsed as the expected JSON, this function returns an empty list.
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
                {
                    "type": "image_url",
                    "image_url": {
                        "url": data_url
                    },
                },
            ],
        }
    ]

    response = client.chat.complete(
        model=VISION_MODEL_NAME,
        messages=messages,
    )

    text = response.choices[0].message.content

    # Print raw output for debugging in the Streamlit console
    print("Vision raw output:", text)

    # First attempt: assume the model obeyed and returned pure JSON
    try:
        parsed = json.loads(text)
    except Exception:
        # Second attempt: try to extract the first {...} block
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
        mass_g = float(item.get("mass_g", 0.0))
        if not name or mass_g <= 0:
            continue
        cleaned.append({"name": name, "mass_g": mass_g})
    return cleaned
