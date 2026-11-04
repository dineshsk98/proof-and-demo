import os
import sys

from openai import OpenAI

from env import load_env_file
from script_narration import PRICE_IN, PRICE_OUT  # the same model and tier, so the same price
from usage import log_call

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
log_call(MODEL, "smoke test", None, (usage.input_tokens * PRICE_IN + usage.output_tokens * PRICE_OUT) / 1e6,
         input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
