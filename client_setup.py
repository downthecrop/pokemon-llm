# client_setup.py
import argparse
import os
import logging
from openai import OpenAI, APIError
from dotenv import load_dotenv
import httpx

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger('llm_client_setup')

# --- Configuration Defaults ---
DEFAULT_MODE = "ANTHOPIC" # OPENAI, GEMINI, OLLAMA, LMSTUDIO, GROQ, TOGETHER, GROK, ANTHOPIC
DEFAULT_OPENAI_MODEL = "o3"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-preview-05-20"
DEFAULT_OLLAMA_MODEL = "gemma3:27b-it-q4_K_M"
DEFAULT_LMSTUDIO_MODEL = "google/gemma-3-27b"
DEFAULT_GROQ_MODEL = "meta-llama/llama-4-maverick-17b-128e-instruct"
DEFAULT_TOGETHER_MODEL = "Qwen/Qwen2.5-VL-72B-Instruct"
DEFAULT_GROK_MODEL = "grok-3-mini"
DEFAULT_ANTHOPIC_MODEL = "claude-sonnet-4-20250514"

DEFAULT_MODEL_BY_MODE = {
    "OPENAI": DEFAULT_OPENAI_MODEL,
    "GEMINI": DEFAULT_GEMINI_MODEL,
    "OLLAMA": DEFAULT_OLLAMA_MODEL,
    "LMSTUDIO": DEFAULT_LMSTUDIO_MODEL,
    "GROQ": DEFAULT_GROQ_MODEL,
    "TOGETHER": DEFAULT_TOGETHER_MODEL,
    "GROK": DEFAULT_GROK_MODEL,
    "ANTHOPIC": DEFAULT_ANTHOPIC_MODEL,
}

MODES = list(DEFAULT_MODEL_BY_MODE.keys())

REASONING_EFFORT = "low" # Default reasoning effort level, can be "low", "medium", or "high" for models that support it
ONE_IMAGE_PER_PROMPT = True # Set to False to allow multiple images per prompt (Often performs better with single image)
MINIMAP_ENABLED = True # Set to False to disable minimap features
MINIMAP_2D = True # Set to False to disable 2D minimap features
REASONING_ENABLED = True # Set to False to disable reasoning features
MAX_TOKENS = 2048 # Default maximum tokens for model responses
SYSTEM_PROMPT_UNSUPPORTED = False # Instead it will be injected into messages. (NOT IMPLEMENTED YET)
TEMPERATURE = 0.7 # Default temperature for model responses
IMAGE_DETAIL = "low" # Default image detail level can be "low", or "high"
USES_MAX_COMPLETION_TOKENS = True # Some models (OAI o3) require setting max_completion_tokens instead of max_tokens
USES_DEFAULT_TEMPERATURE = True # Some models (OAI o3) don't support temperature, so we use a default value (1)

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

# helper function for selecting AI model
def parse_mode_arg(modes, default_mode=DEFAULT_MODE):
    parser = argparse.ArgumentParser(description="Parse LLM mode argument", add_help=False)

    parser.add_argument(
        '--mode',
        choices=modes,
        help='LLM mode to use (choose from the supported modes)'
    )

    # Use parse_known_args to ignore other arguments
    args, _ = parser.parse_known_args()
    mode = args.mode

    if not mode:
        print("\nNo LLM mode specified via command line.")
        print("Please choose the LLM mode from the list below:")
        for i, m in enumerate(modes, start=1):
            print(f"  {i}. {m}")
        choice = input("Enter the number of your choice: ").strip()

        try:
            choice_num = int(choice)
            if 1 <= choice_num <= len(modes):
                mode = modes[choice_num - 1]
                print(f"Great! You selected: {mode}")
            else:
                print(f"Invalid choice. Defaulting to '{default_mode}'.")
                mode = default_mode
        except ValueError:
            print(f"Invalid input. Defaulting to '{default_mode}'.")
            mode = default_mode
    else:
        print(f"LLM mode specified via command line: {mode}")

    # Assuming you have this dictionary defined somewhere:
    print(f"Using default model: {DEFAULT_MODEL_BY_MODE[mode]}")

    return mode

def setup_llm_client() -> tuple[OpenAI | None, str | None, str | None]:
    MODE = parse_mode_arg(MODES)

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
            supports_reasoning = True
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
            #supports_reasoning = True # Not sure for this
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
    
    elif MODE == "GROK":
        api_key = os.getenv("GROK_API_KEY")
        if not api_key:
            log.error("MODE is GROK but GROK_API_KEY not found in environment variables.")
            return None, None
        try:
            client = OpenAI(
                base_url="https://api.x.ai/v1",
                api_key=api_key,
                timeout=TIMEOUT
            )
            supports_reasoning = True # Grok supports reasoning
            model = get_config("GROK_MODEL", DEFAULT_GROK_MODEL)
            log.info(f"Using Grok Mode (via OpenAI client). Model: {model}")
        except Exception as e:
            log.error(f"Failed to initialize Grok client: {e}", exc_info=True)
            return None, None
        
    elif MODE == "ANTHOPIC":
        api_key = os.getenv("ANTHOPIC_API_KEY")
        if not api_key:
            log.error("MODE is ANTHOPIC but ANTHOPIC_API_KEY not found in environment variables.")
            return None, None
        try:
            client = OpenAI(
                base_url="https://api.anthropic.com/v1/",
                api_key=api_key,
                timeout=TIMEOUT
            )
            supports_reasoning = True
            model = get_config("ANTHOPIC_MODEL", DEFAULT_ANTHOPIC_MODEL)
            log.info(f"Using ANTHOPIC Mode (via OpenAI client). Model: {model}")
        except Exception as e:
            log.error(f"Failed to initialize ANTHOPIC client: {e}", exc_info=True)
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
    print(f"Client: {client}, model: {model}, supports_reasoning: {supports_reasoning}")
    return client, model, supports_reasoning