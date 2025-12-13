# ui.py

import io

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
        This assistant helps you estimate the CO2 emissions of what you eat during the day.
        
        - Describe your meals in natural language, or  
        - Upload a photo of your meal and let the agent detect the foods and quantities.
        """
    )

    agent = get_agent()

    # Sidebar for image upload
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
                    # Save last detected items in session, for quick reuse
                    st.session_state["last_detected_items"] = items
                    readable = ", ".join(
                        f"{round(it['mass_g'])} g {it['name']}" for it in items
                    )
                    st.success(f"Detected: {readable}")

                    st.info(
                        "You can now paste this into the chat or adapt it:\n\n"
                        f"\"I ate: {readable}.\""
                    )

    # Chat history
    for msg in agent.get_display_history():
        if msg["role"] == "user":
            with st.chat_message("user"):
                st.markdown(msg["content"])
        elif msg["role"] == "assistant":
            with st.chat_message("assistant"):
                st.markdown(msg["content"])

    # User input
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
