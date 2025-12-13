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
    analyze_meal_image,
    warm_up_rag,
    get_food_nutrition,
    evaluate_meal_healthiness,
)

load_dotenv()


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

    def get_display_history(self) -> List[Dict[str, str]]:
        return self.display_history

    def analyze_image(self, image_bytes: bytes) -> List[Dict[str, Any]]:
        return analyze_meal_image(image_bytes=image_bytes)

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
            response = self.client.chat.complete(
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
                self.display_history.append({"role": "assistant", "content": content})
                return content

            # Case 2: assistant is asking to call one or more tools
            self.messages.append(assistant_message)

            for tool_call in tool_calls:
                function_name = tool_call.function.name
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
