"""CTFd Queue Challenge plugin entry point."""
import logging
import os
import threading
import time

from flask import Blueprint, abort, render_template, request

from CTFd.api import CTFd_API_v1
from CTFd.plugins import (
    register_admin_plugin_menu_bar,
    register_plugin_assets_directory,
    register_plugin_script,
)
from CTFd.plugins.challenges import CHALLENGE_CLASSES, CTFdStandardChallenge
from CTFd.utils.decorators import admins_only
from CTFd.utils.user import get_current_team, get_current_user, is_admin

from .api import queue_namespace
from .models import QueueChallengeModel, QueueEntry, db
from .queue_service import QueueError, QueueService

log = logging.getLogger("ctfd.queue_challenge")
_scheduler_lock_handle = None


class QueueChallenge(CTFdStandardChallenge):
    """Native CTFd challenge type with queue-gated attempts and native scoring."""

    id = "queue"
    name = "Queue Challenge"
    challenge_model = QueueChallengeModel
    route = "/plugins/queue_challenge/assets/"
    templates = {
        "create": "/plugins/queue_challenge/assets/create.html",
        "update": "/plugins/queue_challenge/assets/update.html",
        "view": "/plugins/queue_challenge/assets/view.html",
    }
    scripts = {
        "create": "/plugins/queue_challenge/assets/create.js",
        "update": "/plugins/queue_challenge/assets/update.js",
        "view": "/plugins/queue_challenge/assets/view.js",
    }

    @classmethod
    def _normalise_queue_fields(cls, data, error_type, partial=False):
        result = dict(data)
        allowed_modes = {"manual", "automatic"}

        if not partial or "queue_mode" in result:
            mode = str(result.get("queue_mode", "manual")).strip().lower()
            if mode not in allowed_modes:
                raise error_type("Queue mode must be manual or automatic.")
            result["queue_mode"] = mode

        if not partial or "function" in result:
            scoring_function = str(result.get("function", "static")).strip().lower()
            if scoring_function not in ("static", "linear", "logarithmic"):
                raise error_type(
                    "Scoring function must be static, linear or logarithmic."
                )
            result["function"] = scoring_function

        for key, default, minimum, maximum in (
            ("batch_size", 1, 1, 1000),
            ("interval_seconds", 900, 1, 86400),
        ):
            if not partial or key in result:
                try:
                    value = int(result.get(key, default))
                except (TypeError, ValueError) as exc:
                    raise error_type("{} must be an integer.".format(key)) from exc
                if not minimum <= value <= maximum:
                    raise error_type(
                        "{} must be between {} and {}.".format(key, minimum, maximum)
                    )
                result[key] = value

        for key, default in (("show_position", True), ("show_eta", True)):
            if not partial or key in result:
                value = result.get(key, default)
                if isinstance(value, bool):
                    result[key] = value
                else:
                    result[key] = str(value).strip().lower() in (
                        "1", "true", "yes", "on"
                    )

        if not partial or "selected_message" in result:
            message = str(
                result.get(
                    "selected_message",
                    "You have been selected! You may now attempt this challenge.",
                )
                or ""
            )
            if len(message) > 4000:
                raise error_type("The selected-participant message must be 4000 characters or fewer.")
            result["selected_message"] = message

        return result

    @classmethod
    def create(cls, request):
        from types import SimpleNamespace
        from CTFd.exceptions.challenges import ChallengeCreateException

        data = dict(request.form or request.get_json() or {})
        data = cls._normalise_queue_fields(data, ChallengeCreateException)
        return super().create(SimpleNamespace(form=data, get_json=lambda: data))

    @classmethod
    def update(cls, challenge, request):
        from types import SimpleNamespace
        from CTFd.exceptions.challenges import ChallengeUpdateException

        data = dict(request.form or request.get_json() or {})
        # Scheduler bookkeeping is server-owned and never accepted from an API client.
        data.pop("last_draw_at", None)
        data.pop("last_selection_at", None)
        data.pop("round_number", None)
        data = cls._normalise_queue_fields(
            data, ChallengeUpdateException, partial=True
        )
        return super().update(
            challenge, SimpleNamespace(form=data, get_json=lambda: data)
        )

    @classmethod
    def read(cls, challenge):
        data = super().read(challenge)
        data.update(
            {
                "queue_mode": challenge.queue_mode or "manual",
                "batch_size": challenge.batch_size or 1,
                "interval_seconds": challenge.interval_seconds or 900,
                "show_position": bool(challenge.show_position),
                "show_eta": bool(challenge.show_eta),
                "selected_message": challenge.selected_message or "",
            }
        )
        return data

    @classmethod
    def attempt(cls, challenge, request):
        from CTFd.utils.user import get_current_team, get_current_user, is_admin

        user = get_current_user()
        if user is None:
            return False, "Sign in and join the queue before submitting a flag."
        if not is_admin():
            entry = QueueEntry.query.filter_by(
                challenge_id=challenge.id,
                user_id=user.id,
                state="selected",
            ).first()
            if entry is None:
                abort(403, description="You must be selected from the queue before attempting this challenge.")
            if entry.account_key.startswith("team:"):
                team = get_current_team()
                if team is None or entry.team_id != team.id:
                    abort(403, description="The selected queue entry does not match your current team.")
        return super().attempt(challenge, request)

    @classmethod
    def solve(cls, user, team, challenge, request):
        result = super().solve(user, team, challenge, request)
        QueueService.mark_completed(
            challenge_id=challenge.id,
            user_id=user.id,
            team_id=team.id if team else None,
        )
        return result

    @classmethod
    def delete(cls, challenge):
        QueueEntry.query.filter_by(challenge_id=challenge.id).delete(
            synchronize_session=False
        )
        db.session.commit()
        return super().delete(challenge)


def get_config_user_mode():
    from CTFd.utils import get_config

    return get_config("user_mode", "users")


def _automatic_worker(app):
    tick = max(1, int(app.config.get("QUEUE_CHALLENGE_TICK_SECONDS", 5)))
    while True:
        time.sleep(tick)
        try:
            with app.app_context():
                QueueService.process_due_challenges()
        except Exception:
            log.exception("Queue Challenge automatic scheduler iteration failed")


def _start_scheduler_once(app):
    """Run one scheduler process per host/container using an advisory file lock."""
    global _scheduler_lock_handle
    try:
        import fcntl

        handle = open(
            os.path.join("/tmp", "ctfd_queue_challenge_scheduler.lock"), "w"
        )
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            return
        _scheduler_lock_handle = handle
        threading.Thread(
            target=_automatic_worker,
            args=(app,),
            name="ctfd-queue-challenge-scheduler",
            daemon=True,
        ).start()
        log.info("Queue Challenge automatic scheduler started")
    except Exception:
        log.exception("Queue Challenge automatic scheduler could not start")


def load(app):
    # Importing the models before create_all registers the plugin tables with SQLAlchemy.
    with app.app_context():
        db.create_all()

    CHALLENGE_CLASSES[QueueChallenge.id] = QueueChallenge
    register_plugin_assets_directory(
        app, base_path="/plugins/queue_challenge/assets/"
    )
    register_plugin_script("plugins/queue_challenge/assets/notifications.js")
    register_admin_plugin_menu_bar(
        title="Queues", route="/plugins/ctfd-queue/admin"
    )
    CTFd_API_v1.add_namespace(queue_namespace, path="/plugins/ctfd-queue")

    page_blueprint = Blueprint(
        "queue_challenge",
        __name__,
        template_folder="templates",
        url_prefix="/plugins/ctfd-queue",
    )

    @page_blueprint.route("/admin")
    @admins_only
    def admin_index():
        challenges = (
            QueueChallengeModel.query.order_by(QueueChallengeModel.id.desc()).all()
        )
        overview = []
        for challenge in challenges:
            overview.append(
                {
                    "challenge": challenge,
                    "waiting_count": QueueEntry.query.filter_by(
                        challenge_id=challenge.id, state="waiting"
                    ).count(),
                    "selected_count": QueueEntry.query.filter_by(
                        challenge_id=challenge.id, state="selected"
                    ).count(),
                }
            )
        return render_template(
            "plugins/queue_challenge/templates/admin.html",
            queue_challenges=overview,
        )

    @page_blueprint.route("/admin/<int:challenge_id>")
    @admins_only
    def admin_challenge(challenge_id):
        try:
            challenge = QueueService.get_challenge(challenge_id)
            state = QueueService.admin_state(challenge.id)
        except QueueError:
            abort(404)
        return render_template(
            "plugins/queue_challenge/templates/admin_detail.html",
            challenge=challenge,
            state=state,
        )

    app.register_blueprint(page_blueprint)

    @app.before_request
    def enforce_queue_selection_before_ctfd_attempt_limits():
        # CTFd increments anti-bruteforce and max-attempt counters before calling
        # a challenge's attempt() method. Check selection first so a teammate
        # cannot exhaust a selected player's attempt budget with forged requests.
        if (
            request.method != "POST"
            or not request.path.endswith("/api/v1/challenges/attempt")
        ):
            return None
        data = request.get_json(silent=True) if request.is_json else request.form
        try:
            challenge_id = int((data or {}).get("challenge_id"))
        except (TypeError, ValueError):
            return None
        challenge = QueueChallengeModel.query.filter_by(id=challenge_id).first()
        if challenge is None:
            return None
        if is_admin():
            # Preserve CTFd's existing administrator testing behavior.
            return None
        user = get_current_user()
        if user is None:
            return None
        entry = QueueEntry.query.filter_by(
            challenge_id=challenge.id, user_id=user.id, state="selected"
        ).first()
        if entry is None:
            abort(403, description="You must be selected from the queue before attempting this challenge.")
        if entry.account_key.startswith("team:"):
            team = get_current_team()
            if team is None or entry.team_id != team.id:
                abort(403, description="The selected queue entry does not match your current team.")
        return None

    _start_scheduler_once(app)
