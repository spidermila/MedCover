from __future__ import annotations

from flask import Flask
from flask import redirect
from flask import Response
from flask import url_for

from .admin import admin_bp
from .admin_digest import bp as admin_digest_bp
from .app_settings import app_settings_bp
from .assignments import assignments_bp
from .auth import auth_bp
from .backup import backup_bp
from .calendar import calendar_bp
from .debriefing import debriefing_bp
from .equipment import equipment_bp
from .events import events_bp
from .feedback import feedback_bp
from .import_events import import_bp
from .main import main_bp
from .master_events import master_events_bp
from .notifications import notifications_bp
from .qualifications import qualifications_bp
from .reports import reports_bp
from .setup import setup_bp
from .templates import templates_bp
from .users import users_bp
from .work_report import work_report_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(setup_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(events_bp)
    app.register_blueprint(master_events_bp)
    app.register_blueprint(templates_bp)
    app.register_blueprint(qualifications_bp)
    app.register_blueprint(assignments_bp)
    app.register_blueprint(debriefing_bp)
    app.register_blueprint(equipment_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(app_settings_bp)
    app.register_blueprint(import_bp)
    app.register_blueprint(feedback_bp)
    app.register_blueprint(admin_digest_bp)
    app.register_blueprint(backup_bp)
    app.register_blueprint(work_report_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(calendar_bp)

    if app.config.get("DEV_LOGIN_ENABLED"):
        from .dev import dev_bp
        app.register_blueprint(dev_bp)

    @app.route("/")
    def index() -> Response:
        return redirect(url_for("auth.login"))
