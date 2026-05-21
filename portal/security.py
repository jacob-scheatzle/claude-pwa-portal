import bcrypt

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_BYTES = 72  # bcrypt's hard limit


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def validate_password(password: str) -> list[str]:
    errors: list[str] = []
    if len(password) < MIN_PASSWORD_LEN:
        errors.append(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        errors.append(f"Password must be {MAX_PASSWORD_BYTES} bytes or fewer.")
    return errors
