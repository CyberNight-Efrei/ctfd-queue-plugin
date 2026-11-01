"""CTFd Queue Challenge plugin entry point."""
import logging
import os
import threading
import time

from flask import Blueprint, abort, render_template
from sqlalchemy import func

from CTFd.api import CTFd_API_v1
from CTFd.plugins import (
    register_admin_plugin_menu_bar,
    register_plugin_assets_directory,
    register_plugin_script,
)
from CTFd.plugins.challenges import CHALLENGE_CLASSES, CTFdStandardChallenge
from CTFd.utils.decorators import admins_only
from CTFd.utils.user import get_locale

from .api import queue_namespace
from .models import QueueChallengeModel, QueueEntry, QueueNotification, db
from .queue_service import QueueError, QueueService
from .validation import normalize_queue_fields

log = logging.getLogger("ctfd.queue_challenge")
_scheduler_lock_handle = None


def _challenge_tag_values(challenge):
    """Return normalized display values for the tags assigned to a challenge."""
    values = []
    for tag in getattr(challenge, "tags", None) or []:
        # CTFd's Tags model stores the visible tag text in its value field.
        # Keep fallbacks for installations/plugins that expose strings
        # or custom tag objects instead.
        value = tag if isinstance(tag, str) else getattr(tag, "value", None)
        if value is None:
            value = getattr(tag, "tag", None)
        if value is None:
            value = getattr(tag, "name", None)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            values.append(value)
    return sorted(set(values), key=str.casefold)


class QueueChallenge(CTFdStandardChallenge):
    """Native CTFd challenge type with round-based queueing and native scoring."""

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
        "view": "/plugins/queue_challenge/assets/view.js?v=20261101-ui-polish-v1",
    }

    @classmethod
    def _normalise_queue_fields(cls, data, error_type, partial=False):
        """Keep CTFd's challenge API contract while delegating validation."""
        return normalize_queue_fields(data, error_type, partial=partial)

    @classmethod
    def create(cls, request):
        from types import SimpleNamespace
        from CTFd.exceptions.challenges import ChallengeCreateException

        try:
            data = dict(request.form or request.get_json() or {})
            data = cls._normalise_queue_fields(data, ChallengeCreateException)
            result = super().create(SimpleNamespace(form=data, get_json=lambda: data))
            log.info("Queue challenge created challenge_id=%s", getattr(result, "id", None))
            return result
        except ChallengeCreateException as exc:
            log.warning("Queue challenge creation rejected: %s", exc)
            raise
        except Exception:
            log.exception("Unexpected error creating Queue Challenge")
            raise

    @classmethod
    def update(cls, challenge, request):
        from types import SimpleNamespace
        from CTFd.exceptions.challenges import ChallengeUpdateException

        try:
            data = dict(request.form or request.get_json() or {})
            # Scheduler bookkeeping is server-owned and never accepted from an API client.
            data.pop("last_draw_at", None)
            data.pop("last_selection_at", None)
            data.pop("round_number", None)
            data = cls._normalise_queue_fields(
                data, ChallengeUpdateException, partial=True
            )
            result = super().update(
                challenge, SimpleNamespace(form=data, get_json=lambda: data)
            )
            log.info("Queue challenge updated challenge_id=%s", challenge.id)
            return result
        except ChallengeUpdateException as exc:
            log.warning(
                "Queue challenge update rejected challenge_id=%s: %s",
                challenge.id,
                exc,
            )
            raise
        except Exception:
            log.exception("Unexpected error updating Queue Challenge challenge_id=%s", challenge.id)
            raise

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
    def solve(cls, user, team, challenge, request):
        # Do not remove the selected participant immediately on solve. The
        # current selection remains visible for this round and is evicted by
        # QueueService._advance at the next queue iteration.
        result = super().solve(user, team, challenge, request)
        log.info(
            "CTFd recorded a solve for queue challenge_id=%s user_id=%s team_id=%s; "
            "queue state will advance on the next iteration",
            challenge.id,
            user.id,
            team.id if team else None,
        )
        return result

    @classmethod
    def delete(cls, challenge):
        challenge_id = challenge.id
        try:
            # Keep queue-entry cleanup in the same transaction as CTFd's
            # challenge deletion. The parent implementation commits the
            # complete challenge deletion after removing its native resources.
            QueueEntry.query.filter_by(challenge_id=challenge_id).delete(
                synchronize_session=False
            )
            result = super().delete(challenge)
            log.info("Queue challenge deleted challenge_id=%s", challenge_id)
            return result
        except Exception:
            db.session.rollback()
            log.exception("Failed to delete queue challenge challenge_id=%s", challenge_id)
            raise



def _automatic_worker(app):
    try:
        tick = int(app.config.get("QUEUE_CHALLENGE_TICK_SECONDS", 5))
    except (TypeError, ValueError):
        log.warning(
            "Invalid QUEUE_CHALLENGE_TICK_SECONDS value; falling back to 5 seconds"
        )
        tick = 5
    tick = max(1, tick)
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
            log.debug(
                "Queue Challenge scheduler is already owned by another worker on this host"
            )
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
    log.info("Initializing Queue Challenge plugin")
    # Importing the models before create_all registers the plugin tables with SQLAlchemy.
    try:
        with app.app_context():
            db.create_all()
            # Notifications are a user's durable history, not children of a queue
            # entry. Detach legacy references so later queue cleanup cannot cascade
            # delete notifications that their recipients may still want to revisit.
            detached_count = QueueNotification.query.filter(
                QueueNotification.queue_entry_id.isnot(None)
            ).update(
                {QueueNotification.queue_entry_id: None},
                synchronize_session=False,
            )
            if detached_count:
                db.session.commit()
                log.info(
                    "Detached %s queue notification record(s) from queue entries "
                    "to preserve notification history",
                    detached_count,
                )
    except Exception:
        log.exception("Failed to initialize Queue Challenge database tables")
        raise

    log.info("Queue Challenge database tables initialized")
    CHALLENGE_CLASSES[QueueChallenge.id] = QueueChallenge
    register_plugin_assets_directory(
        app, base_path="/plugins/queue_challenge/assets/"
    )
    register_plugin_script("/plugins/queue_challenge/assets/notifications.js?v=20261101-hardening-v2")
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

    @page_blueprint.route("/notifications")
    def notification_history():
        locale = str(get_locale() or "en").lower()
        page_title = "Notif. Fil" if locale.startswith("fr") else "Queue Notif."
        return render_template(
            "plugins/queue_challenge/templates/notifications.html",
            title=page_title,
            notification_page_title=page_title,
        )

    @page_blueprint.route("/admin")
    @admins_only
    def admin_index():
        challenges = (
            QueueChallengeModel.query.order_by(QueueChallengeModel.id.desc()).all()
        )
        queue_counts = (
            db.session.query(
                QueueEntry.challenge_id,
                QueueEntry.state,
                func.count(QueueEntry.id),
            )
            .filter(QueueEntry.state.in_(("waiting", "selected")))
            .group_by(QueueEntry.challenge_id, QueueEntry.state)
            .all()
        )
        counts_by_challenge = {
            (challenge_id, state): count
            for challenge_id, state, count in queue_counts
        }

        overview = []
        available_tags = {}
        for challenge in challenges:
            challenge_tags = _challenge_tag_values(challenge)
            for tag in challenge_tags:
                available_tags.setdefault(tag.casefold(), tag)

            overview.append(
                {
                    "challenge": challenge,
                    "tags": challenge_tags,
                    "waiting_count": counts_by_challenge.get(
                        (challenge.id, "waiting"), 0
                    ),
                    "selected_count": counts_by_challenge.get(
                        (challenge.id, "selected"), 0
                    ),
                }
            )
        return render_template(
            "plugins/queue_challenge/templates/admin.html",
            queue_challenges=overview,
            queue_tags=sorted(available_tags.values(), key=str.casefold),
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
            challenge_tags=_challenge_tag_values(challenge),
            state=state,
        )

    app.register_blueprint(page_blueprint)

    _start_scheduler_once(app)
