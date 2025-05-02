# client_setup.py
import os
import logging
from openai import OpenAI, APIError
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger('llm_client_setup')

# --- Configuration Defaults ---
DEFAULT_MODE = "GEMINI" # OPENAI, GEMINI, OLLAMA, LMSTUDIO
DEFAULT_IMAGE_DETAIL = "low" # high or low
DEFAULT_OPENAI_MODEL = "gpt-4.1-nano"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-preview-04-17"
DEFAULT_OLLAMA_MODEL = "llama3.2-vision:11b"
DEFAULT_LMSTUDIO_MODEL = "amoral-fallen-omega-gemma3-12b-mlx"

load_dotenv() # Load variables from .env file

def get_config(env_var: str, default_value: str) -> str:
    """Gets configuration from environment variable or returns default."""
    value = os.getenv(env_var, default_value)
    source = 'Env Var' if os.getenv(env_var) else 'Default'
    # Avoid logging sensitive keys like API keys directly
    if "API_KEY" not in env_var:
         log.info(f"Config '{env_var}': {value} (Source: {source})")
    else:
         # Log API keys securely (presence only)
         log.info(f"Config '{env_var}': {'Present' if value else 'Not Set'} (Source: {source})")
    return value

def setup_llm_client() -> tuple[OpenAI | None, str | None, str | None]:
    MODE = get_config("MODE", DEFAULT_MODE)
    IMAGE_DETAIL = get_config("IMAGE_DETAIL", DEFAULT_IMAGE_DETAIL)
    if IMAGE_DETAIL not in ["low", "high"]:
        log.warning(f"Invalid IMAGE_DETAIL '{IMAGE_DETAIL}' found. Defaulting to 'low'.")
        IMAGE_DETAIL = "low"

    client = None
    model = None

    log.info(f"--- Initializing LLM Client (Mode: {MODE}) ---")

    if MODE == "OPENAI":
        # OpenAI requires a real API key from environment
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            log.error("MODE is OPENAI but OPENAI_API_KEY not found in environment variables.")
            return None, None, IMAGE_DETAIL
        try:
            client = OpenAI(api_key=api_key)
            model = get_config("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
            log.info(f"Using OpenAI Mode. Model: {model}")
        except Exception as e:
            log.error(f"Failed to initialize OpenAI client: {e}", exc_info=True)
            return None, None, IMAGE_DETAIL

    elif MODE == "GEMINI":
        # Gemini requires a real API key from environment
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            log.error("MODE is GEMINI but GEMINI_API_KEY not found in environment variables.")
            return None, None, IMAGE_DETAIL
        try:
            client = OpenAI(
                api_key=api_key,
                # Using the OpenAI compatibility endpoint for Gemini
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
            )
            model = get_config("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
            log.info(f"Using Gemini Mode (via OpenAI client). Model: {model}")
        except Exception as e:
            log.error(f"Failed to initialize Gemini client (via OpenAI compat): {e}", exc_info=True)
            return None, None, IMAGE_DETAIL

    elif MODE == "OLLAMA":
        ollama_base_url = get_config("OLLAMA_BASE_URL", 'http://localhost:11434/v1')
        try:
            client = OpenAI(
                base_url=ollama_base_url,
                api_key='ollama', # Hardcoded placeholder key for Ollama
            )
            model = get_config("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
            log.info(f"Using Ollama Mode. Base URL: {ollama_base_url}, Model: {model} (API Key: Placeholder)")
        except Exception as e:
            log.error(f"Failed to initialize Ollama client: {e}", exc_info=True)
            return None, None, IMAGE_DETAIL

    elif MODE == "LMSTUDIO":
        lmstudio_base_url = get_config("LMSTUDIO_BASE_URL", 'http://localhost:1234/v1')
        try:
            client = OpenAI(
                base_url=lmstudio_base_url,
                api_key='lmstudio', # Hardcoded placeholder key for LMStudio
            )
            model = get_config("LMSTUDIO_MODEL", DEFAULT_LMSTUDIO_MODEL)
            log.info(f"Using LMStudio Mode. Base URL: {lmstudio_base_url}, Model: {model} (API Key: Placeholder)")
        except Exception as e:
            log.error(f"Failed to initialize LMStudio client: {e}", exc_info=True)
            return None, None, IMAGE_DETAIL

    else:
        log.error(f"Invalid MODE selected: {MODE}. Set MODE environment variable correctly (e.g., OPENAI, GEMINI, OLLAMA, LMSTUDIO).")
        return None, None, IMAGE_DETAIL

    # Optional: Connection verification (can be commented out if causing issues)
    if client and model:
        try:
            log.info(f"Attempting to verify connection to {MODE} service...")
            models_list = client.models.list()
            log.info(f"Successfully connected to {MODE} service (Base URL: {client.base_url}). Found {len(models_list.data)} models.")
        except APIError as e:
            log.error(f"APIError verifying connection to {MODE}: {e}. Check URL/Permissions/Service Status.")
        except Exception as e:
            log.warning(f"Unexpected error verifying {MODE} connection: {e}. Proceeding cautiously.")

    log.info(f"LLM Client setup complete. Image Detail: {IMAGE_DETAIL}")
    return client, model, IMAGE_DETAIL