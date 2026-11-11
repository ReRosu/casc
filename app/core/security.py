import hashlib
import hmac

from app.core.config import get_settings


def callback_signature(provider: str, attempt_id: str) -> str:
    """HMAC для callback URL. PayMaster не подписывает уведомления, поэтому подпись мы
    кладём в сам URL, который передаём провайдеру: подделать вебхук без секрета нельзя."""
    secret = get_settings().webhook_secret.get_secret_value().encode()
    return hmac.new(secret, f"{provider}:{attempt_id}".encode(), hashlib.sha256).hexdigest()[:32]


def verify_callback_signature(provider: str, attempt_id: str, signature: str) -> bool:
    return hmac.compare_digest(callback_signature(provider, attempt_id).encode(),
                               signature.encode())
