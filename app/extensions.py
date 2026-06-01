from __future__ import annotations

from typing import TYPE_CHECKING

from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

if TYPE_CHECKING:
    from app.models.user import UserAccount

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
mail = Mail()
csrf = CSRFProtect()

login_manager.login_view = "auth.login"
login_manager.login_message = "Pro přístup na tuto stránku se prosím přihlaste."
login_manager.login_message_category = "warning"


@login_manager.user_loader
def load_user(user_id: str) -> UserAccount | None:
    import uuid  # pylint: disable=import-outside-toplevel

    from app.models.user import UserAccount  # pylint: disable=import-outside-toplevel

    try:
        return db.session.get(UserAccount, uuid.UUID(user_id))
    except ValueError, AttributeError:
        return None
