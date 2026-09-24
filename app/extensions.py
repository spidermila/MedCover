import uuid
from typing import TYPE_CHECKING

from flask import current_app, session
from flask_login import LoginManager, user_logged_in
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
    # Import after extension construction: UserAccount imports this module's db object.
    from app.models.user import UserAccount  # pylint: disable=import-outside-toplevel

    try:
        user = db.session.get(UserAccount, uuid.UUID(user_id))
    except ValueError, AttributeError:
        return None
    # A back-channel logout moves the epoch on, ending sessions from before it.
    if user is None or user.session_epoch != session.get("session_epoch", 0):
        return None
    # A switch of AUTH_MODE ends the sessions started under the other mode
    # (sessions from before this check are local ones).
    if session.get("auth_mode", "local") != current_app.config["AUTH_MODE"]:
        return None
    return user


@user_logged_in.connect
def _remember_session_epoch(sender: object, user: UserAccount, **extra: object) -> None:
    """Every login path stores the epoch and login mode that ``load_user`` checks."""
    session["session_epoch"] = user.session_epoch
    session["auth_mode"] = current_app.config["AUTH_MODE"]
