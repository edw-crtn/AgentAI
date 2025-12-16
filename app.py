# app.py

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from dotenv import load_dotenv
from mistralai import Mistral


from prompt import SYSTEM_PROMPT
from tools import (
    compute_meal_footprint,
    analyze_meal_image_with_usage,
    warm_up_rag,
    get_food_nutrition,
    evaluate_meal_healthiness,
)

load_dotenv()



class TokenTracker:
    """Accumulates token usage across all model calls in a Streamlit session.

    We rely on the `usage` field returned by the Mistral SDK. If usage is missing,
    we simply do not count tokens for that call.
    """

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.calls = 0
        self.vision_calls = 0
        self.vision_total_tokens = 0

    @staticmethod
    def _usage_to_dict(usage: Any) -> Dict[str, int]:
        if usage is None:
            return {}
        if isinstance(usage, dict):
            return {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}
        # SDK objects sometimes expose attributes
        out: Dict[str, int] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if hasattr(usage, key):
                val = getattr(usage, key)
                if isinstance(val, (int, float)):
                    out[key] = int(val)
        return out

    def add_from_mistral_response(self, resp: Any, *, is_vision: bool = False) -> None:
        usage = getattr(resp, "usage", None)
        usage_dict = self._usage_to_dict(usage)

        # Some SDK responses may nest usage; keep it robust
        if not usage_dict and isinstance(resp, dict):
            usage_dict = self._usage_to_dict(resp.get("usage"))

        if not usage_dict:
            return

        p = int(usage_dict.get("prompt_tokens", 0))
        c = int(usage_dict.get("completion_tokens", 0))
        t = int(usage_dict.get("total_tokens", 0)) or (p + c)

        self.prompt_tokens += p
        self.completion_tokens += c
        self.total_tokens += t
        self.calls += 1

        if is_vision:
            self.vision_calls += 1
            self.vision_total_tokens += t

    def summary(self) -> Dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "vision_calls": self.vision_calls,
            "vision_total_tokens": self.vision_total_tokens,
        }


def _tokens_to_co2_and_km(total_tokens: int) -> Dict[str, float]:
    """Convert tokens into a CO2 estimate and an equivalent car distance.

    The conversion factors are configurable via env vars so you can justify them in your report.
    Defaults are intentionally conservative and should be cited/justified in the report.
    """
    token_co2_g_per_1k = float(os.getenv("TOKEN_CO2_G_PER_1K", "0.4"))  # grams CO2 per 1K tokens
    car_co2_g_per_km = float(os.getenv("CAR_CO2_G_PER_KM", "120"))      # grams CO2 per km

    co2_g = (total_tokens / 1000.0) * token_co2_g_per_1k
    co2_kg = co2_g / 1000.0
    km = co2_g / car_co2_g_per_km if car_co2_g_per_km > 0 else 0.0

    return {
        "token_co2_g_per_1k": token_co2_g_per_1k,
        "car_co2_g_per_km": car_co2_g_per_km,
        "co2_g": co2_g,
        "co2_kg": co2_kg,
        "km": km,
    }

def _get_mistral_client() -> Mistral:
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        raise EnvironmentError("MISTRAL_API_KEY environment variable is not set.")
    return Mistral(api_key=api_key)


def _build_tools_spec() -> List[Dict[str, Any]]:
    """
    Build the function-calling tool specifications for Mistral.
    """
    tools: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "compute_meal_footprint",
                "description": (
                    "Compute the CO2 emissions of a single meal using a local Excel database. "
                    "Takes a JSON string 'payload' describing the meal and returns a JSON string "
                    "with detailed emissions per item and a subtotal. "
                    "IMPORTANT: You MUST use this tool whenever the user describes what they ate. "
                    "Convert all food descriptions into the expected JSON format with meal_label and items array."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "string",
                            "description": (
                                "JSON string with keys 'meal_label' (string: breakfast/lunch/dinner/snack) "
                                "and 'items' (array of objects with 'name' and 'mass_g'). "
                                "Example: "
                                "'{\"meal_label\": \"lunch\", \"items\": ["
                                "{\"name\": \"pork sausage\", \"mass_g\": 120}, "
                                "{\"name\": \"potatoes\", \"mass_g\": 150}]}'"
                            ),
                        }
                    },
                    "required": ["payload"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_food_nutrition",
                "description": (
                    "Retrieve basic nutrition information for a single food item "
                    "using the USDA FoodData Central API. "
                    "The result is per 100 g and includes energy (kcal), protein, fat, "
                    "carbohydrates, sugars, fiber and sodium when available."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "food_name": {
                            "type": "string",
                            "description": (
                                "Short English name of the food, for example "
                                "'cow milk', 'orange', 'banana', 'spaghetti', "
                                "'pork sausage', 'plain croissant'."
                            ),
                        }
                    },
                    "required": ["food_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "evaluate_meal_healthiness",
                "description": (
                    "Classify a SINGLE MEAL as healthy or unhealthy using a small ML model "
                    "trained on a healthy eating dataset. "
                    "Input must contain the total nutrition values for that meal only "
                    "(not the whole day), and the tool returns a prediction plus an "
                    "analysis of strengths and weaknesses (for example low fiber, high sugar, etc.)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "string",
                            "description": (
                                "JSON string with keys 'meal_label' and numeric totals for that meal: "
                                "calories, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg. "
                                "Example: "
                                "'{\"meal_label\": \"dinner\", \"calories\": 750, \"protein_g\": 30, "
                                "\"carbs_g\": 60, \"fat_g\": 28, \"fiber_g\": 7, "
                                "\"sugar_g\": 8, \"sodium_mg\": 900}'"
                            ),
                        }
                    },
                    "required": ["payload"],
                },
            },
        },
    ]
    return tools


@dataclass
class CarbonAgent:
    model: str = field(default_factory=lambda: os.getenv("MISTRAL_MODEL", "mistral-small-latest"))
    temperature: float = field(default_factory=lambda: float(os.getenv("MISTRAL_TEMPERATURE", "0.2")))
    client: Mistral = field(default_factory=_get_mistral_client)
    tools_spec: List[Dict[str, Any]] = field(default_factory=_build_tools_spec)
    messages: List[Any] = field(default_factory=list)
    display_history: List[Dict[str, str]] = field(default_factory=list)
    token_tracker: TokenTracker = field(default_factory=TokenTracker)
    _health_analysis_called: bool = False
    _token_report_sent: bool = False

    def __post_init__(self) -> None:
        # System prompt
        self.messages.append({"role": "system", "content": SYSTEM_PROMPT})

        # Map tool names -> Python functions
        self.names_to_functions: Dict[str, Any] = {
            "compute_meal_footprint": compute_meal_footprint,
            "get_food_nutrition": get_food_nutrition,
            "evaluate_meal_healthiness": evaluate_meal_healthiness,
        }

        # Warm up RAG (build or load vectorstore)
        warm_up_rag()

        # Initial assistant greeting shown in the chat BEFORE any user message
        intro_message = (
        "Hi! I am your personal food carbon footprint and nutrition assistant.\n\n"
        "I will help you estimate the CO2 emissions of your meals, compute basic "
        "nutrition values (calories, protein, carbs, fat, sugar, fiber, sodium), "
        "and, if you want, analyze how healthy each meal is using a small ML model.\n\n"
        "Let's go step by step through your day.\n"
        "To start, what did you have for breakfast today? Please list the foods "
        "and, if you can, approximate quantities in grams (for example: "
        "\"1 orange (130 g), 2 slices of whole wheat bread (60 g), 2 eggs (120 g)\")."
        )

        # We register this as an assistant message in the conversation history
        self.messages.append({"role": "assistant", "content": intro_message})
        self.display_history.append({"role": "assistant", "content": intro_message})
    def _mistral_chat(self, **kwargs):
        """Wrapper around `client.chat.complete` that also accumulates token usage."""
        resp = self.client.chat.complete(**kwargs)
        self.token_tracker.add_from_mistral_response(resp, is_vision=False)
        return resp
    
    def get_display_history(self) -> List[Dict[str, str]]:
        return self.display_history

    def analyze_image(self, image_bytes: bytes) -> List[Dict[str, Any]]:
        items, resp = analyze_meal_image_with_usage(image_bytes=image_bytes)
        # Track vision token usage if available
        self.token_tracker.add_from_mistral_response(resp, is_vision=True)
        return items


    def _render_token_report(self) -> str:
        stats = self.token_tracker.summary()
        conv = _tokens_to_co2_and_km(stats["total_tokens"])

        # Keep it short and explicit that this is an estimate.
        lines = []
        lines.append("### Token and CO₂ estimate for this conversation")
        lines.append(f"- Total tokens: **{stats['total_tokens']}** (prompt: {stats['prompt_tokens']}, completion: {stats['completion_tokens']})")
        if stats["vision_calls"] > 0:
            lines.append(f"- Vision tokens (included above): {stats['vision_total_tokens']} across {stats['vision_calls']} vision call(s)")
        lines.append(f"- Estimated CO₂ from tokens: **{conv['co2_g']:.2f} g CO₂e** ({conv['co2_kg']:.6f} kg)")
        lines.append(f"- Car-equivalent distance: **{conv['km']:.3f} km** (using {conv['car_co2_g_per_km']:.0f} g CO₂/km)")
        lines.append("")
        lines.append("_Note: This token→CO₂ conversion uses configurable factors. "
                     "You can adjust TOKEN_CO2_G_PER_1K and CAR_CO2_G_PER_KM in your .env to match your report assumptions._")
        return "\n".join(lines)


    def _run_one_step_with_tools(self) -> str:
        """
        Run one logical assistant step, allowing the model to:
        - call one or several tools,
        - get their results,
        - and finally produce a normal assistant message.

        We loop a few times to allow chained tool calls (e.g. CO2 -> nutrition -> ML classifier)
        in a single user turn, while always keeping the number of tool_calls and tool responses
        perfectly aligned (so Mistral does not raise 'Not the same number of function calls and responses').
        """
        max_tool_loops = 5

        for _ in range(max_tool_loops):
            response = self._mistral_chat(
                model=self.model,
                messages=self.messages,
                tools=self.tools_spec,
                tool_choice="auto",
                temperature=self.temperature,
            )

            assistant_message = response.choices[0].message
            tool_calls = getattr(assistant_message, "tool_calls", None) or []

            # Case 1: no tool calls -> final answer
            if not tool_calls:
                self.messages.append(assistant_message)
                content = assistant_message.content or ""
                # After the ML healthiness analysis, append token/CO2 stats once.
                if self._health_analysis_called and not self._token_report_sent:
                    content = content + "\n\n" + self._render_token_report()
                    self._token_report_sent = True
                self.display_history.append({"role": "assistant", "content": content})
                return content

            # Case 2: assistant is asking to call one or more tools
            self.messages.append(assistant_message)

            for tool_call in tool_calls:
                function_name = tool_call.function.name
                if function_name == "evaluate_meal_healthiness":
                    self._health_analysis_called = True
                raw_args = tool_call.function.arguments

                print(f"[DEBUG] Tool called: {function_name}")
                print(f"[DEBUG] Raw arguments: {raw_args}")

                # Parse arguments JSON
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError as e:
                    print(f"[ERROR] Failed to parse tool arguments: {e}")
                    function_result = json.dumps(
                        {
                            "error": "Tool arguments were not valid JSON.",
                            "raw_arguments": raw_args,
                        }
                    )
                else:
                    fn = self.names_to_functions.get(function_name)
                    if fn is None:
                        function_result = json.dumps(
                            {
                                "error": f"Unknown tool {function_name}",
                                "raw_arguments": args,
                            }
                        )
                    else:
                        try:
                            # Normal case: function defined with keyword arguments
                            function_result = fn(**args)
                        except TypeError:
                            # Fallback: some tools may expect a single positional argument
                            try:
                                function_result = fn(args)
                            except Exception as exc:
                                print(f"[ERROR] Tool execution failed: {exc}")
                                function_result = json.dumps(
                                    {
                                        "error": f"Exception while running tool {function_name}: {exc}",
                                        "raw_arguments": args,
                                    }
                                )
                        except Exception as exc:
                            print(f"[ERROR] Tool execution failed: {exc}")
                            function_result = json.dumps(
                                {
                                    "error": f"Exception while running tool {function_name}: {exc}",
                                    "raw_arguments": args,
                                }
                            )

                print(f"[DEBUG] Tool result: {str(function_result)[:200]}...")

                # IMPORTANT: one tool message per tool_call, with matching tool_call_id
                self.messages.append(
                    {
                        "role": "tool",
                        "name": function_name,
                        "tool_call_id": tool_call.id,
                        "content": function_result,
                    }
                )

        # If we exit the loop without a final assistant message
        fallback = "I'm sorry, something went wrong while coordinating tools. Please try rephrasing your last message."
        self.display_history.append({"role": "assistant", "content": fallback})
        return fallback

    def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        self.display_history.append({"role": "user", "content": user_message})
        return self._run_one_step_with_tools()
