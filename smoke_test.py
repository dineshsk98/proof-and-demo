import os
import sys

from openai import OpenAI

from env import load_env_file

MODEL = "muse-spark-1.3"  # standard tier; never a "-contributor" model
BASE_URL = "https://api.meta.ai/v1"

load_env_file()
api_key = os.environ.get("MODEL_API_KEY")
if not api_key:
    sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and put your key in it.")

client = OpenAI(base_url=BASE_URL, api_key=api_key)
response = client.responses.create(model=MODEL, input="Reply with the single word: ready")

print("Reply:", response.output_text)
usage = response.usage
print(f"Tokens: input={usage.input_tokens} output={usage.output_tokens} total={usage.total_tokens}")
