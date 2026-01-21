import asyncio
import json
import logging
import re

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    cli,
    inference,
    metrics,
    room_io,
)
from livekit.plugins import noise_cancellation, silero, bey
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from config import *
from tools import SessionData, estimate_call_cost, get_tools

logger = logging.getLogger("agent")

load_dotenv()


class BookingAssistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=LLM_INSTRUCTIONS,
            tools=get_tools(),
        )


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
        min_endpointing_delay=MIN_ENDPOINTING_DELAY,
        max_endpointing_delay=MAX_ENDPOINTING_DELAY,
        min_interruption_duration=MIN_INTERRUPTION_DURATION,
        false_interruption_timeout=FALSE_INTERRUPTION_TIMEOUT,
        userdata=SessionData(room=ctx.room),
    )

    avatar = bey.AvatarSession(avatar_id=BEY_AVATAR_ID)
    await avatar.start(session, livekit_url=LIVEKIT_WSS_URL, room=ctx.room)

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

    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: metrics.MetricsCollectedEvent):
        usage_collector.collect(ev.metrics)
        summary = usage_collector.get_summary()
        session.userdata.state.usage_summary = summary
        session.userdata.state.estimated_cost = estimate_call_cost(summary)

    await session.generate_reply(
        instructions="Greet the user and offer your assistance."
    )

    # Join the room and connect to the user
    await ctx.connect()

    async def _send_user_data_rpc(payload: dict) -> None:
        try:
            participant = payload.pop("_participant", None)
            if participant is None:
                # fallback: pick first remote participant
                remote = list(ctx.room.remote_participants.values())
                participant = remote[0] if remote else None
            if participant is None:
                logger.warning("No participant found for user data RPC")
                return
            await ctx.room.local_participant.perform_rpc(
                destination_identity=participant.identity,
                method="client.showNotification",
                payload=json.dumps(payload),
            )
        except Exception as exc:
            logger.warning("Failed to send user data RPC", exc_info=exc)

    def _notify_user_data(participant: rtc.Participant | None, phone: str | None, name: str | None, source: str) -> None:
        logger.info("User data updated", extra={"phone": phone, "full_name": name, "source": source})
        asyncio.create_task(
            _send_user_data_rpc(
                {
                    "_participant": participant,
                    "type": "user_data",
                    "status": "updated",
                    "data": {"phone": phone, "name": name, "source": source},
                }
            )
        )

    logger.info("Agent connected; waiting for participant to hydrate user context")
    # Hydrate user context from participant attributes/metadata
    try:
        participant = await ctx.wait_for_participant()
        logger.info(
            f"Participant joined",
            extra={"identity": participant.identity, "attributes": participant.attributes, "metadata": participant.metadata, "full": str(participant)},
        )
        attributes = participant.attributes or {}
        phone = attributes.get("user.phone") or attributes.get("user_phone") or attributes.get("phone")
        name = attributes.get("user.name") or attributes.get("user_name") or attributes.get("name")

        if participant.metadata and (not phone or not name):
            try:
                metadata = json.loads(participant.metadata)
                phone = phone or metadata.get("phone") or metadata.get("user_phone")
                name = name or metadata.get("name") or metadata.get("user_name")
            except Exception:
                pass

        if phone:
            session.userdata.state.contact_number = phone
            logger.info(
                "Loaded name from participant context",
                extra={"participant_phone": phone}
            )
        if name:
            session.userdata.state.name = name
            logger.info(
                "Loaded name from participant context",
                extra={"participant_name": name}
            )
        if phone or name:
            _notify_user_data(participant, phone, name, "participant_context")
    except Exception as exc:
        logger.warning("Failed to hydrate user context from participant", exc_info=exc)

    # Listen for user identification data
    @ctx.room.on("data_received")
    def on_data_received(data: rtc.DataPacket):
        try:
            message = data.data.decode("utf-8")
            logger.info("Received data message", extra={"message": message})
            
            # Extract phone and name from message
            if "phone number" in message.lower():
                # Extract phone number (10 digits)
                phone_match = re.search(r'\d{10}', message)
                if phone_match:
                    phone = phone_match.group(0)
                    session.userdata.state.contact_number = phone
                    logger.info("Pre-populated phone from welcome", extra={"phone": phone})
                
                # Extract name if present
                name_match = re.search(r'name is ([A-Za-z\s]+)', message, re.IGNORECASE)
                if name_match:
                    name = name_match.group(1).strip()
                    session.userdata.state.name = name
                    logger.info("Pre-populated name from welcome", extra={"user_name": name})
                if phone_match or name_match:
                    _notify_user_data(data.participant, session.userdata.state.contact_number, session.userdata.state.name, "data_packet")
        except Exception as exc:
            logger.warning("Failed to process data message", exc_info=exc)

    @ctx.room.on("participant_attributes_changed")
    def on_participant_attributes_changed(changed: dict[str, str], participant: rtc.Participant):
        try:
            phone = (
                changed.get("user.phone")
                or changed.get("user_phone")
                or changed.get("phone")
                or participant.attributes.get("user.phone")
                or participant.attributes.get("user_phone")
                or participant.attributes.get("phone")
            )
            name = (
                changed.get("user.name")
                or changed.get("user_name")
                or changed.get("name")
                or participant.attributes.get("user.name")
                or participant.attributes.get("user_name")
                or participant.attributes.get("name")
            )
            if phone:
                session.userdata.state.contact_number = phone
            if name:
                session.userdata.state.name = name
            if phone or name:
                _notify_user_data(participant, phone, name, "participant_attributes")
        except Exception as exc:
            logger.warning("Failed to process participant attributes change", exc_info=exc)

    @ctx.room.on("participant_metadata_changed")
    def on_participant_metadata_changed(participant: rtc.Participant, _old: str, new: str):
        try:
            parsed = json.loads(new) if new else {}
            phone = parsed.get("phone") or parsed.get("user_phone")
            name = parsed.get("name") or parsed.get("user_name")
            if phone:
                session.userdata.state.contact_number = phone
            if name:
                session.userdata.state.name = name
            if phone or name:
                _notify_user_data(participant, phone, name, "participant_metadata")
        except Exception as exc:
            logger.warning("Failed to process participant metadata change", exc_info=exc)


if __name__ == "__main__":
    cli.run_app(server)
