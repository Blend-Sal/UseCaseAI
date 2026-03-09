import os
import base64
import hashlib
import hmac

ITERATIONS = 120_000
SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = os.urandom(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (
        ITERATIONS,
        base64.b64encode(salt).decode("utf-8"),
        base64.b64encode(dk).decode("utf-8"),
    )

def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iter_s, salt_b64, dk_b64 = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iter_s)
        salt = base64.b64decode(salt_b64.encode("utf-8"))
        expected = base64.b64decode(dk_b64.encode("utf-8"))
        test = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
        return hmac.compare_digest(test, expected)
    except Exception:
        return False