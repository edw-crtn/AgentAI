# ui.py

import streamlit as st

from app import CarbonAgent


def get_agent() -> CarbonAgent:
    """
    Retrieve a singleton CarbonAgent from Streamlit session state.
    Warm-up (RAG embeddings build/load) happens when the agent is first created.
    """
    if "carbon_agent" not in st.session_state:
        with st.spinner(
            "Loading food database and preparing embeddings (first time may take a bit)..."
        ):
            st.session_state["carbon_agent"] = CarbonAgent()
    return st.session_state["carbon_agent"]


def main() -> None:
    st.set_page_config(
        page_title="Food CO2 Assistant",
        page_icon="CO2",
        layout="wide",
    )

    st.title("Personal Food Carbon Footprint Assistant")

    st.markdown(
        """
        This assistant helps you estimate the CO₂ emissions and basic nutrition
        of what you eat during the day.

        - You can describe your meals in natural language (breakfast, lunch, dinner, snacks).
        - Or you can upload a photo of your meal: the assistant will detect foods and quantities,
          then ask you to confirm or correct them directly in the chat.
        """
    )

    agent = get_agent()

    # Sidebar: image upload + analysis
    with st.sidebar:
        st.header("Meal photo")
        uploaded_file = st.file_uploader(
            "Upload a meal picture (JPEG or PNG).",
            type=["jpg", "jpeg", "png"],
        )
        if uploaded_file is not None:
            image_bytes = uploaded_file.read()
            st.image(image_bytes, caption="Uploaded meal")

            if st.button("Analyze picture"):
                with st.spinner("Analyzing image..."):
                    items = agent.analyze_image(image_bytes=image_bytes)

                if not items:
                    st.warning(
                        "The vision model did not return a valid list of foods. "
                        "You can still describe your meal manually in the chat."
                    )
                else:
                    # Save last detected items in session state (optional)
                    st.session_state["last_detected_items"] = items

                    # Build a human-readable summary
                    readable = ", ".join(
                        f"{round(it['mass_g'])} g {it['name']}" for it in items
                    )
                    bullet_lines = "\n".join(
                        f"- {round(it['mass_g'])} g {it['name']}" for it in items
                    )

                    # 1) Inject a "user" message describing what was detected
                    user_text = (
                        "From the photo, this is what the assistant detected for my meal: "
                        f"{readable}."
                    )
                    agent.messages.append({"role": "user", "content": user_text})
                    agent.display_history.append({"role": "user", "content": user_text})

                    # 2) Inject an "assistant" message asking for confirmation/correction
                    assistant_text = (
                        "I analyzed your meal picture and detected the following items:\n\n"
                        f"{bullet_lines}\n\n"
                        "Please confirm if this is correct, or send a message to adjust the foods "
                        "and quantities (for example: \"replace 150 g spaghetti with 200 g\" or "
                        "\"add 1 glass of orange juice (200 ml)\")."
                    )
                    agent.messages.append(
                        {"role": "assistant", "content": assistant_text}
                    )
                    agent.display_history.append(
                        {"role": "assistant", "content": assistant_text}
                    )

                    st.success(
                        "The detected meal description has been inserted into the chat. "
                        "Please confirm or correct it in the conversation."
                    )

    # Chat history
    for msg in agent.get_display_history():
        if msg["role"] == "user":
            with st.chat_message("user"):
                st.markdown(msg["content"])
        elif msg["role"] == "assistant":
            with st.chat_message("assistant"):
                st.markdown(msg["content"])

    # User text input
    user_input = st.chat_input("Describe what you ate for a meal…")
    if user_input:
        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                reply = agent.chat(user_input)
            st.markdown(reply)


if __name__ == "__main__":
    main()
