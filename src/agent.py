import logging

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent, AgentServer, AgentSession,
    JobContext, JobProcess, cli,
    inference, room_io, function_tool, RunContext,
)
from livekit.plugins import noise_cancellation, silero, bey
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from config import *

logger = logging.getLogger("agent")

load_dotenv()


class BookingAssistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=LLM_INSTRUCTIONS,
        )

    @function_tool
    async def crack_a_joke(self, context: RunContext, location: str):
        """Use this tool to look up current weather information in the given location and crack a joke.
        If the location is not supported by the weather joke service, the tool will indicate this. You must tell the user the location's weather is unavailable.
        Args:
            location: The location to look up weather information for (e.g. city name)
        """

        logger.info(f"Looking up weather for {location}")

        return "Sunny with a temperature of 70 degrees."


server = AgentServer()


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session()
async def my_agent(ctx: JobContext):
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    # Voice AI pipeline using OpenAI, Deepgram, Cartesia and the LiveKit turn detector
    session = AgentSession(
        stt=inference.STT(model=STT_MODEL_NAME, language=STT_MODEL_LANGUAGE),
        llm=inference.LLM(model=LLM_NAME),
        tts=inference.TTS(model=TTS_MODEL_NAME, voice=TTS_VOICE_ID),
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        preemptive_generation=ALLOW_PREEMPTIVE_GENERATION,
    )

    avatar = bey.AvatarSession(avatar_id=BEY_AVATAR_ID)
    await avatar.start(session, room=ctx.room)

    # Start session - initializes the voice pipeline and warms up the models
    await session.start(
        agent=BookingAssistant(),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=lambda params: noise_cancellation.BVCTelephony()
                if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                else noise_cancellation.BVC(),
            ),
        ),
    )

    await session.generate_reply(
        instructions="Greet the user and offer your assistance."
    )

    # Join the room and connect to the user
    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
