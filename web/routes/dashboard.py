from flask import Blueprint, redirect, render_template, url_for
from flask_login import current_user
from config.settings import POLL_INTERVAL

bp = Blueprint("dashboard", __name__)


@bp.route('/')
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    if not current_user.is_verified:
        return render_template("pending_verification.html", load_dashboard_scripts=False)
    username = current_user.username if current_user.is_authenticated else "میهمان"
    return render_template("dashboard.html", poll_interval=POLL_INTERVAL, username=username)
