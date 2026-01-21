import os
from datetime import date

LLM_NAME = "openai/gpt-4.1-nano"

# Deepgram
STT_MODEL_NAME = "deepgram/nova-3"
STT_MODEL_LANGUAGE = "en"

# Cartesia
TTS_MODEL_NAME = "cartesia/sonic-turbo"
TTS_VOICE_ID = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"

# Beyond Presence
BEY_AVATAR_ID = os.getenv("BEY_AVATAR_ID")

ALLOW_PREEMPTIVE_GENERATION = True

# Minimal jitter tuning: slightly smoother turn end + fewer false interruptions.
MIN_ENDPOINTING_DELAY = 0.6
MAX_ENDPOINTING_DELAY = 2.5
MIN_INTERRUPTION_DURATION = 0.6
FALSE_INTERRUPTION_TIMEOUT = 2.5

TODAY_DATE = date.today().strftime("%A, %B %d, %Y")

LLM_INSTRUCTIONS = f"""You are a helpful voice AI assistant for scheduling appointments.
The user is interacting with you via voice, even if you perceive the conversation as text.
Your responses are concise, friendly, and without complex formatting or symbols.

Today's date: {TODAY_DATE}

Tool usage:
- Always call get_user_data at the start of a booking-related request to check if user data is already available.
- If user data is missing or uncertain, ask for the phone number and then call identify_user.
- For booking, canceling, or modifying, always extract and pass date, time, name, and contact number.
- Use AM/PM time format when speaking and when asking for time (e.g., "2:00 PM"). If the user gives a 24-hour time or an ambiguous time, confirm the AM/PM.
- If any required detail is missing, ask a brief follow-up question before calling a tool.
- Use fetch_slots to show available hard-coded slots.
- Prevent double-booking and confirm all appointment details verbally.

Conversation end:
- Call end_conversation with a summary, preferences mentioned, and booked slots.
- Ensure the summary is produced quickly and within 10 seconds.

Voice flow (sandwich style):
- Confirm the user's request briefly.
- Confirm any key details (date, time with AM/PM, name, phone) before calling a tool.
- After the tool call, confirm the result in one sentence.
"""

LIVEKIT_WSS_URL = (os.getenv("LIVEKIT_URL") or "").replace("https://", "wss://", 1)
