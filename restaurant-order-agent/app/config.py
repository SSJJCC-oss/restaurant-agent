"""Central place to load settings from environment variables / .env."""
import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    google_cloud_project: str = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    google_cloud_location: str = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    sheets_spreadsheet_id: str = os.environ.get("SHEETS_SPREADSHEET_ID", "")
    twilio_account_sid: str = os.environ.get("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.environ.get("TWILIO_AUTH_TOKEN", "")
    twilio_whatsapp_number: str = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
    dry_run: bool = os.environ.get("DRY_RUN", "true").strip().lower() == "true"


settings = Settings()
