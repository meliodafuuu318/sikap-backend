import base64
import binascii
import json
import os
from typing import TypedDict

from google import genai
from google.genai import types


# ---------- schemas ----------

class Source(TypedDict):
    page: int      # 0 if the file has no pages
    line: int      # 0 if unknown
    excerpt: str   # short verbatim quote; empty if not from the file


class Question(TypedDict):
    question: str
    options: list[str]
    correct_index: int
    category: str


class QuestionWithSource(Question):
    source: Source


# ---------- config ----------

GEMINI_CLIENT = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"]
)

DIFFICULTY_LEVELS = {
    "easy": "primary school level",
    "medium": "high school level",
    "hard": "college level",
}

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
MAX_ANALYZE_QUESTIONS = 50

# A category scoring below this percentage counts as a weakness
WEAK_BELOW_PERCENT = 70
UNCATEGORIZED = "Uncategorized"

CORS_HEADERS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "Content-Type"),
]

STATUS_TEXT = {
    200: "200 OK",
    400: "400 Bad Request",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    500: "500 Internal Server Error",
}


# ---------- helpers ----------

def respond(start_response, status, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    headers = CORS_HEADERS + [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]
    start_response(STATUS_TEXT[status], headers)
    return [body]


def read_json(environ):
    """Returns the parsed body as a dict, or None if invalid."""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        data = json.loads(environ["wsgi.input"].read(length) or b"{}")
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None

def get_client():
    return GEMINI_CLIENT


def get_model():
    return os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")


def normalize_source(question):
    """Turn the model's source into null when it isn't backed by the file."""
    src = question.get("source")
    if not isinstance(src, dict):
        question["source"] = None
        return question
    page = src.get("page") or None
    line = src.get("line") or None
    excerpt = str(src.get("excerpt") or "").strip()
    question["source"] = (
        {"page": page, "line": line, "excerpt": excerpt}
        if excerpt or page or line
        else None
    )
    return question


def normalize_categories(questions):
    """Make labels that differ only by case/spacing identical (first seen wins)."""
    seen = {}
    for q in questions:
        label = " ".join(str(q.get("category") or "").split()) or UNCATEGORIZED
        q["category"] = seen.setdefault(label.casefold(), label)
    return questions


# ---------- /api/generate ----------

def handle_generate(environ, start_response):
    data = read_json(environ)
    if data is None:
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

    # optional file
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
        "and correct_index is the 0-based index of the correct option.\n\n"
        "Categorization rules (the quiz results are grouped by category, so follow "
        "these strictly):\n"
        "- Every question must have a category: a short subtopic label of 1-3 words "
        "in Title Case, such as 'Cell Biology' or 'Fractions'.\n"
        "- A category must be a specific subtopic of the quiz topic, never the whole "
        "topic itself and never a generic label like 'General' or 'Other'.\n"
        "- If the topic lists several subjects (for example separated by commas), "
        "use those subjects as the categories.\n"
        "- Use 2 to 5 distinct categories overall (1 or 2 if there are only a few "
        "questions), and spread the questions across them as evenly as possible. "
        "Where possible, give each category at least 2 questions.\n"
        "- Reuse the exact same label, with identical spelling and capitalization, "
        "for every question in the same category."
    )
    if file_part:
        prompt += (
            "\n\nA reference file is attached. Base the questions on its content wherever "
            "relevant to the topic. If the file is unrelated to the topic or unreadable, "
            "ignore it and use your own knowledge. For each question, fill in source: "
            "page is the page number in the file (0 if the file has no pages, such as "
            "plain text or an image), line is the approximate line number on that page "
            "(0 if unsure), and excerpt is a short verbatim quote (under 200 characters) "
            "from the file that the question is based on. If a question is not based on "
            "the file, use page 0, line 0 and an empty excerpt. Never invent quotes."
        )

    try:
        client = get_client()

        response = client.models.generate_content(
            model=get_model(),
            contents=[file_part, prompt] if file_part else prompt,
            config=types.GenerateContentConfig(
                system_instruction="You are a quiz generator API.",
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=list[QuestionWithSource] if file_part else list[Question],
            ),
        )
        
        questions = [normalize_source(q) for q in json.loads(response.text)]
        return respond(start_response, 200, normalize_categories(questions))
    except Exception as e:
        print("Gemini error:", e)
        return respond(start_response, 500, {"error": "Failed to generate questions"})


# ---------- /api/analyze ----------

def compute_category_stats(items):
    """Group items by category and compute the percentage answered correctly."""
    groups = {}
    for item in items:
        key = item["category"].casefold()
        group = groups.setdefault(
            key, {"category": item["category"], "correct": 0, "total": 0}
        )
        group["total"] += 1
        group["correct"] += 1 if item["is_correct"] else 0

    result = list(groups.values())
    for group in result:
        group["percentage"] = round(group["correct"] / group["total"] * 100)
    # lowest percentage first
    return sorted(result, key=lambda g: (g["percentage"], g["category"]))


def handle_analyze(environ, start_response):
    data = read_json(environ)
    if data is None:
        return respond(start_response, 400, {"error": "Invalid JSON body"})

    raw = data.get("questions")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ANALYZE_QUESTIONS:
        return respond(
            start_response,
            400,
            {"error": f"questions must be a list of 1 to {MAX_ANALYZE_QUESTIONS} items"},
        )

    items = []
    for entry in raw:
        if not isinstance(entry, dict):
            return respond(start_response, 400, {"error": "Invalid question entry"})
        category = " ".join(str(entry.get("category") or "").split())[:60]
        items.append(
            {
                "category": category or UNCATEGORIZED,
                "is_correct": entry.get("isCorrect") is True,
            }
        )

    score = sum(1 for i in items if i["is_correct"])
    categories = compute_category_stats(items)

    # Lowest-scoring categories are the weaknesses. Old sessions saved without a
    # category are skipped, since they can't be used as a quiz topic.
    weaknesses = [
        c["category"]
        for c in categories
        if c["percentage"] < WEAK_BELOW_PERCENT and c["category"] != UNCATEGORIZED
    ]

    return respond(
        start_response,
        200,
        {
            "score": score,
            "total": len(items),
            "percentage": round(score / len(items) * 100),
            "categories": categories,
            "weaknesses": weaknesses,
        },
    )


# ---------- entrypoint ----------

def app(environ, start_response):
    method = environ["REQUEST_METHOD"]

    if method == "OPTIONS":
        return respond(start_response, 200)
    if method != "POST":
        return respond(start_response, 405, {"error": "Method not allowed"})

    path = environ.get("PATH_INFO", "").rstrip("/")
    if path.endswith("/analyze"):
        return handle_analyze(environ, start_response)
    return handle_generate(environ, start_response)