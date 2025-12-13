# tools/fooddata_central_tool.py

import json
import os
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

FOODDATA_API_KEY_ENV = "FOODDATA_API_KEY"
API_BASE_URL = "https://api.nal.usda.gov/fdc/v1"

# Nutrient numbers in FoodData Central:
# 208: Energy (kcal)
# 203: Protein
# 204: Total lipid (fat)
# 205: Carbohydrate, by difference
# 269: Sugars, total including NLEA
# 291: Fiber, total dietary
# 606: Fatty acids, total saturated
# 307: Sodium, Na
TARGET_NUTRIENTS = {
    "208": "energy_kcal",
    "203": "protein_g",
    "204": "fat_g",
    "205": "carbohydrate_g",
    "269": "sugars_g",
    "291": "fiber_g",
    "606": "saturated_fat_g",
    "307": "sodium_mg",
}

_nutrition_cache: Dict[str, Dict[str, Any]] = {}


def _get_api_key() -> str:
    api_key = os.getenv(FOODDATA_API_KEY_ENV)
    if not api_key:
        raise EnvironmentError(
            f"{FOODDATA_API_KEY_ENV} environment variable is not set. "
            "Please put your FoodData Central API key in the .env file."
        )
    return api_key


def _choose_best_food(foods: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
    """
    Choose the best matching food from the search results.

    Heuristic:
    - First, try to keep only foods whose description contains ALL query tokens.
    - Then, prefer generic data types (SR Legacy, Survey (FNDDS), Foundation).
    - Within that, prefer higher search score (if available).
    """
    if not foods:
        return None

    query_tokens = [t for t in (query or "").lower().split() if t]

    def matches_tokens(food: Dict[str, Any]) -> bool:
        description = (food.get("description") or "").lower()
        if not query_tokens:
            return True
        return any(tok in description for tok in query_tokens)

    # Filter by description containing all tokens if possible
    filtered = [f for f in foods if matches_tokens(f)]
    if filtered:
        foods_to_consider = filtered
    else:
        foods_to_consider = foods

    priority_order = {
        "SR Legacy": 0,
        "Survey (FNDDS)": 1,
        "Foundation": 2,
    }

    def sort_key(food: Dict[str, Any]) -> Any:
        data_type = food.get("dataType") or ""
        priority = priority_order.get(data_type, 99)
        score = food.get("score")
        try:
            score_val = float(score) if score is not None else 0.0
        except (TypeError, ValueError):
            score_val = 0.0
        # sort ascending; use -score to keep highest score first
        return (priority, -score_val)

    sorted_foods = sorted(foods_to_consider, key=sort_key)
    return sorted_foods[0] if sorted_foods else None


def _search_food_in_fdc(food_name: str) -> Optional[Dict[str, Any]]:
    """
    Call the FoodData Central search endpoint for a given food name and
    return the single best matching food entry metadata, or None if nothing is found.
    """
    api_key = _get_api_key()
    params = {
        "query": food_name,
        "pageSize": 10,
        "api_key": api_key,
    }

    response = requests.get(
        f"{API_BASE_URL}/foods/search",
        params=params,
        timeout=5,
    )
    response.raise_for_status()
    data = response.json()
    foods = data.get("foods") or []
    return _choose_best_food(foods, food_name)


def _get_food_details(fdc_id: int) -> Dict[str, Any]:
    """
    Fetch detailed information for a specific food by FDC ID.

    We call the Food Details endpoint in FULL format (default), so that each
    entry in "foodNutrients" has a nested "nutrient" object with fields
    "number", "name", "unitName", etc.
    """
    api_key = _get_api_key()
    params = {
        "api_key": api_key,
        # no 'format': we use full detail format
        # no 'nutrients' filter: we fetch everything and pick what we need
    }

    response = requests.get(
        f"{API_BASE_URL}/food/{fdc_id}",
        params=params,
        timeout=5,
    )
    response.raise_for_status()
    return response.json()



def _extract_basic_nutrients(food_detail: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Extract a small set of key nutrients from a FoodData Central food detail entry.

    For the full format of /v1/food/{fdcId}, the structure is generally:

      "foodNutrients": [
        {
          "nutrient": {
            "id": 1003,
            "number": "203",
            "name": "Protein",
            "unitName": "g",
            ...
          },
          "amount": 3.2,
          ...
        },
        ...
      ]

    We map certain nutrient numbers (208, 203, 204, 205, 269, 291, 606, 307)
    to our own keys and keep value + unit + descriptive name.
    Values are per 100 g of food.
    """
    result: Dict[str, Dict[str, Any]] = {}
    nutrients = food_detail.get("foodNutrients") or []

    for fn in nutrients:
        nutrient_info = fn.get("nutrient") or {}
        number = str(nutrient_info.get("number"))
        if number not in TARGET_NUTRIENTS:
            continue

        key = TARGET_NUTRIENTS[number]
        amount = fn.get("amount")
        unit = nutrient_info.get("unitName")
        name = nutrient_info.get("name")

        if amount is None:
            continue

        result[key] = {
            "value": float(amount),
            "unit": unit,
            "name": name,
        }

    return result



def get_food_nutrition(food_name: str) -> str:
    """
    Tool entry point used by the LLM.

    Input:
        food_name: the human-readable name of a food item
                   (for example "cow milk", "boiled potatoes", "pork sausage").

    Output:
        JSON string with the structure:
        {
          "food_name_query": "...",
          "found": true/false,
          "fdc_id": ...,
          "description": "...",
          "data_type": "...",
          "food_category": "...",
          "nutrients_per_100g": {
            "energy_kcal": {"value": 61.0, "unit": "kcal", "name": "Energy"},
            "protein_g": {"value": 3.15, "unit": "g", "name": "Protein"},
            ...
          },
          "notes": "..."
        }

    If an error occurs or no food is found, found is false and notes explains why.
    """
    query = (food_name or "").strip()
    if not query:
        result = {
            "food_name_query": food_name,
            "found": False,
            "nutrients_per_100g": {},
            "notes": "No food name was provided.",
        }
        return json.dumps(result)

    cache_key = query.lower()
    if cache_key in _nutrition_cache:
        return json.dumps(_nutrition_cache[cache_key])

    try:
        food_meta = _search_food_in_fdc(query)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 403:
            error_result = {
                "food_name_query": query,
                "found": False,
                "nutrients_per_100g": {},
                "notes": (
                    "FoodData Central returned 403 Forbidden. "
                    "This usually means the API key is missing, invalid, or not a genuine FoodData Central key. "
                    "Please check that FOODDATA_API_KEY in the .env file contains the correct USDA FDC API key."
                ),
            }
        else:
            error_result = {
                "food_name_query": query,
                "found": False,
                "nutrients_per_100g": {},
                "notes": (
                    f"FoodData Central returned an HTTP error (status {status}): {exc}"
                ),
            }
        _nutrition_cache[cache_key] = error_result
        return json.dumps(error_result)
    except Exception as exc:
        error_result = {
            "food_name_query": query,
            "found": False,
            "nutrients_per_100g": {},
            "notes": (
                "Could not retrieve information from FoodData Central "
                f"because of a network or API error: {exc}"
            ),
        }
        _nutrition_cache[cache_key] = error_result
        return json.dumps(error_result)

    if food_meta is None:
        no_result = {
            "food_name_query": query,
            "found": False,
            "nutrients_per_100g": {},
            "notes": "FoodData Central did not return any food for this query.",
        }
        _nutrition_cache[cache_key] = no_result
        return json.dumps(no_result)

    fdc_id = food_meta.get("fdcId")
    if fdc_id is None:
        no_result = {
            "food_name_query": query,
            "found": False,
            "nutrients_per_100g": {},
            "notes": "Search result had no FDC ID; cannot fetch nutrient details.",
        }
        _nutrition_cache[cache_key] = no_result
        return json.dumps(no_result)

    # Fetch detailed nutrients
    try:
        details = _get_food_details(fdc_id)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        error_result = {
            "food_name_query": query,
            "found": False,
            "nutrients_per_100g": {},
            "notes": (
                f"Failed to fetch detailed nutrients for FDC ID {fdc_id}. "
                f"HTTP error (status {status}): {exc}"
            ),
        }
        _nutrition_cache[cache_key] = error_result
        return json.dumps(error_result)
    except Exception as exc:
        error_result = {
            "food_name_query": query,
            "found": False,
            "nutrients_per_100g": {},
            "notes": (
                f"Failed to fetch detailed nutrients for FDC ID {fdc_id} "
                f"because of a network or API error: {exc}"
            ),
        }
        _nutrition_cache[cache_key] = error_result
        return json.dumps(error_result)

    nutrients = _extract_basic_nutrients(details)

    result = {
        "food_name_query": query,
        "found": True,
        "fdc_id": fdc_id,
        "description": food_meta.get("description"),
        "data_type": food_meta.get("dataType"),
        "food_category": food_meta.get("foodCategory"),
        "nutrients_per_100g": nutrients,
        "notes": (
            "Nutrient values are per 100 g of the generic food as provided by "
            "USDA FoodData Central (abridged format). "
            "Only a small subset of key nutrients is returned."
        ),
    }

    _nutrition_cache[cache_key] = result
    return json.dumps(result)
