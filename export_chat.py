"""
Export Cursor chat JSONL to a readable Markdown file.
Usage: python export_chat.py
Output: chat_export.md in the same folder
"""

import json
import re
from pathlib import Path

JSONL_PATH = Path(
    r"C:\Users\Mark\.cursor\projects"
    r"\c-Users-Mark-PycharmProjects-Actors-tripadvisor"    
    r"\agent-transcripts"
    r"\1469c369-a0f0-443d-8b5c-e8ceaea382f7"
    r"\1469c369-a0f0-443d-8b5c-e8ceaea382f7.jsonl"
)
OUTPUT_PATH = Path(__file__).parent / "chat_export.md"

# Tags to strip from user messages (Cursor injects these automatically)
STRIP_PATTERNS = [
    r"<user_query>\s*",
    r"\s*</user_query>",
    r"<system_reminder>.*?</system_reminder>",
    r"<open_and_recently_viewed_files>.*?</open_and_recently_viewed_files>",
    r"<git_status>.*?</git_status>",
    r"<agent_transcripts>.*?</agent_transcripts>",
    r"<agent_skills>.*?</agent_skills>",
    r"<image_files>.*?</image_files>",
    r"<user_info>.*?</user_info>",
]


def clean_user_text(text: str) -> str:
    for pattern in STRIP_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.DOTALL)
    return text.strip()


def extract_text(content: list) -> str:
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def main():
    lines = JSONL_PATH.read_text(encoding="utf-8").splitlines()
    messages = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = obj.get("role", "")
        content = obj.get("message", {}).get("content", [])
        if not content:
            continue
        text = extract_text(content)
        if not text.strip():
            continue
        if role == "user":
            text = clean_user_text(text)
            if not text:
                continue
        messages.append((role, text))

    md_lines = ["# Cursor Chat Export\n", f"**Source:** `{JSONL_PATH.name}`\n", "---\n"]
    for i, (role, text) in enumerate(messages, 1):
        if role == "user":
            md_lines.append(f"\n## 🧑 User (message {i})\n")
        else:
            md_lines.append(f"\n## 🤖 Assistant (message {i})\n")
        md_lines.append(text.strip())
        md_lines.append("\n")

    OUTPUT_PATH.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Done! Exported {len(messages)} messages -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
