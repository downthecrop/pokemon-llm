# client_setup.py
import os
import logging
from openai import OpenAI, APIError
from dotenv import load_dotenv
import httpx

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger('llm_client_setup')

# --- Configuration Defaults ---
DEFAULT_MODE = "TOGETHER" # OPENAI, GEMINI, OLLAMA, LMSTUDIO, GROQ, TOGETHER
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-preview-05-20"
DEFAULT_OLLAMA_MODEL = "llava-phi3"
DEFAULT_LMSTUDIO_MODEL = "qwen2.5-vl-32b-instruct"
DEFAULT_GROQ_MODEL = "meta-llama/llama-4-maverick-17b-128e-instruct"
DEFAULT_TOGETHER_MODEL = "Qwen/Qwen2-VL-72B-Instruct" # Default for Ollama if not specified

REASONING_EFFORT = "low"
MINIMAP_ENABLED = True # Set to False to disable minimap features
MINIMAP_2D = True # Set to False to disable 2D minimap features
REASONING_ENABLED = True # Set to False to disable reasoning features
MAX_TOKENS = 2048
TEMPERATURE = 0.7 # Default temperature for model responses
IMAGE_DETAIL = "low" # Default image detail level

TIMEOUT = httpx.Timeout(15.0, read=15.0, write=10.0, connect=10.0) 

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

    client = None
    model = None
    supports_reasoning = False

    log.info(f"--- Initializing LLM Client (Mode: {MODE}) ---")

    if MODE == "OPENAI":
        # OpenAI requires a real API key from environment
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            log.error("MODE is OPENAI but OPENAI_API_KEY not found in environment variables.")
            return None, None
        try:
            client = OpenAI(api_key=api_key, timeout=TIMEOUT)
            model = get_config("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
            #supports_reasoning = True
            log.info(f"Using OpenAI Mode. Model: {model}")
        except Exception as e:
            log.error(f"Failed to initialize OpenAI client: {e}", exc_info=True)
            return None, None

    elif MODE == "GEMINI":
        # Gemini requires a real API key from environment
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            log.error("MODE is GEMINI but GEMINI_API_KEY not found in environment variables.")
            return None, None
        try:
            client = OpenAI(
                api_key=api_key,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                timeout=TIMEOUT
            )
            model = get_config("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
            supports_reasoning = True
            log.info(f"Using Gemini Mode (via OpenAI client). Model: {model}")
        except Exception as e:
            log.error(f"Failed to initialize Gemini client (via OpenAI compat): {e}", exc_info=True)
            return None, None

    elif MODE == "OLLAMA":
        ollama_base_url = get_config("OLLAMA_BASE_URL", 'http://localhost:11434/v1')
        try:
            client = OpenAI(
                base_url=ollama_base_url,
                api_key='ollama', # Hardcoded placeholder key for Ollama
            )
            model = get_config("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
            supports_reasoning = True # Not sure for this
            log.info(f"Using Ollama Mode. Base URL: {ollama_base_url}, Model: {model} (API Key: Placeholder)")
        except Exception as e:
            log.error(f"Failed to initialize Ollama client: {e}", exc_info=True)
            return None, None

    elif MODE == "LMSTUDIO":
        lmstudio_base_url = get_config("LMSTUDIO_BASE_URL", 'http://localhost:1234/v1')
        try:
            client = OpenAI(
                base_url=lmstudio_base_url,
                api_key='lmstudio', # Hardcoded placeholder key for LMStudio
            )
            model = get_config("LMSTUDIO_MODEL", DEFAULT_LMSTUDIO_MODEL)
            supports_reasoning = True # Not sure for this
            log.info(f"Using LMStudio Mode. Base URL: {lmstudio_base_url}, Model: {model} (API Key: Placeholder)")
        except Exception as e:
            log.error(f"Failed to initialize LMStudio client: {e}", exc_info=True)
            return None, None
        
    elif MODE == "GROQ":
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            log.error("MODE is GROQ but GROQ_API_KEY not found in environment variables.")
            return None, None
        try:
            client = OpenAI(
                base_url="https://api.groq.com/openai/v1",
                api_key=api_key,
                timeout=TIMEOUT
            )
            model = get_config("GROQ_MODEL", DEFAULT_GROQ_MODEL)
            log.info(f"Using Groq Mode (via OpenAI client). Model: {model}")
        except Exception as e:
            log.error(f"Failed to initialize Groq client: {e}", exc_info=True)
            return None, None
        
    elif MODE == "TOGETHER":
        api_key = os.getenv("TOGETHER_API_KEY")
        if not api_key:
            log.error("MODE is TOGETHER but TOGETHER_API_KEY not found in environment variables.")
            return None, None
        try:
            client = OpenAI(
                base_url="https://api.together.xyz/v1",
                api_key=api_key,
                timeout=TIMEOUT
            )
            model = get_config("TOGETHER_MODEL", DEFAULT_TOGETHER_MODEL)
            log.info(f"Using Together Mode (via OpenAI client). Model: {model}")
        except Exception as e:
            log.error(f"Failed to initialize Together client: {e}", exc_info=True)
            return None, None        

    else:
        log.error(f"Invalid MODE selected: {MODE}. Set MODE environment variable correctly (e.g., OPENAI, GEMINI, OLLAMA, LMSTUDIO).")
        return None, None

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
    return client, model, supports_reasoning