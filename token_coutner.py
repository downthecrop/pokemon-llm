import logging
import tiktoken

# https://platform.openai.com/docs/guides/images-vision
MODEL_VISION_TOKEN_MULT = 2.5 # gpt-4.1-nano is 2.46 learn more here: 
IMAGE_TOKEN_COST_HIGH_DETAIL = int(258 * MODEL_VISION_TOKEN_MULT)
IMAGE_TOKEN_COST_LOW_DETAIL = int(85 * MODEL_VISION_TOKEN_MULT)

log = logging.getLogger('token_counter')

"""
Note that you can use
    ...
    stream=True,
    stream_options={"include_usage": True},
)

for a more accurate token usage.
"""
try:
    encoding = tiktoken.get_encoding("cl100k_base")
    log.info("Tiktoken encoder 'cl100k_base' loaded.")
except Exception as e:
    log.warning(f"Failed to load tiktoken encoder: {e}. Token counts will be approximate (char/4).")
    encoding = None


def count_tokens(text: str) -> int:
    """Estimates token count for a given text using the loaded encoding."""
    if not text:
        return 0
    if not encoding:
        return len(text) // 4
    try:
        return len(encoding.encode(text))
    except Exception as e:
        log.warning(f"Tiktoken encoding failed (len {len(text)}): {e}. Using fallback.")
        return len(text) // 4


def calculate_prompt_tokens(messages):
    """Estimates token count for a list of messages."""
    tokens = 0
    tokens_per_message = 3
    tokens_per_role = 1
    try:
        for message in messages:
            tokens += tokens_per_message + tokens_per_role
            content = message.get('content', '')
            if isinstance(content, str):
                tokens += count_tokens(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        item_type = item.get('type')
                        if item_type == 'text':
                            tokens += count_tokens(item.get('text', ''))
                        elif item_type == 'image_url':
                            tokens += IMAGE_TOKEN_COST_HIGH_DETAIL
        tokens += 3
        return tokens
    except Exception as e:
         log.error(f"Error calculating prompt tokens: {e}", exc_info=True)
         return 0