import base64
import binascii
import json
import os
from typing import TypedDict

from google import genai
from google.genai import types


class Question(TypedDict):
    question: str
    options: list[str]
    correct_index: int


DIFFICULTY_LEVELS = {
    "easy": "primary school level",
    "medium": "high school level",
    "hard": "college level",
}

# NEW: file types Gemini can read directly
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/html",
    "image/png",
    "image/jpeg",
    "image/webp",
}
# Vercel rejects request bodies over ~4.5 MB, and base64 adds ~33%
MAX_FILE_BYTES = 3 * 1024 * 1024

CORS_HEADERS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "Content-Type"),
]

STATUS_TEXT = {
    200: "200 OK",
    400: "400 Bad Request",
    405: "405 Method Not Allowed",
    500: "500 Internal Server Error",
}


def respond(start_response, status, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    headers = CORS_HEADERS + [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]
    start_response(STATUS_TEXT[status], headers)
    return [body]


def app(environ, start_response):
    method = environ["REQUEST_METHOD"]

    if method == "OPTIONS":
        return respond(start_response, 200)
    if method != "POST":
        return respond(start_response, 405, {"error": "Method not allowed"})

    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        data = json.loads(environ["wsgi.input"].read(length) or b"{}")
    except (ValueError, json.JSONDecodeError):
        return respond(start_response, 400, {"error": "Invalid JSON body"})

    topic = data.get("topic")
    difficulty = str(data.get("difficulty", "")).lower()

    difficulty_description = DIFFICULTY_LEVELS.get(difficulty)
    if not difficulty_description:
        return respond(start_response, 400, {"error": "Invalid difficulty"})

    if not isinstance(topic, str) or not topic.strip() or len(topic) > 200:
        return respond(start_response, 400, {"error": "Invalid topic"})

    try:
        count = int(data.get("count"))
    except (TypeError, ValueError):
        return respond(start_response, 400, {"error": "Invalid count"})
    if not 1 <= count <= 20:
        return respond(start_response, 400, {"error": "Count must be between 1 and 20"})

    # NEW: optional file
    file_part = None
    file_b64 = data.get("file")
    if file_b64:
        mime_type = str(data.get("file_mime_type", "")).lower()
        if mime_type not in ALLOWED_MIME_TYPES:
            return respond(start_response, 400, {"error": "Unsupported file type"})
        try:
            file_bytes = base64.b64decode(file_b64, validate=True)
        except (binascii.Error, ValueError, TypeError):
            return respond(start_response, 400, {"error": "Invalid file encoding"})
        if len(file_bytes) > MAX_FILE_BYTES:
            return respond(start_response, 400, {"error": "File too large (max 3 MB)"})
        file_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)

    prompt = (
        f'Generate exactly {count} multiple-choice questions about "{topic.strip()}" '
        f"({difficulty_description}). Each question must have exactly 4 options, "
        "and correct_index is the 0-based index of the correct option."
    )
    if file_part:
        prompt += (
            " A reference file is attached. Base the questions on its content wherever "
            "relevant to the topic. If the file is unrelated to the topic or unreadable, "
            "ignore it and use your own knowledge."
        )

    try:
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
            contents=[file_part, prompt] if file_part else prompt,
            config=types.GenerateContentConfig(
                system_instruction="You are a quiz generator API.",
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=list[Question],
            ),
        )
        return respond(start_response, 200, json.loads(response.text))
    except Exception as e:
        print("Gemini error:", e)
        return respond(start_response, 500, {"error": "Failed to generate questions"})