import asyncio
import base64
import json
import uuid
from collections import deque
from datetime import date

from aws_sdk_bedrock_runtime.client import (
    BedrockRuntimeClient,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from aws_sdk_bedrock_runtime.models import (
    InvokeModelWithBidirectionalStreamInputChunk,
    BidirectionalInputPayloadPart,
)
from aws_sdk_bedrock_runtime.config import Config
from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver

DEFAULT_SYSTEM_PROMPT_TEMPLATE = (
    "You are the virtual receptionist for Greenfield Dental Clinic. "
    "Today's date is {today} ({day_of_week}). "
    "You help callers book a dentist appointment over the phone."
    "IDENTITY:"
    "- Ask whether this is their first time visiting the clinic."
    "- Before calling findPatientTool or registerPatientTool, full name and date of birth must "
    "  be collected and confirmed the same strict way every time, for both returning and "
    "  first-time patients - never assemble, guess, or fill in either yourself:"
    "  - Ask for their first and last name, one question. Call collectPatientNameTool with "
    "    whatever you heard - it reports which part, if any, is still missing. Keep asking and "
    "    calling it again until it reports 'complete', then read the full name back to the "
    "    caller and get an explicit yes before calling confirmPatientNameTool."
    "  - Ask for their date of birth. Never guess, assume, or fill in a birth year the caller "
    "    did not actually say - if you did not clearly hear a 4-digit year, ask them to repeat "
    "    it before calling the tool. Call collectDobTool with year, month, and day. If it "
    "    reports 'invalid' or 'implausible', ask them to clearly repeat their date of birth "
    "    including the year - never retry with a guessed value. Once it reports 'complete', "
    "    read the date back to the caller and get an explicit yes before calling confirmDobTool."
    "  - findPatientTool and registerPatientTool will both refuse to run until both of the "
    "    above have been confirmed for this call."
    "- Returning patient: once name and date of birth are confirmed, call findPatientTool."
    "  - If found is true and usualDoctorId is present, call getDoctorTool with that doctorId "
    "    and mention 'I see you've been treated by Dr [Name] before.'"
    "  - If found is false even though they said they were returning, treat them as a "
    "    first-time patient instead (use registerPatientTool once phone is also confirmed) - "
    "    do not dead-end the call."
    "- First-time patient: before asking anything else - including their name - ask if they "
    "  have a preferred dentist, or if anyone is fine; there's nothing to look up yet. Only "
    "  after that, collect and confirm their name and date of birth (using the process above). "
    "  Do not ask for their phone number at this point - that happens later, in COLLECTING "
    "  DETAILS, only after CHIEF COMPLAINT and FINDING A SLOT below. Do not go straight from "
    "  confirming date of birth into asking for a phone number."
    "CHIEF COMPLAINT:"
    "- Ask what the problem is, and whether there's any swelling or severe pain."
    "- Do not call a tool for this - carry it forward as the reason field on bookAppointmentTool."
    "- Based on what they describe, pick a sensible appointmentType for booking later "
    "  (e.g. 'CheckUp', 'Emergency', 'Review', 'NewPatient', 'FollowUp') - a toothache that's "
    "  getting worse over multiple days with pain leans toward 'Emergency', a routine cleaning "
    "  is 'CheckUp'."
    "FINDING A SLOT:"
    "- Call findNextOpeningTool right away, with no spoken preamble first - do not say anything "
    "  like 'one moment', 'let me check', or 'I'll search for that' before calling it. Call the "
    "  tool immediately, then speak once you have its result."
    "- Call findNextOpeningTool with dateFrom=today, dateTo=end of this week, and doctorId set "
    "  to their usual/preferred doctor (or omitted entirely for 'anyone is fine')."
    "- If found is true, tell them the date, time, and doctor's name."
    "- If found is false, offer two choices: wait until next week with the same doctor (call "
    "  findNextOpeningTool again with dateFrom/dateTo shifted to next week, same doctorId), or "
    "  see someone sooner (call findNextOpeningTool again with doctorId omitted, same this-week "
    "  window)."
    "- Once the caller accepts a slot, do not call findNextOpeningTool again for it - move on to "
    "  collecting their details."
    "COLLECTING DETAILS:"
    "- Do this after CHIEF COMPLAINT and FINDING A SLOT above, never right after confirming "
    "  name/date of birth - the caller should already know their appointment day and time "
    "  before you ask for their phone number."
    "- Phone number, step 1 - offer the caller ID first, if one is available for this call: "
    "  ask the caller if they'd like to use the number they're calling from as their contact "
    "  number, and read that number back to them grouped as part of the same question (e.g. "
    "  'You're calling from seven-eight-six, two-two-eight, seven-two-zero-four - would you "
    "  like to use this number?'). This is one combined question - do not call any tool yet."
    "  - If they say yes, call useCallerIdAsPhoneTool right away. Do not run manual digit "
    "    collection at all in this case - skip straight to step 3 below."
    "  - If they say no, or no caller ID is available for this call, move on to step 2."
    "- Phone number, step 2 - manual digit collection is a strict, tool-tracked process - never "
    "  assemble, remember, or guess the digits yourself, even across multiple things the caller "
    "  says. Call collectPhoneDigitsTool immediately after each caller turn that contains any "
    "  digits - do not wait, and do not combine what they just said with anything from earlier "
    "  in the call into one call. One caller turn with digits in it = one "
    "  collectPhoneDigitsTool call, made right away, every time - even if it's only 2 or 3 "
    "  digits. Keep asking for more and calling it again until it reports status 'complete'. If "
    "  it reports 'too_many_digits', apologize and ask them to say the full 10-digit number "
    "  again from the start - the buffer is already cleared for you."
    "  - This applies at ANY point during collection, not just after a full readback: if the "
    "    caller ever says the digits so far are wrong, or corrects themselves ('that's wrong, "
    "    it's actually...', 'wait, I made a mistake', 'let me start over'), call "
    "    collectPhoneDigitsTool with reset=true right then - do not treat their correction as "
    "    more digits to append onto what you already have. If they gave new digits in that "
    "    same turn, include them in the same reset=true call."
    "  - Once collectPhoneDigitsTool reports 'complete', read the grouped number back to the "
    "    caller exactly as given (e.g. 'I have seven-eight-six, two-two-eight, "
    "    seven-two-zero-four - is that right?') and wait for an explicit yes. If they say it's "
    "    wrong, call collectPhoneDigitsTool with reset=true and start over. Only after they "
    "    confirm, call confirmPhoneNumberTool."
    "  - If collectPhoneDigitsTool ever reports status 'escalate', stop asking the caller to "
    "    repeat their number - manual capture has failed too many times. Follow the message's "
    "    instruction exactly: it will offer the caller ID number again as a fallback (call "
    "    useCallerIdAsPhoneTool if they agree this time) or, if that's not available or they "
    "    still decline, apologize and end the call gracefully rather than asking again."
    "  - If the caller indicates they're calling from outside the US, gives a country code, or "
    "    gives a number that clearly isn't 10 US digits, switch to "
    "    collectInternationalPhoneDigitsTool instead - do not keep insisting on exactly 10 "
    "    digits for a number that isn't a US number. It works the same way (one fragment per "
    "    call, immediately), except there's no fixed digit count - only call it with done=true "
    "    once the caller has confirmed that's their complete number, including country code. If "
    "    it reports 'invalid', ask them to repeat their full number including country code."
    "- Phone number, step 3 - registerPatientTool and updatePatientPhoneTool will refuse to run "
    "  until either confirmPhoneNumberTool or useCallerIdAsPhoneTool has succeeded for this "
    "  call - always finish step 1 or step 2 before either of those, every time."
    "- Returning patient: ask for a current phone number (using the process above), then call "
    "  updatePatientPhoneTool with their patientId, in case it's changed since their last visit."
    "- First-time patient: confirm name and date of birth (using the IDENTITY process above, if "
    "  not already done), collect and confirm the phone number (using the process above), then "
    "  call registerPatientTool. If it comes back registered:false because they already exist, "
    "  call findPatientTool with the same confirmed name/DOB instead of retrying registration."
    "BOOKING:"
    "- Call bookAppointmentTool right away, with no spoken preamble first - same rule as "
    "  findNextOpeningTool above. Call it immediately, then speak once you have its result."
    "- Call bookAppointmentTool with the patientId, doctorId, date, startTime, durationMinutes, "
    "  appointmentType, and reason gathered above."
    "- If booked is true, confirm the date, time, and doctor's name back to the caller, and let "
    "  them know to bring their ID and any relevant medical or dental information."
    "- If booked is false, explain the reason in plain language and go back to findNextOpeningTool "
    "  for a different slot - there is no way to force a booking through."
    "ENDING THE CALL:"
    "- When the caller has nothing further to add, call endCallTool right away - you do not need "
    "  to say goodbye first."
    "- The tool's result will tell you what to do next. Follow that instruction exactly."
    "TURN LENGTH:"
    "- Keep every turn to 1-2 sentences when it ends in a question you need the caller to "
    "  answer - the question must be the last thing you say, with nothing appended after it. "
    "  Do not tack on a status recap, a reminder of prior details, or anything else once you've "
    "  asked something."
    "- Ask exactly one question per turn, always. Once you've asked something, stop talking and "
    "  wait for the caller's answer before asking anything else - never move straight into the "
    "  next question (e.g. date of birth) in the same turn as a confirmation question (e.g. "
    "  'is that correct?') for something else. Even if you already know what you'll need to ask "
    "  next, do not ask it until the caller has responded to the current question."
    "- Never restate the appointment date, time, or doctor while you are still asking for "
    "  something else (e.g. re-asking for a phone number or a name). If you need to reference "
    "  the booking in progress, say 'your appointment' or 'this booking', not the full "
    "  date/time/doctor again."
    "- State the full appointment date, time, and doctor's name out loud exactly once - at the "
    "  moment you confirm bookAppointmentTool returned booked:true. Do not repeat it in the "
    "  goodbye or anywhere else after that."
    "- Confirmation turns (reading back a name, date of birth, or phone number) follow the same "
    "  1-2 sentence limit - state the value and ask if it's correct as a single combined "
    "  question, e.g. 'I have your number as 786-228-7204 - is that right?' Do not add a "
    "  separate follow-up sentence like 'please say yes to confirm' - the question alone is "
    "  enough."
    "- When asking the caller to retry or clarify something, give exactly one instruction, not a "
    "  choice between two ways to respond - e.g. ask them to repeat the number, or ask them to "
    "  spell it out, not both in the same breath."
    "- This applies to every tool call, not just the ones called out elsewhere: never say you're "
    "  about to do something ('let me check', 'one moment', 'I'll look that up', 'I'll search "
    "  for that') and then pause - call the tool immediately in the same turn, then speak once "
    "  you have its result."
    "STYLE:"
    "- Be warm, professional, and concise, like a real dental receptionist."
    "- Confirm important details back to the caller before booking."
    "- Do not invent patients, doctors, or appointment slots that a tool didn't actually return."
    "- Tool field descriptions like 'YYYY-MM-DD' or 'HH:MM' are format hints for you to fill in "
    "  the tool call correctly - never speak them out loud. Ask for dates and times the way a "
    "  human receptionist would (e.g. 'What's your date of birth?', 'What time works for you?'), "
    "  accept the answer in whatever form the caller gives it, and silently convert it to the "
    "  required format yourself before calling the tool."
)


def _build_system_prompt() -> str:
    today = date.today()
    return DEFAULT_SYSTEM_PROMPT_TEMPLATE.format(
        today=today.isoformat(), day_of_week=today.strftime("%A")
    )


_START_SESSION_EVENT = """{
    "event": {
        "sessionStart": {
        "inferenceConfiguration": {
            "maxTokens": 1024,
            "topP": 0.9,
            "temperature": 0.7
            }
        }
    }
}"""

_CONTENT_START_EVENT = """{
    "event": {
        "contentStart": {
        "promptName": "%s",
        "contentName": "%s",
        "type": "AUDIO",
        "interactive": true,
        "role": "USER",
        "audioInputConfiguration": {
            "mediaType": "audio/lpcm",
            "sampleRateHertz": 16000,
            "sampleSizeBits": 16,
            "channelCount": 1,
            "audioType": "SPEECH",
            "encoding": "base64"
            }
        }
    }
}"""

_AUDIO_EVENT_TEMPLATE = """{
    "event": {
        "audioInput": {
        "promptName": "%s",
        "contentName": "%s",
        "content": "%s"
        }
    }
}"""

_TEXT_CONTENT_START_EVENT = """{
    "event": {
        "contentStart": {
        "promptName": "%s",
        "contentName": "%s",
        "type": "TEXT",
        "role": "%s",
        "interactive": false,
            "textInputConfiguration": {
                "mediaType": "text/plain"
            }
        }
    }
}"""

_TEXT_INPUT_EVENT = """{
    "event": {
        "textInput": {
        "promptName": "%s",
        "contentName": "%s",
        "content": "%s"
        }
    }
}"""

_TOOL_CONTENT_START_EVENT = """{
    "event": {
        "contentStart": {
            "promptName": "%s",
            "contentName": "%s",
            "interactive": false,
            "type": "TOOL",
            "role": "TOOL",
            "toolResultInputConfiguration": {
                "toolUseId": "%s",
                "type": "TEXT",
                "textInputConfiguration": {
                    "mediaType": "text/plain"
                }
            }
        }
    }
}"""

_CONTENT_END_EVENT = """{
    "event": {
        "contentEnd": {
        "promptName": "%s",
        "contentName": "%s"
        }
    }
}"""

_PROMPT_END_EVENT = """{
    "event": {
        "promptEnd": {
        "promptName": "%s"
        }
    }
}"""

_SESSION_END_EVENT = """{
    "event": {
        "sessionEnd": {}
    }
}"""


def _start_prompt_event(prompt_name: str) -> str:
    find_patient_schema = json.dumps({
        "type": "object",
        "properties": {
            "firstName": {"type": "string", "description": "Patient's first name."},
            "lastName": {"type": "string", "description": "Patient's last name."},
            "dob": {"type": "string", "description": "Patient's date of birth, YYYY-MM-DD."},
        },
        "required": ["firstName", "lastName", "dob"],
    })

    register_patient_schema = json.dumps({
        "type": "object",
        "properties": {
            "firstName": {"type": "string", "description": "Patient's first name."},
            "lastName": {"type": "string", "description": "Patient's last name."},
            "dob": {"type": "string", "description": "Patient's date of birth, YYYY-MM-DD."},
            "phone": {"type": "string", "description": "Patient's phone number."},
        },
        "required": ["firstName", "lastName", "dob", "phone"],
    })

    update_patient_phone_schema = json.dumps({
        "type": "object",
        "properties": {
            "patientId": {"type": "string", "description": "Patient ID from findPatientTool."},
            "phone": {"type": "string", "description": "Patient's current phone number."},
        },
        "required": ["patientId", "phone"],
    })

    get_doctor_schema = json.dumps({
        "type": "object",
        "properties": {
            "doctorId": {"type": "string", "description": "Doctor ID, e.g. 'D-100'."},
        },
        "required": ["doctorId"],
    })

    find_next_opening_schema = json.dumps({
        "type": "object",
        "properties": {
            "dateFrom": {"type": "string", "description": "Start of the search window, YYYY-MM-DD."},
            "dateTo": {"type": "string", "description": "End of the search window, YYYY-MM-DD."},
            "durationMinutes": {"type": "integer", "description": "Appointment duration in minutes.", "default": 30},
            "doctorId": {
                "type": "string",
                "description": "Search only this doctor. Omit to search every doctor and return the single earliest slot.",
            },
        },
        "required": ["dateFrom", "dateTo"],
    })

    book_appointment_schema = json.dumps({
        "type": "object",
        "properties": {
            "patientId": {"type": "string", "description": "Patient ID from findPatientTool or registerPatientTool."},
            "doctorId": {"type": "string", "description": "Doctor ID from findNextOpeningTool."},
            "date": {"type": "string", "description": "Appointment date, YYYY-MM-DD, from findNextOpeningTool."},
            "startTime": {"type": "string", "description": "Appointment start time, HH:MM, from findNextOpeningTool."},
            "durationMinutes": {"type": "integer", "description": "Appointment duration in minutes.", "default": 30},
            "appointmentType": {
                "type": "string",
                "description": "Category of visit, e.g. 'CheckUp', 'Emergency', 'Review', 'NewPatient', 'FollowUp'.",
            },
            "reason": {"type": "string", "description": "The patient's own description of why they're coming in."},
        },
        "required": ["patientId", "doctorId", "date", "startTime", "appointmentType", "reason"],
    })

    collect_phone_digits_schema = json.dumps({
        "type": "object",
        "properties": {
            "digits": {
                "type": "string",
                "description": (
                    "Only the digits you just heard from the caller in this turn, digits only "
                    "(e.g. '78'), no spaces, dashes, or words. Call this every time the caller "
                    "gives you part or all of their phone number, even a single fragment."
                ),
            },
            "reset": {
                "type": "boolean",
                "description": "Set true to discard everything collected so far and start over.",
                "default": False,
            },
        },
        "required": ["digits"],
    })

    collect_international_phone_digits_schema = json.dumps({
        "type": "object",
        "properties": {
            "digits": {
                "type": "string",
                "description": (
                    "Only the digits you just heard from the caller in this turn, digits only, "
                    "no spaces, dashes, or words - including the country code if they give it. "
                    "Call this every time the caller gives you part of their number."
                ),
            },
            "done": {
                "type": "boolean",
                "description": (
                    "Set true only once the caller has confirmed this is their complete "
                    "number - there's no fixed digit count for international numbers, so this "
                    "is what signals completion instead."
                ),
                "default": False,
            },
            "reset": {
                "type": "boolean",
                "description": "Set true to discard everything collected so far and start over.",
                "default": False,
            },
        },
        "required": ["digits"],
    })

    confirm_phone_number_schema = json.dumps({"type": "object", "properties": {}})

    use_caller_id_as_phone_schema = json.dumps({"type": "object", "properties": {}})

    collect_patient_name_schema = json.dumps({
        "type": "object",
        "properties": {
            "firstName": {
                "type": "string",
                "description": "Caller's first name, if they just gave it. Omit if not yet heard - never guess it.",
            },
            "lastName": {
                "type": "string",
                "description": "Caller's last name, if they just gave it. Omit if not yet heard - never guess it.",
            },
        },
    })

    confirm_patient_name_schema = json.dumps({"type": "object", "properties": {}})

    collect_dob_schema = json.dumps({
        "type": "object",
        "properties": {
            "year": {"type": "integer", "description": "4-digit birth year. Never guess this - only fill it in if the caller actually said it."},
            "month": {"type": "integer", "description": "Birth month, 1-12."},
            "day": {"type": "integer", "description": "Birth day of month, 1-31."},
        },
        "required": ["year", "month", "day"],
    })

    confirm_dob_schema = json.dumps({"type": "object", "properties": {}})

    end_call_tool_schema = json.dumps({"type": "object", "properties": {}})

    return json.dumps({
        "event": {
            "promptStart": {
                "promptName": prompt_name,
                "textOutputConfiguration": {"mediaType": "text/plain"},
                "audioOutputConfiguration": {
                    "mediaType": "audio/lpcm",
                    "sampleRateHertz": 24000,
                    "sampleSizeBits": 16,
                    "channelCount": 1,
                    "voiceId": "tiffany",
                    "encoding": "base64",
                    "audioType": "SPEECH",
                },
                "toolUseOutputConfiguration": {"mediaType": "application/json"},
                "toolConfiguration": {
                    "tools": [
                        {"toolSpec": {
                            "name": "findPatientTool",
                            "description": "Look up a returning patient by first name, last name, and date of birth.",
                            "inputSchema": {"json": find_patient_schema},
                        }},
                        {"toolSpec": {
                            "name": "registerPatientTool",
                            "description": "Register a new, first-time patient.",
                            "inputSchema": {"json": register_patient_schema},
                        }},
                        {"toolSpec": {
                            "name": "updatePatientPhoneTool",
                            "description": "Update a returning patient's phone number on file.",
                            "inputSchema": {"json": update_patient_phone_schema},
                        }},
                        {"toolSpec": {
                            "name": "collectPhoneDigitsTool",
                            "description": (
                                "Call this every time the caller says any digits of their phone "
                                "number, even a single fragment - never assemble or remember the "
                                "number yourself. Returns how many digits have been collected so "
                                "far, or the complete number once all 10 digits are in, grouped "
                                "for you to read back."
                            ),
                            "inputSchema": {"json": collect_phone_digits_schema},
                        }},
                        {"toolSpec": {
                            "name": "collectInternationalPhoneDigitsTool",
                            "description": (
                                "Use this instead of collectPhoneDigitsTool when the caller "
                                "indicates they're calling from outside the US, gives a country "
                                "code, or gives a number that clearly isn't 10 US digits. No "
                                "fixed digit count - call it per fragment same as the domestic "
                                "tool, then once more with done=true once the caller confirms "
                                "that's their complete number."
                            ),
                            "inputSchema": {"json": collect_international_phone_digits_schema},
                        }},
                        {"toolSpec": {
                            "name": "confirmPhoneNumberTool",
                            "description": (
                                "Call this only after collectPhoneDigitsTool reports all 10 "
                                "digits collected, you have read the number back to the caller "
                                "grouped, and they confirmed it's correct. This locks in the "
                                "number - registerPatientTool/updatePatientPhoneTool refuse to "
                                "run until this has succeeded."
                            ),
                            "inputSchema": {"json": confirm_phone_number_schema},
                        }},
                        {"toolSpec": {
                            "name": "useCallerIdAsPhoneTool",
                            "description": (
                                "Call this only after reading the caller ID number back to the "
                                "caller and they said yes to using it - either the upfront "
                                "offer at the start of phone collection, or the fallback offer "
                                "after repeated manual capture failures. Uses the number "
                                "they're calling from as the confirmed contact number, "
                                "bypassing manual digit capture entirely."
                            ),
                            "inputSchema": {"json": use_caller_id_as_phone_schema},
                        }},
                        {"toolSpec": {
                            "name": "collectPatientNameTool",
                            "description": (
                                "Call this every time the caller gives their first and/or last "
                                "name - never assemble or remember it yourself. Reports which "
                                "part, if any, is still missing, or 'complete' once both are in."
                            ),
                            "inputSchema": {"json": collect_patient_name_schema},
                        }},
                        {"toolSpec": {
                            "name": "confirmPatientNameTool",
                            "description": (
                                "Call this only after collectPatientNameTool reports 'complete', "
                                "you have read the full name back to the caller, and they "
                                "confirmed it. This locks in the name - findPatientTool/"
                                "registerPatientTool refuse to run until this has succeeded."
                            ),
                            "inputSchema": {"json": confirm_patient_name_schema},
                        }},
                        {"toolSpec": {
                            "name": "collectDobTool",
                            "description": (
                                "Call this once you have the caller's full date of birth - year, "
                                "month, and day. Never guess or fill in a year they didn't "
                                "actually say. Reports 'invalid' or 'implausible' if the date "
                                "doesn't make sense, or 'complete' with the date to read back."
                            ),
                            "inputSchema": {"json": collect_dob_schema},
                        }},
                        {"toolSpec": {
                            "name": "confirmDobTool",
                            "description": (
                                "Call this only after collectDobTool reports 'complete', you "
                                "have read the date back to the caller, and they confirmed it. "
                                "This locks in the date of birth - findPatientTool/"
                                "registerPatientTool refuse to run until this has succeeded."
                            ),
                            "inputSchema": {"json": confirm_dob_schema},
                        }},
                        {"toolSpec": {
                            "name": "getDoctorTool",
                            "description": "Resolve a doctor ID to their name.",
                            "inputSchema": {"json": get_doctor_schema},
                        }},
                        {"toolSpec": {
                            "name": "findNextOpeningTool",
                            "description": "Find the earliest available appointment slot in a date range, for one doctor or across all of them.",
                            "inputSchema": {"json": find_next_opening_schema},
                        }},
                        {"toolSpec": {
                            "name": "bookAppointmentTool",
                            "description": "Book the appointment once patient, doctor, slot, and chief complaint are all confirmed.",
                            "inputSchema": {"json": book_appointment_schema},
                        }},
                        {"toolSpec": {
                            "name": "endCallTool",
                            "description": (
                                "Call this as soon as the caller has nothing further to add - no need "
                                "to say goodbye first. The tool's result will instruct you what to say."
                            ),
                            "inputSchema": {"json": end_call_tool_schema},
                        }},
                    ]
                },
            }
        }
    })


def _tool_result_event(prompt_name: str, content_name: str, content) -> str:
    content_json_string = json.dumps(
        content) if isinstance(content, dict) else content
    return json.dumps({
        "event": {
            "toolResult": {
                "promptName": prompt_name,
                "contentName": content_name,
                "content": content_json_string,
            }
        }
    })


def _summarize_event_for_log(event_json: str) -> str:
    """Compact, base64-free summary of one outbound event, for the
    recent_sent_events diagnostic trail. Full event text minus any bulky
    payload (audioInput/textInput/toolResult content), so a
    ValidationException's likely cause is readable at a glance instead of
    buried in a base64 audio blob."""
    try:
        data = json.loads(event_json)
    except json.JSONDecodeError:
        return event_json[:200]

    event = data.get("event", {})
    if not event:
        return event_json[:200]

    event_type = next(iter(event), "?")
    body = event[event_type]
    if isinstance(body, dict):
        body = dict(body)
        content = body.get("content")
        if isinstance(content, str) and len(content) > 60:
            body["content"] = f"<{len(content)} chars omitted>"
    return f"{event_type}: {body}"


# After this many failed phone-capture attempts (a reset or a
# too_many_digits overflow), stop asking the caller to repeat their
# number and escalate instead - see _phone_capture_escalation_message().
# Chosen from CALL_REVIEW_2026-08-30.md: six restarts and a hang-up with
# zero booking is strictly worse than asking fewer times and offering a
# fallback.
PHONE_CAPTURE_ESCALATION_THRESHOLD = 3

class BedrockStreamManager:
    """Bidirectional Nova Sonic session. Audio in/out via add_audio_chunk()
    and audio_output_queue; tool calls dispatched to an injected
    tool_executor (which makes HTTP calls to booking_service) instead of a
    hardcoded client, so this module has no dependency on main.py."""

    def __init__(self, tool_executor, model_id="amazon.nova-2-sonic-v1:0", region="us-east-1", system_prompt=None, caller_phone_number=None):
        self.tool_executor = tool_executor
        self.model_id = model_id
        self.region = region
        self.system_prompt = system_prompt or _build_system_prompt()
        # E.164 number from Twilio's own From field (see main.py's
        # twilio_voice/media_stream) -- trusted, signed data, not anything
        # transcribed. Used as a fallback contact number if manual
        # phone-digit capture keeps failing; may be None if it wasn't
        # available on the <Stream> Parameter for this call.
        self.caller_phone_number = caller_phone_number
        self.phone_capture_failures = 0

        self.audio_input_queue = asyncio.Queue()
        self.audio_output_queue = asyncio.Queue()
        self.output_queue = asyncio.Queue()

        self.response_task = None
        self.audio_input_task = None
        self.stream_response = None
        self.is_active = False
        self.bedrock_client = None

        self.role = None
        self.display_assistant_text = False
        self.call_should_end = asyncio.Event()

        # Barge-in: set whenever Bedrock reports a new USER content block
        # starting - the signal that the caller has begun a new turn,
        # which should take priority over whatever assistant audio is
        # still queued/streaming. main.py's forwarding loop watches this
        # and clears both audio_output_queue and Twilio's own playback
        # buffer when it fires. Previously nothing in this codebase did
        # this at all - already-generated audio always played out in
        # full regardless of the caller talking over it.
        self.barge_in_signal = asyncio.Event()

        # Deterministic phone-number capture: the model reports fragments as
        # it hears them, code owns the concatenation and the final value.
        # registerPatientTool/updatePatientPhoneTool are only ever given
        # self.confirmed_phone, never whatever the model itself types into
        # the phone argument - see _execute_tool.
        self.phone_digits_buffer = ""
        self.confirmed_phone = None
        # True once the buffer holds a caller-confirmed-ready number -
        # either the domestic path hitting exactly 10 digits, or the
        # international path's explicit done=true. confirmPhoneNumberTool
        # checks this instead of a hardcoded length, so it works for both
        # paths without knowing which one filled the buffer.
        self.phone_digits_ready = False

        # Same pattern for name and date of birth: findPatientTool/
        # registerPatientTool are only ever given the confirmed values, not
        # whatever the model itself typed - see _execute_tool. Name fields
        # accumulate independently (first/last can arrive in separate
        # turns); DOB is collected as one unit since it's usually given
        # together, but validated (real date, plausible year) before it can
        # be confirmed.
        self.pending_first_name = ""
        self.pending_last_name = ""
        self.confirmed_first_name = None
        self.confirmed_last_name = None
        self.pending_dob = None
        self.confirmed_dob = None

        self.prompt_name = str(uuid.uuid4())
        self.content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())
        self.tool_use_content = {}
        self.tool_use_id = ""
        self.tool_name = ""

        self.pending_tool_tasks = {}

        # Diagnostic trail for the intermittent ValidationException
        # (CALL_REVIEW_2026-09-05.md): every prior occurrence only logged
        # that it happened, never what we'd just sent Bedrock, so it was
        # never root-causeable. Rolling window of the last few outbound
        # events, base64 payloads summarized rather than dumped in full -
        # printed by _process_responses's exception handler when it fires.
        self.recent_sent_events = deque(maxlen=8)

        # Set at the very start of close() -- lets _process_responses's
        # exception handler tell "the stream ended because we're already
        # tearing it down on purpose" apart from "the stream broke on its
        # own mid-call". The former is expected noise (contentEnd/
        # promptEnd/sessionEnd racing the SDK's read loop); the latter is
        # the one shape (2026-09-05, "Call A") that's actually worth the
        # full diagnostic dump.
        self._closing = False

    def _initialize_client(self):
        config = Config(
            endpoint_uri=f"https://bedrock-runtime.{self.region}.amazonaws.com",
            region=self.region,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
        )
        self.bedrock_client = BedrockRuntimeClient(config=config)

    async def initialize_stream(self):
        if not self.bedrock_client:
            self._initialize_client()

        self.stream_response = await self.bedrock_client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(
                model_id=self.model_id)
        )
        self.is_active = True

        prompt_event = _start_prompt_event(self.prompt_name)
        text_content_start = _TEXT_CONTENT_START_EVENT % (
            self.prompt_name, self.content_name, "SYSTEM")
        text_content = _TEXT_INPUT_EVENT % (
            self.prompt_name, self.content_name, self.system_prompt)
        text_content_end = _CONTENT_END_EVENT % (
            self.prompt_name, self.content_name)

        for event in (_START_SESSION_EVENT, prompt_event, text_content_start, text_content, text_content_end):
            await self.send_raw_event(event)
            await asyncio.sleep(0.3)

        self.response_task = asyncio.create_task(self._process_responses())
        self.audio_input_task = asyncio.create_task(
            self._process_audio_input())

        await asyncio.sleep(0.3)
        return self

    async def send_raw_event(self, event_json):
        if not self.stream_response or not self.is_active:
            return
        self.recent_sent_events.append(_summarize_event_for_log(event_json))
        event = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(
                bytes_=event_json.encode("utf-8"))
        )
        await self.stream_response.input_stream.send(event)

    async def send_audio_content_start_event(self):
        event = _CONTENT_START_EVENT % (
            self.prompt_name, self.audio_content_name)
        await self.send_raw_event(event)

    async def _process_audio_input(self):
        while self.is_active:
            try:
                data = await self.audio_input_queue.get()
                blob = base64.b64encode(data["audio_bytes"])
                event = _AUDIO_EVENT_TEMPLATE % (
                    self.prompt_name, self.audio_content_name, blob.decode("utf-8"))
                await self.send_raw_event(event)
            except asyncio.CancelledError:
                break

    def add_audio_chunk(self, audio_bytes):
        self.audio_input_queue.put_nowait({"audio_bytes": audio_bytes})

    async def send_audio_content_end_event(self):
        if not self.is_active:
            return
        event = _CONTENT_END_EVENT % (
            self.prompt_name, self.audio_content_name)
        await self.send_raw_event(event)

    async def send_tool_start_event(self, content_name, tool_use_id):
        event = _TOOL_CONTENT_START_EVENT % (
            self.prompt_name, content_name, tool_use_id)
        await self.send_raw_event(event)

    async def send_tool_result_event(self, content_name, tool_result):
        event = _tool_result_event(self.prompt_name, content_name, tool_result)
        await self.send_raw_event(event)

    async def send_tool_content_end_event(self, content_name):
        event = _CONTENT_END_EVENT % (self.prompt_name, content_name)
        await self.send_raw_event(event)

    async def send_prompt_end_event(self):
        if not self.is_active:
            return
        await self.send_raw_event(_PROMPT_END_EVENT % (self.prompt_name))

    async def send_session_end_event(self):
        if not self.is_active:
            return
        await self.send_raw_event(_SESSION_END_EVENT)
        self.is_active = False

    async def _process_responses(self):
        try:
            while self.is_active:
                try:
                    output = await self.stream_response.await_output()
                    result = await output[1].receive()
                    # result can resolve to None (not raise StopAsyncIteration)
                    # if the input stream gets closed mid-read -- close()
                    # closing stream_response.input_stream while this call is
                    # still in flight is exactly that race. is_active is
                    # already False by the time this happens (set by
                    # send_session_end_event() just before close() closes the
                    # stream), so `continue` here just lets the loop's own
                    # `while self.is_active` check end it cleanly next
                    # iteration, instead of treating a normal hangup as an
                    # unexpected error.
                    if result is None or not (result.value and result.value.bytes_):
                        continue

                    json_data = json.loads(result.value.bytes_.decode("utf-8"))
                    event = json_data.get("event", {})

                    if "contentStart" in event:
                        self.role = event["contentStart"]["role"]
                        if self.role == "USER":
                            self.barge_in_signal.set()
                        self.display_assistant_text = False
                        additional_fields_raw = event["contentStart"].get(
                            "additionalModelFields")
                        if additional_fields_raw:
                            try:
                                additional_fields = json.loads(
                                    additional_fields_raw)
                                self.display_assistant_text = (
                                    additional_fields.get(
                                        "generationStage") == "SPECULATIVE"
                                )
                            except json.JSONDecodeError:
                                pass
                    elif "textOutput" in event:
                        text_content = event["textOutput"].get("content", "")
                        role = event["textOutput"].get("role", self.role)
                        if role == "USER" or (role == "ASSISTANT" and self.display_assistant_text):
                            print(f"TRANSCRIPT [{role}]: {text_content}")
                    elif "audioOutput" in event:
                        audio_bytes = base64.b64decode(
                            event["audioOutput"]["content"])
                        await self.audio_output_queue.put(audio_bytes)
                    elif "toolUse" in event:
                        self.tool_use_content = event["toolUse"]
                        self.tool_name = event["toolUse"]["toolName"]
                        self.tool_use_id = event["toolUse"]["toolUseId"]
                    elif event.get("contentEnd", {}).get("type") == "TOOL":
                        self.handle_tool_request(
                            self.tool_name, self.tool_use_content, self.tool_use_id)

                    await self.output_queue.put(json_data)
                except StopAsyncIteration:
                    break
                except Exception as e:
                    if self._closing:
                        # close() was already in progress when the SDK's
                        # read loop broke - expected noise (it's racing
                        # our own contentEnd/promptEnd/sessionEnd), not a
                        # real failure. Skip the diagnostic dump; just
                        # note the session ended.
                        print("INFO: response stream ended (session already closing).")
                    else:
                        print(f"ERROR processing response: {e!r}")
                        # Diagnostic trail for CALL_REVIEW_2026-09-05.md's top
                        # priority item - every prior occurrence only logged
                        # that this fired, never what we'd just sent Bedrock,
                        # so it was never root-causeable. Print what led up to
                        # it now, oldest first.
                        print(f"ERROR context - last {len(self.recent_sent_events)} "
                              "event(s) sent to Bedrock before this:")
                        for i, sent in enumerate(self.recent_sent_events):
                            print(f"  [{i}] {sent}")
                    self.call_should_end.set()
                    break
        finally:
            self.is_active = False

    def handle_tool_request(self, tool_name, tool_content, tool_use_id):
        content_name = str(uuid.uuid4())
        task = asyncio.create_task(
            self._execute_tool_and_send_result(
                tool_name, tool_content, tool_use_id, content_name)
        )
        self.pending_tool_tasks[content_name] = task
        task.add_done_callback(
            lambda t: self.pending_tool_tasks.pop(content_name, None))

    def _phone_capture_escalation_message(self):
        """Called once phone_capture_failures crosses the threshold, from
        either failure path in collectPhoneDigitsTool. Stops the retry
        loop that CALL_REVIEW_2026-08-30.md documented running six times
        with no booking at the end of it. Same caller-ID-offer path as the
        upfront step 1 in COLLECTING DETAILS, just reached a second way -
        offered again here in case the caller declined it the first time
        but has since changed their mind after struggling manually."""
        if self.caller_phone_number:
            digits = self.caller_phone_number.lstrip("+")
            grouped = "-".join(digits[i:i + 3] for i in range(0, len(digits), 3))
            return {
                "status": "escalate",
                "message": (
                    "Multiple attempts to capture the phone number by voice have failed. "
                    "Stop asking the caller to repeat their number. Instead, ask if it's okay "
                    f"to use the number they're calling from - read it back to them grouped "
                    f"as {grouped} - as their contact number, a single yes/no question, "
                    "nothing else. If they say yes, call useCallerIdAsPhoneTool. If they say "
                    "no, apologize, let them know someone from the clinic will call them back "
                    "to confirm the appointment details, and call endCallTool."
                ),
            }
        return {
            "status": "escalate",
            "message": (
                "Multiple attempts to capture the phone number by voice have failed and no "
                "caller ID is available for this call. Stop asking the caller to repeat "
                "their number. Apologize, let them know someone from the clinic will call "
                "them back to confirm the appointment details, and call endCallTool."
            ),
        }

    async def _execute_tool(self, tool_name, tool_content):
        tool = tool_name.lower()
        content = tool_content.get("content", {})
        if isinstance(content, str):
            try:
                content_data = json.loads(content)
            except json.JSONDecodeError:
                content_data = {}
        else:
            content_data = content

        tool_map = {
            "findpatienttool": "findPatientTool",
            "registerpatienttool": "registerPatientTool",
            "updatepatientphonetool": "updatePatientPhoneTool",
            "getdoctortool": "getDoctorTool",
            "findnextopeningtool": "findNextOpeningTool",
            "bookappointmenttool": "bookAppointmentTool",
            "collectphonedigitstool": "collectPhoneDigitsTool",
            "collectinternationalphonedigitstool": "collectInternationalPhoneDigitsTool",
            "confirmphonenumbertool": "confirmPhoneNumberTool",
            "usecalleridasphonetool": "useCallerIdAsPhoneTool",
            "collectpatientnametool": "collectPatientNameTool",
            "confirmpatientnametool": "confirmPatientNameTool",
            "collectdobtool": "collectDobTool",
            "confirmdobtool": "confirmDobTool",
            "endcalltool": "endCallTool",
        }
        canonical_name = tool_map.get(tool)
        if canonical_name is None:
            return {"error": f"Unsupported tool: {tool_name}"}

        # Logged as "raw" deliberately -- for registerPatientTool/
        # updatePatientPhoneTool this is what the model itself put in the
        # phone argument, BEFORE the confirmed-phone override below runs.
        # Don't trust this line alone to prove what actually got sent to
        # booking_service -- see the "TOOL CALL (sent)" line right before
        # dispatch for that.
        print(f"TOOL CALL (raw): {canonical_name} args={content_data}")

        if canonical_name == "endCallTool":
            self.call_should_end.set()
            return {
                "success": True,
                "instruction": (
                    "Say a brief, warm goodbye out loud right now. "
                    "The call will end automatically once you finish speaking - "
                    "do not call endCallTool again."
                ),
            }

        if canonical_name == "collectPhoneDigitsTool":
            is_reset = bool(content_data.get("reset"))
            if is_reset:
                self.phone_digits_buffer = ""
                self.confirmed_phone = None
                self.phone_digits_ready = False
                self.phone_capture_failures += 1
                if self.phone_capture_failures >= PHONE_CAPTURE_ESCALATION_THRESHOLD:
                    return self._phone_capture_escalation_message()

            # Bug fixed here: a reset used to return immediately, before
            # this line ran - any digits sent alongside reset=True (e.g.
            # "that's wrong, it's actually one one one") were silently
            # discarded instead of starting the new buffer. Confirmed
            # against CALL_REVIEW_2026-08-30_CALL2.md §2's exact arithmetic.
            stripped = "".join(ch for ch in str(content_data.get("digits", "")) if ch.isdigit())
            self.phone_digits_buffer += stripped

            if is_reset and not stripped:
                return {
                    "status": "reset",
                    "message": "Cleared. Ask for the phone number again from the start.",
                }

            if len(self.phone_digits_buffer) < 10:
                self.phone_digits_ready = False
                return {
                    "status": "collecting",
                    "digitsSoFar": len(self.phone_digits_buffer),
                    "message": (
                        f"Got {len(self.phone_digits_buffer)} of 10 digits so far. Ask for the "
                        "rest - do not guess or fill in missing digits yourself. If the caller "
                        "indicates this is an international number, switch to "
                        "collectInternationalPhoneDigitsTool instead - do not keep forcing 10 "
                        "digits on a number that isn't a US number."
                    ),
                }
            if len(self.phone_digits_buffer) > 10:
                self.phone_digits_buffer = ""
                self.phone_digits_ready = False
                self.phone_capture_failures += 1
                if self.phone_capture_failures >= PHONE_CAPTURE_ESCALATION_THRESHOLD:
                    return self._phone_capture_escalation_message()
                return {
                    "status": "too_many_digits",
                    "message": (
                        "That's more than 10 digits total - something got repeated or misheard, "
                        "or this may be an international number (use "
                        "collectInternationalPhoneDigitsTool instead if so). Buffer cleared. Ask "
                        "the caller for their full 10-digit number again from the start."
                    ),
                }

            grouped = f"{self.phone_digits_buffer[0:3]}-{self.phone_digits_buffer[3:6]}-{self.phone_digits_buffer[6:10]}"
            self.phone_digits_ready = True
            return {
                "status": "complete",
                "phoneGrouped": grouped,
                "message": (
                    f"All 10 digits collected: {grouped}. Read this back to the caller exactly "
                    "as grouped and get an explicit yes before calling confirmPhoneNumberTool."
                ),
            }

        if canonical_name == "collectInternationalPhoneDigitsTool":
            is_reset = bool(content_data.get("reset"))
            if is_reset:
                self.phone_digits_buffer = ""
                self.confirmed_phone = None
                self.phone_digits_ready = False
                self.phone_capture_failures += 1
                if self.phone_capture_failures >= PHONE_CAPTURE_ESCALATION_THRESHOLD:
                    return self._phone_capture_escalation_message()

            stripped = "".join(ch for ch in str(content_data.get("digits", "")) if ch.isdigit())
            self.phone_digits_buffer += stripped
            is_done = bool(content_data.get("done"))

            if is_reset and not stripped and not is_done:
                return {
                    "status": "reset",
                    "message": "Cleared. Ask for the phone number again from the start.",
                }

            if not is_done:
                self.phone_digits_ready = False
                return {
                    "status": "collecting",
                    "digitsSoFar": len(self.phone_digits_buffer),
                    "message": (
                        f"Got {len(self.phone_digits_buffer)} digits so far. Keep calling this "
                        "with each new fragment, immediately, one call per caller turn - same "
                        "rule as the domestic tool. Only pass done=true once the caller confirms "
                        "that was their complete number."
                    ),
                }

            # done=true: sanity-check length against real-world E.164 bounds
            # (a real phone number, any country, is 7-15 digits) rather than
            # a fixed count - there's no single "correct" length across
            # countries the way there is for a US number.
            digit_count = len(self.phone_digits_buffer)
            if digit_count < 7 or digit_count > 15:
                self.phone_digits_buffer = ""
                self.phone_digits_ready = False
                self.phone_capture_failures += 1
                if self.phone_capture_failures >= PHONE_CAPTURE_ESCALATION_THRESHOLD:
                    return self._phone_capture_escalation_message()
                return {
                    "status": "invalid",
                    "message": (
                        f"{digit_count} digits doesn't look like a real phone number (expected "
                        "roughly 7-15, including country code). Buffer cleared. Ask the caller "
                        "to repeat their full number, including country code, from the start."
                    ),
                }

            grouped = "-".join(
                self.phone_digits_buffer[i:i + 3]
                for i in range(0, digit_count, 3)
            )
            self.phone_digits_ready = True
            return {
                "status": "complete",
                "phoneGrouped": grouped,
                "message": (
                    f"{digit_count} digits collected: {grouped}. Read this back to the caller "
                    "exactly as grouped, including country code, and get an explicit yes before "
                    "calling confirmPhoneNumberTool."
                ),
            }

        if canonical_name == "confirmPhoneNumberTool":
            if not self.phone_digits_ready:
                return {
                    "success": False,
                    "error": (
                        "No complete, unconfirmed phone number is pending. Call "
                        "collectPhoneDigitsTool or collectInternationalPhoneDigitsTool first "
                        "until it reports status 'complete'."
                    ),
                }
            self.confirmed_phone = self.phone_digits_buffer
            self.phone_digits_buffer = ""
            self.phone_digits_ready = False
            return {"success": True, "message": "Phone number confirmed and locked in."}

        if canonical_name == "useCallerIdAsPhoneTool":
            # Only reachable in practice after the caller has already said
            # yes to using their caller ID (per the escalation message) -
            # still checked here rather than trusted, same as every other
            # confirmed_* value in this class.
            if not self.caller_phone_number:
                return {
                    "success": False,
                    "error": "No caller ID is available for this call.",
                }
            self.confirmed_phone = self.caller_phone_number
            self.phone_digits_buffer = ""
            return {
                "success": True,
                "message": "Using the caller's own number as the confirmed contact number.",
            }

        if canonical_name == "collectPatientNameTool":
            first = str(content_data.get("firstName", "")).strip()
            last = str(content_data.get("lastName", "")).strip()
            if first:
                self.pending_first_name = first
            if last:
                self.pending_last_name = last

            missing = []
            if not self.pending_first_name:
                missing.append("first name")
            if not self.pending_last_name:
                missing.append("last name")
            if missing:
                return {
                    "status": "incomplete",
                    "missing": missing,
                    "message": (
                        f"Still need the caller's {' and '.join(missing)}. Ask for it "
                        "specifically - do not proceed without both."
                    ),
                }

            return {
                "status": "complete",
                "firstName": self.pending_first_name,
                "lastName": self.pending_last_name,
                "message": (
                    f"Full name collected: {self.pending_first_name} {self.pending_last_name}. "
                    "Read it back to the caller and get an explicit yes before calling "
                    "confirmPatientNameTool."
                ),
            }

        if canonical_name == "confirmPatientNameTool":
            if not (self.pending_first_name and self.pending_last_name):
                return {
                    "success": False,
                    "error": (
                        "No complete name is pending. Call collectPatientNameTool first until "
                        "it reports status 'complete'."
                    ),
                }
            self.confirmed_first_name = self.pending_first_name
            self.confirmed_last_name = self.pending_last_name
            return {"success": True, "message": "Name confirmed and locked in."}

        if canonical_name == "collectDobTool":
            try:
                dob = date(
                    int(content_data.get("year")),
                    int(content_data.get("month")),
                    int(content_data.get("day")),
                )
            except (TypeError, ValueError):
                return {
                    "status": "invalid",
                    "message": (
                        "That's not a valid date. Ask the caller for their date of birth "
                        "again - month, day, and a 4-digit year."
                    ),
                }

            if dob > date.today() or dob.year < date.today().year - 110:
                return {
                    "status": "implausible",
                    "message": (
                        f"A birth year of {dob.year} doesn't look right. Never guess or assume "
                        "a year the caller didn't actually say - ask them to clearly repeat "
                        "their date of birth, including the year."
                    ),
                }

            self.pending_dob = dob.isoformat()
            return {
                "status": "complete",
                "dob": self.pending_dob,
                "message": (
                    f"Date of birth collected: {dob.strftime('%B %d, %Y')}. Read it back to "
                    "the caller and get an explicit yes before calling confirmDobTool."
                ),
            }

        if canonical_name == "confirmDobTool":
            if not self.pending_dob:
                return {
                    "success": False,
                    "error": (
                        "No complete date of birth is pending. Call collectDobTool first "
                        "until it reports status 'complete'."
                    ),
                }
            self.confirmed_dob = self.pending_dob
            return {"success": True, "message": "Date of birth confirmed and locked in."}

        if canonical_name in ("findPatientTool", "registerPatientTool"):
            # Same hard-guarantee pattern as the phone number: these calls
            # only ever use the confirmed name/DOB, never whatever the
            # model itself typed - this is what closes the gap found in
            # the follow-up test (an empty last name and a fabricated
            # 1912 birth year both silently wrote to the database before
            # this existed).
            if not (self.confirmed_first_name and self.confirmed_last_name):
                return {
                    "error": (
                        "No confirmed name on file yet. Collect it with "
                        "collectPatientNameTool, read it back, and call "
                        "confirmPatientNameTool before calling this tool."
                    )
                }
            if not self.confirmed_dob:
                return {
                    "error": (
                        "No confirmed date of birth on file yet. Collect it with "
                        "collectDobTool, read it back, and call confirmDobTool before "
                        "calling this tool."
                    )
                }
            content_data["firstName"] = self.confirmed_first_name
            content_data["lastName"] = self.confirmed_last_name
            content_data["dob"] = self.confirmed_dob

        if canonical_name in ("registerPatientTool", "updatePatientPhoneTool"):
            # Hard guarantee, not just a prompt instruction: these calls can
            # only ever use the code-collected, explicitly-confirmed phone
            # number, never whatever string the model itself put in the
            # phone argument. This is what actually prevents the Jimmy Chang
            # bug (a silently-reconstructed wrong number written to the DB)
            # from recurring, regardless of what the model does or doesn't
            # remember to do in the prompt.
            if not self.confirmed_phone:
                return {
                    "error": (
                        "No confirmed phone number on file for this call yet. Collect the "
                        "digits with collectPhoneDigitsTool, read the grouped number back to "
                        "the caller, and call confirmPhoneNumberTool after they confirm it - "
                        "before calling this tool."
                    )
                }
            content_data["phone"] = self.confirmed_phone

        # This is the value that actually reaches booking_service -- the
        # one worth trusting when verifying a fix against real call logs.
        print(f"TOOL CALL (sent): {canonical_name} args={content_data}")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.tool_executor, canonical_name, content_data)

    async def _execute_tool_and_send_result(self, tool_name, tool_content, tool_use_id, content_name):
        try:
            tool_result = await self._execute_tool(tool_name, tool_content)
        except Exception as e:
            tool_result = {"error": f"Tool execution failed: {str(e)}"}

        await self.send_tool_start_event(content_name, tool_use_id)
        await asyncio.sleep(0.2)
        await self.send_tool_result_event(content_name, tool_result)
        await asyncio.sleep(0.2)
        await self.send_tool_content_end_event(content_name)

    async def close(self):
        self._closing = True

        if not self.is_active:
            return

        for task in self.pending_tool_tasks.values():
            task.cancel()

        if self.audio_input_task and not self.audio_input_task.done():
            self.audio_input_task.cancel()
            # Bug found via CALL_LOG_2026-09-05_1951.md's diagnostic trail
            # (Fix 9): cancel() only requests cancellation, it doesn't wait
            # for it - without this, _process_audio_input() could still be
            # mid-send (or have items queued to drain) when
            # send_audio_content_end_event() fires right after, sending
            # more audioInput events for a content block we're
            # simultaneously closing. Bounded wait since there's no real
            # work left to finish, just a queue drain - should return
            # almost instantly. _process_audio_input() already catches its
            # own CancelledError internally, so this should complete
            # cleanly rather than raise; the try/except is a safety net,
            # not the expected path.
            try:
                await asyncio.wait_for(self.audio_input_task, timeout=1.0)
            except Exception:
                pass

        await self.send_audio_content_end_event()
        await self.send_prompt_end_event()
        await self.send_session_end_event()

        if self.stream_response:
            await self.stream_response.input_stream.close()

        if self.response_task and not self.response_task.done():
            try:
                await asyncio.wait_for(self.response_task, timeout=2.0)
            except Exception:
                self.response_task.cancel()
