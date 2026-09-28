"""Generate production secrets locally, so they never pass through chat or files.

Run from the project root:   python scripts/generate_secrets.py
Paste the printed values into the hosting dashboard's environment variables.
"""
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import hash_password  # noqa: E402

MIN_LENGTH = 8

def ask(prompt: str) -> str:
    # getpass hides typing but needs a real console; fall back when input is piped.
    return getpass.getpass(prompt) if sys.stdin.isatty() else input(prompt)


password = ask(f"اختر كلمة مرور الدخول للموقع ({MIN_LENGTH} خانات على الأقل): ")
confirm = ask("أعد كتابتها للتأكيد: ")
if password != confirm:
    raise SystemExit("كلمتا المرور غير متطابقتين.")
if len(password) < MIN_LENGTH:
    raise SystemExit(f"كلمة المرور أقصر من {MIN_LENGTH} خانات.")

print()
print("انسخ هذه القيم إلى Render (Environment):")
print(f"API_KEY={secrets.token_hex(32)}")
print(f"SESSION_SECRET={secrets.token_hex(32)}")
print("UI_LOGIN_USER=manager")
print(f"UI_LOGIN_PASSWORD_HASH={hash_password(password)}")
