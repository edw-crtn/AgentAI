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
    tools = [
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
                                "Example: '{\"meal_label\": \"lunch\", \"items\": ["
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
                    "Fetch basic nutrition information for a single food from the USDA FoodData Central API. "
                    "Use this when the user asks about calories or nutrients of a specific food they ate."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "food_name": {
                            "type": "string",
                            "description": (
                                "Short generic name of a food item, such as "
                                "'cow milk', 'boiled potatoes', 'pork sausage', 'apple'."
                            ),
                        }
                    },
                    "required": ["food_name"],
                },
            },
        },
    ]
    return tools




@dataclass
class CarbonAgent:
    model: str = "mistral-small-latest"
    client: Mistral = field(default_factory=_get_mistral_client)
    tools_spec: List[Dict[str, Any]] = field(default_factory=_build_tools_spec)
    messages: List[Any] = field(default_factory=list)
    display_history: List[Dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.messages.append({"role": "system", "content": SYSTEM_PROMPT})
        
        self.names_to_functions = {
            "compute_meal_footprint": compute_meal_footprint,
            "get_food_nutrition": get_food_nutrition,
        }
        
        warm_up_rag()

    def get_display_history(self) -> List[Dict[str, str]]:
        return self.display_history

    def analyze_image(self, image_bytes: bytes) -> List[Dict[str, Any]]:
        return analyze_meal_image(image_bytes=image_bytes)

    def _run_one_step_with_tools(self) -> str:
        # First call with tools
        response = self.client.chat.complete(
            model=self.model,
            messages=self.messages,
            tools=self.tools_spec,
            tool_choice="auto",
        )

        assistant_message = response.choices[0].message
        tool_calls = getattr(assistant_message, "tool_calls", None)

        # If no tool calls, return the response directly
        if not tool_calls:
            self.messages.append(assistant_message)
            content = assistant_message.content or ""
            self.display_history.append({"role": "assistant", "content": content})
            return content

        # Process tool calls
        self.messages.append(assistant_message)
        
        for tool_call in tool_calls:
            function_name = tool_call.function.name
            raw_args = tool_call.function.arguments

            print(f"[DEBUG] Tool called: {function_name}")
            print(f"[DEBUG] Raw arguments: {raw_args}")

            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError as e:
                print(f"[ERROR] Failed to parse tool arguments: {e}")
                function_result = json.dumps({
                    "error": "Tool arguments were not valid JSON.",
                    "raw_arguments": raw_args,
                })
            else:
                fn = self.names_to_functions.get(function_name)
                if fn is None:
                    function_result = json.dumps({
                        "error": f"Unknown tool {function_name}",
                        "raw_arguments": args,
                    })
                else:
                    try:
                        function_result = fn(**args)
                        print(f"[DEBUG] Tool result: {function_result[:200]}...")
                    except Exception as exc:
                        print(f"[ERROR] Tool execution failed: {exc}")
                        function_result = json.dumps({
                            "error": f"Exception while running tool {function_name}: {exc}",
                            "raw_arguments": args,
                        })

            self.messages.append({
                "role": "tool",
                "name": function_name,
                "content": function_result,
                "tool_call_id": tool_call.id,
            })

        # Second call to get the final response after tool execution
        final_response = self.client.chat.complete(
            model=self.model,
            messages=self.messages,
        )
        
        final_message = final_response.choices[0].message
        self.messages.append(final_message)

        content = final_message.content or ""
        self.display_history.append({"role": "assistant", "content": content})
        return content

    def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        self.display_history.append({"role": "user", "content": user_message})
        return self._run_one_step_with_tools()