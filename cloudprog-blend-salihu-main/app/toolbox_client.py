import os
from openai import OpenAI

MODEL_NAME = os.getenv("MODEL_NAME", "gpt-oss-120b")
TOOLBOX_BASE_URL = os.getenv("TOOLBOX_BASE_URL", "https://models.mylab.th-luebeck.dev/v1")

API_KEY = os.getenv("API_KEY", "dummy").strip() or "dummy"

client = OpenAI(base_url=TOOLBOX_BASE_URL, api_key=API_KEY)

def generate_text(system_prompt: str, user_prompt: str) -> str:
    r = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=int(os.getenv("MAX_TOKENS", "2048")),
        stream=False,
    )
    return (r.choices[0].message.content or "").strip()