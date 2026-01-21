import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from livekit.agents import RunContext, ToolError, function_tool
from supabase import Client, create_client

logger = logging.getLogger("agent.tools")

DEFAULT_DAYS_AHEAD = 7
DEFAULT_TIME_SLOTS = ["10:00", "14:00", "16:00"]


@dataclass
class ConversationState:
    contact_number: str | None = None
    name: str | None = None
    preferences: list[str] = field(default_factory=list)
    booked_slots: list[dict[str, str]] = field(default_factory=list)


@dataclass
class SessionData:
    room: Any
    state: ConversationState = field(default_factory=ConversationState)


_supabase_client: Client | None = None


def _get_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        logger.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY environment variables")
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    logger.debug("Initializing Supabase client", extra={"url": url[:50] + "..." if len(url) > 50 else url})
    return create_client(url, key)


def _supabase() -> Client:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = _get_supabase_client()
    return _supabase_client


def _normalize_phone_number(contact_number: str) -> str:
    logger.debug("Normalizing phone number", extra={"input": contact_number})
    digits = "".join(ch for ch in contact_number if ch.isdigit())
    if len(digits) < 7:
        logger.warning("Invalid phone number format", extra={"input": contact_number, "digits": digits})
        raise ToolError("Ask the user for a valid phone number with country code.")
    logger.debug("Phone number normalized", extra={"normalized": digits})
    return digits


def _normalize_date(date_value: str) -> str:
    logger.debug("Normalizing date", extra={"input": date_value})
    try:
        parsed = datetime.strptime(date_value, "%Y-%m-%d").date()
        normalized = parsed.isoformat()
        logger.debug("Date normalized", extra={"normalized": normalized})
        return normalized
    except ValueError as exc:
        logger.warning("Invalid date format", extra={"input": date_value}, exc_info=exc)
        raise ToolError("Ask the user for a date in YYYY-MM-DD format.") from exc


def _normalize_time(time_value: str) -> str:
    logger.debug("Normalizing time", extra={"input": time_value})
    for fmt in ("%H:%M", "%I:%M %p"):
        try:
            parsed = datetime.strptime(time_value.strip(), fmt).time()
            normalized = parsed.strftime("%H:%M")
            logger.debug("Time normalized", extra={"normalized": normalized})
            return normalized
        except ValueError:
            continue
    logger.warning("Invalid time format", extra={"input": time_value})
    raise ToolError("Ask the user for a time like 14:00 or 2:00 PM.")


def _format_slot(slot_date: str, slot_time: str) -> str:
    return f"{slot_date} at {slot_time}"


def _resolve_participant(ctx: RunContext[SessionData]) -> Any | None:
    room = ctx.userdata.room
    try:
        room_io = ctx.session.room_io
        linked = getattr(room_io, "linked_participant", None)
        if linked is not None:
            return linked
    except Exception:
        pass
    remote_participants = list(room.remote_participants.values())
    if not remote_participants:
        return None
    return remote_participants[0]


def _extract_user_from_participant(participant: Any) -> tuple[str | None, str | None]:
    if participant is None:
        return None, None
    phone = None
    name = None
    try:
        attributes = participant.attributes or {}
        phone = (
            attributes.get("user.phone")
            or attributes.get("user_phone")
            or attributes.get("phone")
        )
        name = (
            attributes.get("user.name")
            or attributes.get("user_name")
            or attributes.get("name")
        )
    except Exception:
        pass

    if phone and name:
        return phone, name

    try:
        metadata = participant.metadata
        if metadata:
            parsed = json.loads(metadata)
            phone = phone or parsed.get("phone") or parsed.get("user_phone")
            name = name or parsed.get("name") or parsed.get("user_name")
    except Exception:
        pass

    return phone, name


def _fetch_user_by_phone(contact_number: str) -> dict[str, Any] | None:
    try:
        result = (
            _supabase()
            .table("users")
            .select("contact_number, name")
            .eq("contact_number", contact_number)
            .maybe_single()
            .execute()
        )
        return result.data if result.data else None
    except Exception as exc:
        logger.error("Failed to fetch user from database", exc_info=exc, extra={"contact": contact_number[:5] + "***"})
        return None


async def _send_rpc(ctx: RunContext[SessionData], payload: dict[str, Any]) -> None:
    try:
        room = ctx.userdata.room
        participant = _resolve_participant(ctx)
        if participant is None:
            logger.warning("No participant found to send RPC, skipping")
            return
        logger.debug(
            "Sending RPC notification",
            extra={"method": "client.showNotification", "type": payload.get("type"), "participant": participant.identity}
        )
        await room.local_participant.perform_rpc(
            destination_identity=participant.identity,
            method="client.showNotification",
            payload=json.dumps(payload),
        )
        logger.debug("RPC notification sent successfully")
    except Exception as exc:
        logger.error("Failed to send RPC payload", exc_info=exc, extra={"payload_type": payload.get("type")})


async def _notify_tool_event(
    ctx: RunContext[SessionData],
    *,
    tool: str,
    status: str,
    data: dict[str, Any] | None = None,
) -> None:
    payload = {
        "type": "tool_call",
        "tool": tool,
        "status": status,
        "data": data or {},
        "ts": datetime.utcnow().isoformat(),
    }
    await _send_rpc(ctx, payload)


def _default_slot_dates() -> list[str]:
    today = date.today()
    return [(today + timedelta(days=offset)).isoformat() for offset in range(DEFAULT_DAYS_AHEAD)]


def _generate_slots(dates: list[str]) -> list[dict[str, str]]:
    slots: list[dict[str, str]] = []
    for day in dates:
        for time_value in DEFAULT_TIME_SLOTS:
            slots.append({"date": day, "time": time_value})
    return slots


def _fetch_booked_slots(dates: list[str]) -> set[tuple[str, str]]:
    if not dates:
        logger.debug("No dates provided for booked slots lookup")
        return set()
    start_date = min(dates)
    end_date = max(dates)
    logger.debug("Fetching booked slots from database", extra={"start_date": start_date, "end_date": end_date})
    try:
        result = (
            _supabase()
            .table("appointments")
            .select("slot_date, slot_time, status")
            .gte("slot_date", start_date)
            .lte("slot_date", end_date)
            .neq("status", "cancelled")
            .execute()
        )
        rows = result.data or []
        booked = {(row["slot_date"], row["slot_time"]) for row in rows}
        logger.info("Fetched booked slots", extra={"count": len(booked), "dates_range": f"{start_date} to {end_date}"})
        return booked
    except Exception as exc:
        logger.error("Failed to fetch booked slots from database", exc_info=exc, extra={"start_date": start_date, "end_date": end_date})
        raise


def _ensure_contact_number(
    ctx: RunContext[SessionData], contact_number: str | None
) -> str:
    stored = ctx.userdata.state.contact_number
    if contact_number:
        return _normalize_phone_number(contact_number)
    if stored:
        return stored
    raise ToolError("Ask the user for their phone number to continue.")


def _ensure_user_exists(contact_number: str, name: str | None = None) -> None:
    """Internal: Create user in database if they don't exist."""
    logger.debug("Ensuring user exists in database", extra={"contact": contact_number[:5] + "***"})
    try:
        # Check if user exists
        existing = (
            _supabase()
            .table("users")
            .select("id")
            .eq("contact_number", contact_number)
            .execute()
        )
        
        if not existing.data:
            # Create new user
            logger.info("Creating new user", extra={"contact": contact_number[:5] + "***", "has_name": name is not None})
            _supabase().table("users").insert({
                "contact_number": contact_number,
                "name": name,
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }).execute()
            logger.info("User created", extra={"contact": contact_number[:5] + "***"})
        elif name:
            # Update name if provided and user exists
            logger.debug("Updating existing user name", extra={"contact": contact_number[:5] + "***"})
            _supabase().table("users").update({
                "name": name,
                "updated_at": datetime.utcnow().isoformat(),
            }).eq("contact_number", contact_number).execute()
    except Exception as exc:
        logger.error("Failed to ensure user exists", exc_info=exc, extra={"contact": contact_number[:5] + "***"})
        # Don't raise - user creation is not critical for booking flow


@function_tool
async def get_user_data(
    ctx: RunContext[SessionData],
    contact_number: str | None = None,
) -> str:
    """Get user data from session, participant attributes/metadata, or database.

    Args:
        contact_number: Optional phone number to lookup if context is missing.
    """
    logger.info(
        "Tool called: get_user_data",
        extra={"has_contact": contact_number is not None, "has_session_contact": ctx.userdata.state.contact_number is not None},
    )
    try:
        # 1) Session state
        if ctx.userdata.state.contact_number or ctx.userdata.state.name:
            logger.debug("User data found in session state")
            return (
                f"User data found in session. Phone: {ctx.userdata.state.contact_number}, "
                f"Name: {ctx.userdata.state.name or 'unknown'}."
            )

        # 2) Participant attributes/metadata
        participant = _resolve_participant(ctx)
        phone, name = _extract_user_from_participant(participant)
        if phone:
            normalized = _normalize_phone_number(phone)
            ctx.userdata.state.contact_number = normalized
            if name:
                ctx.userdata.state.name = name.strip()
            _ensure_user_exists(normalized, ctx.userdata.state.name)
            await _notify_tool_event(
                ctx,
                tool="get_user_data",
                status="found",
                data={"contact_number": normalized, "name": ctx.userdata.state.name},
            )
            return (
                f"User data found from participant context. Phone: {normalized}, "
                f"Name: {ctx.userdata.state.name or 'unknown'}."
            )

        # 3) Provided contact_number lookup
        if contact_number:
            normalized = _normalize_phone_number(contact_number)
            record = _fetch_user_by_phone(normalized)
            if record:
                ctx.userdata.state.contact_number = normalized
                ctx.userdata.state.name = record.get("name")
                await _notify_tool_event(
                    ctx,
                    tool="get_user_data",
                    status="found",
                    data={"contact_number": normalized, "name": ctx.userdata.state.name},
                )
                return (
                    f"User data found in database. Phone: {normalized}, "
                    f"Name: {ctx.userdata.state.name or 'unknown'}."
                )

        await _notify_tool_event(
            ctx,
            tool="get_user_data",
            status="missing",
            data={},
        )
        raise ToolError("Ask the user for their phone number to continue.")
    except Exception as exc:
        logger.error("Tool failed: get_user_data", exc_info=exc)
        raise


@function_tool
async def identify_user(
    ctx: RunContext[SessionData],
    contact_number: str,
    name: str | None = None,
) -> str:
    """Identify the user by phone number before booking or retrieving appointments.

    Args:
        contact_number: The user's phone number with country code.
        name: The user's name, if provided.
    """
    logger.info(
        "Tool called: identify_user",
        extra={"contact_number": contact_number[:5] + "***", "has_name": name is not None}
    )
    try:
        normalized = _normalize_phone_number(contact_number)
        ctx.userdata.state.contact_number = normalized
        if name:
            ctx.userdata.state.name = name.strip()

        # Ensure user exists in database
        _ensure_user_exists(normalized, ctx.userdata.state.name)

        logger.info(
            "User identified",
            extra={
                "normalized_contact": normalized[:5] + "***",
                "user_name": ctx.userdata.state.name,
            }
        )

        await _notify_tool_event(
            ctx,
            tool="identify_user",
            status="identified",
            data={"contact_number": normalized, "name": ctx.userdata.state.name},
        )

        result = (
            f"Thanks {ctx.userdata.state.name}. I have your phone number as {normalized}."
            if ctx.userdata.state.name
            else f"Thanks. I have your phone number as {normalized}."
        )
        logger.debug("Tool completed: identify_user")
        return result
    except Exception as exc:
        logger.error("Tool failed: identify_user", exc_info=exc)
        raise


@function_tool
async def fetch_slots(
    ctx: RunContext[SessionData],
    preferred_date: str | None = None,
) -> list[dict[str, str]]:
    """List available appointment slots.

    Args:
        preferred_date: Optional date to filter slots, in YYYY-MM-DD format.
    """
    logger.info("Tool called: fetch_slots", extra={"preferred_date": preferred_date})
    try:
        if preferred_date:
            preferred_date = _normalize_date(preferred_date)
            dates = [preferred_date]
            logger.debug("Using preferred date filter", extra={"date": preferred_date})
        else:
            dates = _default_slot_dates()
            logger.debug("Using default date range", extra={"count": len(dates)})

        all_slots = _generate_slots(dates)
        booked = _fetch_booked_slots(dates)
        available = [
            slot
            for slot in all_slots
            if (slot["date"], slot["time"]) not in booked
        ]

        logger.info(
            "Slots fetched",
            extra={
                "total_slots": len(all_slots),
                "booked": len(booked),
                "available": len(available),
                "dates": dates,
            }
        )

        await _notify_tool_event(
            ctx,
            tool="fetch_slots",
            status="listed",
            data={"dates": dates, "available_slots": available},
        )

        logger.debug("Tool completed: fetch_slots", extra={"available_count": len(available)})
        return available
    except Exception as exc:
        logger.error("Tool failed: fetch_slots", exc_info=exc)
        raise


@function_tool
async def book_appointment(
    ctx: RunContext[SessionData],
    date_value: str,
    time_value: str,
    contact_number: str | None = None,
    name: str | None = None,
    notes: str | None = None,
) -> str:
    """Book an appointment for the user.

    Args:
        date_value: Appointment date in YYYY-MM-DD format.
        time_value: Appointment time in HH:MM (24h) or HH:MM AM/PM format.
        contact_number: User phone number (required if not already identified).
        name: User name, if provided.
        notes: Optional notes or preferences mentioned by the user.
    """
    logger.info(
        "Tool called: book_appointment",
        extra={
            "date": date_value,
            "time": time_value,
            "has_contact": contact_number is not None,
            "has_name": name is not None,
            "has_notes": notes is not None,
        }
    )
    try:
        normalized_contact = _ensure_contact_number(ctx, contact_number)
        slot_date = _normalize_date(date_value)
        slot_time = _normalize_time(time_value)

        logger.debug(
            "Normalized booking parameters",
            extra={
                "normalized_contact": normalized_contact[:5] + "***",
                "slot_date": slot_date,
                "slot_time": slot_time,
            }
        )

        if name:
            ctx.userdata.state.name = name.strip()

        # Ensure user exists in database
        _ensure_user_exists(normalized_contact, ctx.userdata.state.name)

        logger.debug("Checking for slot conflicts", extra={"slot_date": slot_date, "slot_time": slot_time})
        conflict = (
            _supabase()
            .table("appointments")
            .select("id, status")
            .eq("slot_date", slot_date)
            .eq("slot_time", slot_time)
            .neq("status", "cancelled")
            .execute()
        )
        if conflict.data:
            logger.warning(
                "Booking conflict detected",
                extra={"slot_date": slot_date, "slot_time": slot_time, "conflict_id": conflict.data[0].get("id")}
            )
            await _notify_tool_event(
                ctx,
                tool="book_appointment",
                status="conflict",
                data={"slot_date": slot_date, "slot_time": slot_time},
            )
            return (
                f"That slot on {_format_slot(slot_date, slot_time)} is already booked. "
                "Please choose another time."
            )

        record = {
            "contact_number": normalized_contact,
            "name": ctx.userdata.state.name,
            "slot_date": slot_date,
            "slot_time": slot_time,
            "status": "booked",
            "notes": notes,
            "created_at": datetime.utcnow().isoformat(),
        }
        logger.info("Inserting appointment into database", extra={"slot_date": slot_date, "slot_time": slot_time})
        try:
            result = _supabase().table("appointments").insert(record).execute()
            logger.info(
                "Appointment booked successfully",
                extra={
                    "slot_date": slot_date,
                    "slot_time": slot_time,
                    "contact": normalized_contact[:5] + "***",
                    "appointment_id": result.data[0].get("id") if result.data else None,
                }
            )
        except Exception as db_exc:
            logger.error("Database insert failed for appointment", exc_info=db_exc, extra={"record": record})
            raise

        ctx.userdata.state.booked_slots.append({"date": slot_date, "time": slot_time})
        if notes:
            ctx.userdata.state.preferences.append(notes)

        await _notify_tool_event(
            ctx,
            tool="book_appointment",
            status="booked",
            data=record,
        )

        name_display = ctx.userdata.state.name or "there"
        result_msg = (
            f"All set, {name_display}. Your appointment is booked for "
            f"{_format_slot(slot_date, slot_time)}."
        )
        logger.debug("Tool completed: book_appointment")
        return result_msg
    except Exception as exc:
        logger.error("Tool failed: book_appointment", exc_info=exc)
        raise


@function_tool
async def retrieve_appointments(
    ctx: RunContext[SessionData],
    contact_number: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve a user's appointment history.

    Args:
        contact_number: User phone number (required if not already identified).
    """
    logger.info("Tool called: retrieve_appointments", extra={"has_contact": contact_number is not None})
    try:
        normalized_contact = _ensure_contact_number(ctx, contact_number)
        logger.debug("Fetching appointments from database", extra={"contact": normalized_contact[:5] + "***"})
        try:
            result = (
                _supabase()
                .table("appointments")
                .select("slot_date, slot_time, status, notes, name")
                .eq("contact_number", normalized_contact)
                .order("slot_date", desc=False)
                .order("slot_time", desc=False)
                .execute()
            )
            appointments = result.data or []
            logger.info(
                "Appointments retrieved",
                extra={
                    "contact": normalized_contact[:5] + "***",
                    "count": len(appointments),
                }
            )
        except Exception as db_exc:
            logger.error("Database query failed for appointments", exc_info=db_exc, extra={"contact": normalized_contact[:5] + "***"})
            raise

        await _notify_tool_event(
            ctx,
            tool="retrieve_appointments",
            status="retrieved",
            data={"count": len(appointments), "appointments": appointments},
        )

        logger.debug("Tool completed: retrieve_appointments", extra={"count": len(appointments)})
        return appointments
    except Exception as exc:
        logger.error("Tool failed: retrieve_appointments", exc_info=exc)
        raise


@function_tool
async def cancel_appointment(
    ctx: RunContext[SessionData],
    date_value: str,
    time_value: str,
    contact_number: str | None = None,
    reason: str | None = None,
) -> str:
    """Cancel an existing appointment.

    Args:
        date_value: Appointment date in YYYY-MM-DD format.
        time_value: Appointment time in HH:MM or HH:MM AM/PM format.
        contact_number: User phone number (required if not already identified).
        reason: Optional cancellation reason.
    """
    logger.info(
        "Tool called: cancel_appointment",
        extra={"date": date_value, "time": time_value, "has_reason": reason is not None}
    )
    try:
        normalized_contact = _ensure_contact_number(ctx, contact_number)
        slot_date = _normalize_date(date_value)
        slot_time = _normalize_time(time_value)

        logger.debug(
            "Looking up appointment to cancel",
            extra={"slot_date": slot_date, "slot_time": slot_time, "contact": normalized_contact[:5] + "***"}
        )
        lookup = (
            _supabase()
            .table("appointments")
            .select("id, status")
            .eq("contact_number", normalized_contact)
            .eq("slot_date", slot_date)
            .eq("slot_time", slot_time)
            .execute()
        )
        if not lookup.data:
            logger.warning(
                "Appointment not found for cancellation",
                extra={"slot_date": slot_date, "slot_time": slot_time, "contact": normalized_contact[:5] + "***"}
            )
            await _notify_tool_event(
                ctx,
                tool="cancel_appointment",
                status="not_found",
                data={"slot_date": slot_date, "slot_time": slot_time},
            )
            return "I could not find that appointment to cancel."

        appointment_id = lookup.data[0].get("id")
        update_payload = {"status": "cancelled", "notes": reason}
        logger.info("Cancelling appointment", extra={"appointment_id": appointment_id, "slot_date": slot_date, "slot_time": slot_time})
        try:
            _supabase().table("appointments").update(update_payload).eq(
                "contact_number", normalized_contact
            ).eq("slot_date", slot_date).eq("slot_time", slot_time).execute()
            logger.info("Appointment cancelled successfully", extra={"appointment_id": appointment_id})
        except Exception as db_exc:
            logger.error("Database update failed for cancellation", exc_info=db_exc, extra={"appointment_id": appointment_id})
            raise

        await _notify_tool_event(
            ctx,
            tool="cancel_appointment",
            status="cancelled",
            data={"slot_date": slot_date, "slot_time": slot_time, "reason": reason},
        )

        result_msg = f"Your appointment on {_format_slot(slot_date, slot_time)} is cancelled."
        logger.debug("Tool completed: cancel_appointment")
        return result_msg
    except Exception as exc:
        logger.error("Tool failed: cancel_appointment", exc_info=exc)
        raise


@function_tool
async def modify_appointment(
    ctx: RunContext[SessionData],
    original_date: str,
    original_time: str,
    new_date: str,
    new_time: str,
    contact_number: str | None = None,
) -> str:
    """Modify an existing appointment to a new date and time.

    Args:
        original_date: Original appointment date in YYYY-MM-DD format.
        original_time: Original appointment time in HH:MM or HH:MM AM/PM format.
        new_date: New appointment date in YYYY-MM-DD format.
        new_time: New appointment time in HH:MM or HH:MM AM/PM format.
        contact_number: User phone number (required if not already identified).
    """
    logger.info(
        "Tool called: modify_appointment",
        extra={
            "original_date": original_date,
            "original_time": original_time,
            "new_date": new_date,
            "new_time": new_time,
        }
    )
    try:
        normalized_contact = _ensure_contact_number(ctx, contact_number)
        old_date = _normalize_date(original_date)
        old_time = _normalize_time(original_time)
        new_date = _normalize_date(new_date)
        new_time = _normalize_time(new_time)

        if new_date == old_date and new_time == old_time:
            logger.debug("Modification requested but no change needed", extra={"slot_date": old_date, "slot_time": old_time})
            return "That appointment is already scheduled for the requested time."

        logger.debug(
            "Looking up appointment to modify",
            extra={"old_slot": f"{old_date} {old_time}", "contact": normalized_contact[:5] + "***"}
        )
        existing = (
            _supabase()
            .table("appointments")
            .select("id, status")
            .eq("contact_number", normalized_contact)
            .eq("slot_date", old_date)
            .eq("slot_time", old_time)
            .execute()
        )
        if not existing.data:
            logger.warning(
                "Appointment not found for modification",
                extra={"slot_date": old_date, "slot_time": old_time, "contact": normalized_contact[:5] + "***"}
            )
            await _notify_tool_event(
                ctx,
                tool="modify_appointment",
                status="not_found",
                data={"slot_date": old_date, "slot_time": old_time},
            )
            return "I could not find that appointment to modify."

        existing_id = existing.data[0]["id"]
        logger.debug("Checking for conflicts with new slot", extra={"new_slot": f"{new_date} {new_time}", "existing_id": existing_id})
        conflict = (
            _supabase()
            .table("appointments")
            .select("id, status")
            .eq("slot_date", new_date)
            .eq("slot_time", new_time)
            .neq("status", "cancelled")
            .neq("id", existing_id)
            .execute()
        )
        if conflict.data:
            logger.warning(
                "Conflict detected with new slot",
                extra={"new_slot": f"{new_date} {new_time}", "conflict_id": conflict.data[0].get("id")}
            )
            await _notify_tool_event(
                ctx,
                tool="modify_appointment",
                status="conflict",
                data={"slot_date": new_date, "slot_time": new_time},
            )
            return (
                f"That new slot on {_format_slot(new_date, new_time)} is already booked. "
                "Please choose a different time."
            )

        logger.info(
            "Updating appointment",
            extra={
                "appointment_id": existing_id,
                "old_slot": f"{old_date} {old_time}",
                "new_slot": f"{new_date} {new_time}",
            }
        )
        try:
            _supabase().table("appointments").update(
                {"slot_date": new_date, "slot_time": new_time, "status": "booked"}
            ).eq("contact_number", normalized_contact).eq(
                "slot_date", old_date
            ).eq("slot_time", old_time).execute()
            logger.info("Appointment modified successfully", extra={"appointment_id": existing_id})
        except Exception as db_exc:
            logger.error("Database update failed for modification", exc_info=db_exc, extra={"appointment_id": existing_id})
            raise

        await _notify_tool_event(
            ctx,
            tool="modify_appointment",
            status="modified",
            data={
                "old_date": old_date,
                "old_time": old_time,
                "new_date": new_date,
                "new_time": new_time,
            },
        )

        result_msg = f"Your appointment has been moved to {_format_slot(new_date, new_time)}."
        logger.debug("Tool completed: modify_appointment")
        return result_msg
    except Exception as exc:
        logger.error("Tool failed: modify_appointment", exc_info=exc)
        raise


@function_tool
async def end_conversation(
    ctx: RunContext[SessionData],
    summary: str,
    preferences: list[str],
    booked_slots: list[str],
    contact_number: str | None = None,
) -> str:
    """Finalize the conversation with a summary and close the session. To be called when user wants to hang up/drop the call.

    Args:
        summary: Summary of the conversation.
        preferences: User preferences mentioned.
        booked_slots: List of booked slots in YYYY-MM-DD at HH:MM format.
        contact_number: User phone number (optional, uses session state if available).
    """
    logger.info(
        "Tool called: end_conversation",
        extra={
            "summary_length": len(summary),
            "preferences_count": len(preferences),
            "booked_slots_count": len(booked_slots),
            "has_contact": contact_number is not None,
        }
    )
    try:
        # Try to get contact number, but allow ending without it
        normalized_contact = None
        try:
            normalized_contact = _ensure_contact_number(ctx, contact_number)
        except ToolError:
            logger.warning("Ending conversation without contact number")
            normalized_contact = "unknown"
        if not booked_slots and ctx.userdata.state.booked_slots:
            booked_slots = [
                _format_slot(slot["date"], slot["time"])
                for slot in ctx.userdata.state.booked_slots
            ]
            logger.debug("Using booked slots from conversation state", extra={"count": len(booked_slots)})
        if not preferences and ctx.userdata.state.preferences:
            preferences = ctx.userdata.state.preferences
            logger.debug("Using preferences from conversation state", extra={"count": len(preferences)})

        logger.info(
            "Preparing call summary",
            extra={
                "contact": normalized_contact[:5] + "***",
                "booked_slots_count": len(booked_slots),
                "preferences_count": len(preferences),
            }
        )

        payload = {
            "type": "call_summary",
            "summary": summary,
            "preferences": preferences,
            "booked_slots": booked_slots,
            "contact_number": normalized_contact,
            "created_at": datetime.utcnow().isoformat(),
        }
        await _send_rpc(ctx, payload)
        logger.debug("Call summary RPC sent to client")
        
        await _notify_tool_event(
            ctx,
            tool="end_conversation",
            status="summary_sent",
            data={"contact_number": normalized_contact},
        )

        summary_record = {
            "contact_number": normalized_contact,
            "summary": summary,
            "preferences": preferences,
            "booked_slots": booked_slots,
            "created_at": payload["created_at"],
        }
        logger.info("Saving conversation summary to database", extra={"contact": normalized_contact[:5] + "***"})
        try:
            result = _supabase().table("conversation_summaries").insert(summary_record).execute()
            logger.info(
                "Conversation summary saved",
                extra={
                    "contact": normalized_contact[:5] + "***",
                    "summary_id": result.data[0].get("id") if result.data else None,
                }
            )
        except Exception as db_exc:
            logger.error("Database insert failed for conversation summary", exc_info=db_exc, extra={"contact": normalized_contact[:5] + "***"})
            raise

        async def _shutdown_later() -> None:
            logger.info("Shutting down session in 5 seconds")
            await asyncio.sleep(5)
            ctx.session.shutdown(drain=True)

        asyncio.create_task(_shutdown_later())

        logger.info("Tool completed: end_conversation, session shutdown scheduled")
        return "Thanks for your time. I have saved a summary and will end the call now."
    except Exception as exc:
        logger.error("Tool failed: end_conversation", exc_info=exc)
        raise


def get_tools() -> list[Any]:
    return [
        get_user_data,
        identify_user,
        fetch_slots,
        book_appointment,
        retrieve_appointments,
        cancel_appointment,
        modify_appointment,
        end_conversation,
    ]
