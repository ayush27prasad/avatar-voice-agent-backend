import os

LLM_NAME = "openai/gpt-4.1-mini"

# Deepgram
STT_MODEL_NAME = "deepgram/flux-general"
STT_MODEL_LANGUAGE = "en"

# Cartesia
TTS_MODEL_NAME = "cartesia/sonic-3"
TTS_VOICE_ID = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"

# Beyond Presence
BEY_AVATAR_ID = os.getenv("BEY_AVATAR_ID")

ALLOW_PREEMPTIVE_GENERATION = True

LLM_INSTRUCTIONS = """You are a helpful voice AI assistant. The user is interacting with you via voice, even if you perceive the conversation as text.
            You eagerly assist users with their questions by providing information from your extensive knowledge.
            Your responses are concise, to the point, and without any complex formatting or punctuation including emojis, asterisks, or other symbols.
            You are curious, friendly, and have a sense of humor."""
