import json
import os
from http.server import BaseHTTPRequestHandler
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


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send_json(400, {"error": "Invalid JSON body"})

        topic = data.get("topic")
        difficulty = str(data.get("difficulty", "")).lower()
        count = data.get("count")

        difficulty_description = DIFFICULTY_LEVELS.get(difficulty)
        if not difficulty_description:
            return self._send_json(400, {"error": "Invalid difficulty"})

        if not isinstance(topic, str) or not topic.strip() or len(topic) > 200:
            return self._send_json(400, {"error": "Invalid topic"})

        try:
            count = int(count)
        except (TypeError, ValueError):
            return self._send_json(400, {"error": "Invalid count"})
        if not 1 <= count <= 20:
            return self._send_json(400, {"error": "Count must be between 1 and 20"})

        prompt = (
            f'Generate exactly {count} multiple-choice questions about "{topic.strip()}" '
            f"({difficulty_description}). Each question must have exactly 4 options, "
            "and correct_index is the 0-based index of the correct option."
        )

        try:
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction="You are a quiz generator API.",
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=list[Question],
                ),
            )
            questions = json.loads(response.text)
            return self._send_json(200, questions)
        except Exception as e:
            print("Gemini error:", e)
            return self._send_json(500, {"error": "Failed to generate questions"})