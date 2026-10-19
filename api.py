"""REST API endpoints for queue participants and administrators."""
import logging
from datetime import datetime

from flask import request
from flask_restx import Namespace, Resource, abort

from CTFd.models import Challenges, Flags, Solves, Users
from CTFd.utils.decorators import admins_only, authed_only, during_ctf_time_only
from CTFd.utils.modules import can_access_challenge
from CTFd.utils.user import get_current_team, get_current_user, is_admin

from .models import QueueEntry, QueueNotification, db
from .queue_service import QueueError, QueueService

log = logging.getLogger("ctfd.queue_challenge.api")

queue_namespace = Namespace(
    "ctfd-queue",
    description="Queue challenge participation and administration",
)


def _current_user_id():
    """Return the current user ID for logs without exposing session data."""
    try:
        user = get_current_user()
        return getattr(user, "id", None) if user is not None else None
    except Exception:
        return None


def _challenge_for_user(challenge_id):
    try:
        challenge = QueueService.get_challenge(challenge_id)
    except QueueError:
        # Deliberately keep unavailable and unauthorized challenges indistinguishable.
        log.debug(
            "Queue challenge lookup rejected challenge_id=%s user_id=%s",
            challenge_id,
            _current_user_id(),
        )
        abort(404, "Queue challenge not found.")
    except Exception:
        log.exception(
            "Unexpected queue challenge lookup failure challenge_id=%s user_id=%s",
            challenge_id,
            _current_user_id(),
        )
        abort(500, "The queue request failed unexpectedly. Check CTFd server logs.")

    if not is_admin():
        user = get_current_user()
        user_id = getattr(user, "id", None)
        try:
            can_access = (
                challenge.state == "visible"
                and can_access_challenge(challenge, user)
            )
        except Exception:
            log.exception(
                "Failed to evaluate queue challenge access challenge_id=%s user_id=%s",
                challenge_id,
                user_id,
            )
            abort(500, "The queue request failed unexpectedly. Check CTFd server logs.")

        if not can_access:
            log.debug(
                "Queue challenge access denied challenge_id=%s user_id=%s",
                challenge_id,
                user_id,
            )
            abort(404, "Queue challenge not found.")

        if challenge.requirements:
            try:
                requirements = challenge.requirements.get("prerequisites", [])
                solve_rows = (
                    Solves.query.with_entities(Solves.challenge_id)
                    .filter_by(account_id=user.account_id)
                    .all()
                )
                solved_ids = {required_id for (required_id,) in solve_rows}
                all_ids = {
                    row.id
                    for row in Challenges.query.with_entities(Challenges.id).all()
                }
                prerequisites = set(requirements).intersection(all_ids)
                prerequisites_satisfied = solved_ids >= prerequisites
            except Exception:
                log.exception(
                    "Failed to evaluate queue challenge prerequisites "
                    "challenge_id=%s user_id=%s",
                    challenge_id,
                    user_id,
                )
                abort(500, "The queue request failed unexpectedly. Check CTFd server logs.")

            if not prerequisites_satisfied:
                log.debug(
                    "Queue challenge prerequisite check failed challenge_id=%s user_id=%s",
                    challenge_id,
                    user_id,
                )
                abort(404, "Queue challenge not found.")
    return challenge


def _run_queue_operation(callback, operation, challenge_id=None, entry_id=None):
    """Convert expected errors to client responses and log unexpected failures."""
    context = {
        "operation": operation,
        "challenge_id": challenge_id,
        "entry_id": entry_id,
        "user_id": _current_user_id(),
    }
    try:
        return {"success": True, "data": callback()}
    except QueueError as exc:
        # These represent expected business-rule rejections (e.g. not in queue).
        log.info(
            "Queue operation rejected operation=%s challenge_id=%s entry_id=%s "
            "user_id=%s reason=%s",
            context["operation"],
            context["challenge_id"],
            context["entry_id"],
            context["user_id"],
            str(exc),
        )
        abort(400, str(exc))
    except ValueError as exc:
        log.warning(
            "Invalid queue operation input operation=%s challenge_id=%s "
            "entry_id=%s user_id=%s reason=%s",
            context["operation"],
            context["challenge_id"],
            context["entry_id"],
            context["user_id"],
            str(exc),
        )
        abort(400, str(exc))
    except Exception:
        log.exception(
            "Unexpected queue API failure operation=%s challenge_id=%s "
            "entry_id=%s user_id=%s",
            context["operation"],
            context["challenge_id"],
            context["entry_id"],
            context["user_id"],
        )
        abort(500, "The queue request failed unexpectedly. Check CTFd server logs.")


def _admin_challenge(challenge_id):
    try:
        return QueueService.get_challenge(challenge_id)
    except QueueError:
        log.debug("Admin queue challenge not found challenge_id=%s", challenge_id)
        abort(404, "Queue challenge not found.")
    except Exception:
        log.exception("Admin queue challenge lookup failed challenge_id=%s", challenge_id)
        abort(500, "The queue request failed unexpectedly. Check CTFd server logs.")


@queue_namespace.route("/challenges/<int:challenge_id>/status")
class UserQueueStatus(Resource):
    @authed_only
    def get(self, challenge_id):
        challenge = _challenge_for_user(challenge_id)
        return _run_queue_operation(
            lambda: QueueService.user_status(
                challenge.id, get_current_user(), get_current_team()
            ),
            operation="user_status",
            challenge_id=challenge.id,
        )


@queue_namespace.route("/challenges/<int:challenge_id>/join")
class UserQueueJoin(Resource):
    @authed_only
    @during_ctf_time_only
    def post(self, challenge_id):
        challenge = _challenge_for_user(challenge_id)
        return _run_queue_operation(
            lambda: QueueService.join(
                challenge.id, get_current_user(), get_current_team()
            ),
            operation="join",
            challenge_id=challenge.id,
        )


@queue_namespace.route("/challenges/<int:challenge_id>/leave")
class UserQueueLeave(Resource):
    @authed_only
    @during_ctf_time_only
    def post(self, challenge_id):
        challenge = _challenge_for_user(challenge_id)
        return _run_queue_operation(
            lambda: QueueService.leave(
                challenge.id, get_current_user(), get_current_team()
            ),
            operation="leave",
            challenge_id=challenge.id,
        )


@queue_namespace.route("/notifications")
class UserQueueNotifications(Resource):
    @authed_only
    def get(self):
        user = get_current_user()
        pending_query = QueueNotification.query.filter_by(
            recipient_user_id=user.id,
            acknowledged_at=None,
        )
        pending_count = pending_query.count()
        notifications = (
            pending_query
            .order_by(QueueNotification.id.asc())
            .limit(50)
            .all()
        )
        return {
            "success": True,
            "data": {
                "pending_count": pending_count,
                "notifications": [
                    {
                        "id": notification.id,
                        "title": notification.title,
                        "content": notification.content,
                        "date": (
                            notification.created_at.isoformat()
                            if notification.created_at
                            else None
                        ),
                    }
                    for notification in notifications
                ],
            },
        }


@queue_namespace.route("/notifications/history")
class UserQueueNotificationHistory(Resource):
    """Return the signed-in user's private notification history, newest first."""

    @authed_only
    def get(self):
        user = get_current_user()
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = min(100, max(1, int(request.args.get("limit", 50))))
        except (TypeError, ValueError):
            abort(400, "Notification history pagination must use integer values.")

        query = QueueNotification.query.filter_by(recipient_user_id=user.id)
        total = query.count()
        pending_count = query.filter_by(acknowledged_at=None).count()
        notifications = (
            query.order_by(
                QueueNotification.created_at.desc(),
                QueueNotification.id.desc(),
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {
            "success": True,
            "data": {
                "total": total,
                "offset": offset,
                "limit": limit,
                "pending_count": pending_count,
                "notifications": [
                    {
                        "id": notification.id,
                        "title": notification.title,
                        "content": notification.content,
                        "date": (
                            notification.created_at.isoformat()
                            if notification.created_at
                            else None
                        ),
                        "acknowledged": notification.acknowledged_at is not None,
                    }
                    for notification in notifications
                ],
            },
        }


@queue_namespace.route("/notifications/<int:notification_id>/ack")
class UserQueueNotificationAcknowledge(Resource):
    @authed_only
    def post(self, notification_id):
        user = get_current_user()
        notification = QueueNotification.query.filter_by(
            id=notification_id,
            recipient_user_id=user.id,
        ).first()
        if notification is None:
            abort(404, "Queue notification not found.")

        if notification.acknowledged_at is None:
            notification.acknowledged_at = datetime.utcnow()
            db.session.commit()

        return {"success": True, "data": {"id": notification.id}}


@queue_namespace.route("/admin/challenges/<int:challenge_id>/state")
class AdminQueueState(Resource):
    @admins_only
    def get(self, challenge_id):
        challenge = _admin_challenge(challenge_id)
        return _run_queue_operation(
            lambda: QueueService.admin_state(challenge.id),
            operation="admin_state",
            challenge_id=challenge.id,
        )


@queue_namespace.route("/admin/challenges/<int:challenge_id>/advance")
class AdminQueueAdvance(Resource):
    @admins_only
    def post(self, challenge_id):
        challenge = _admin_challenge(challenge_id)
        return _run_queue_operation(
            lambda: {
                **QueueService.advance(challenge.id),
                "state": QueueService.admin_state(challenge.id),
            },
            operation="admin_advance",
            challenge_id=challenge.id,
        )




@queue_namespace.route("/admin/users/suggest")
class AdminQueueUserSuggestions(Resource):
    @admins_only
    def get(self):
        query = str(request.args.get("q") or "").strip()
        if not query:
            return {"success": True, "data": {"users": []}}

        # Return only a small prefix-matched result set for the administrator's
        # autocomplete field; do not expose a full user directory to the page.
        users = (
            Users.query.filter(Users.name.ilike(query + "%"))
            .order_by(Users.name.asc())
            .limit(15)
            .all()
        )
        return {
            "success": True,
            "data": {
                "users": [
                    {"id": user.id, "username": user.name}
                    for user in users
                ]
            },
        }


@queue_namespace.route("/admin/challenges/<int:challenge_id>/flag")
class AdminQueueCopyFlag(Resource):
    @admins_only
    def get(self, challenge_id):
        challenge = _admin_challenge(challenge_id)
        flags = (
            Flags.query.filter_by(challenge_id=challenge.id)
            .order_by(Flags.id.asc())
            .all()
        )
        if not flags:
            abort(404, "No flag is configured for this challenge.")

        # Prefer a literal static flag if the challenge accepts multiple flag
        # types. The content is returned only to this admin-only copy action;
        # the list page never renders it.
        flag = next(
            (
                item for item in flags
                if getattr(item, "type", None) == "static" and getattr(item, "content", None)
            ),
            None,
        )
        if flag is None:
            abort(400, "No static flag is configured for this challenge.")
        return {
            "success": True,
            "data": {
                "content": flag.content,
                "type": flag.type,
                "flag_count": len(flags),
            },
        }


@queue_namespace.route("/admin/entries/<int:entry_id>/notify-flag")
class AdminQueueNotifyFlag(Resource):
    @admins_only
    def post(self, entry_id):
        entry = QueueEntry.query.filter_by(id=entry_id).first()
        if entry is None:
            abort(404, "Queue entry not found.")
        challenge = _admin_challenge(entry.challenge_id)
        flags = (
            Flags.query.filter_by(challenge_id=challenge.id)
            .order_by(Flags.id.asc())
            .all()
        )
        flag = next(
            (
                item for item in flags
                if getattr(item, "type", None) == "static" and getattr(item, "content", None)
            ),
            None,
        )
        if flag is None:
            abort(400, "No static flag is configured for this challenge.")

        return _run_queue_operation(
            lambda: QueueService.notify_flag_to_selected_team(entry_id, flag.content),
            operation="admin_notify_flag",
            challenge_id=challenge.id,
            entry_id=entry_id,
        )


@queue_namespace.route("/admin/challenges/<int:challenge_id>/entries")
class AdminQueueAdd(Resource):
    @admins_only
    def post(self, challenge_id):
        challenge = _admin_challenge(challenge_id)
        data = request.get_json(silent=True) or {}
        username = str(data.get("username") or "").strip()
        if username:
            user = Users.query.filter_by(name=username).first()
            if user is None:
                log.info(
                    "Admin queue add requested unknown username challenge_id=%s username=%s admin_user_id=%s",
                    challenge_id,
                    username,
                    _current_user_id(),
                )
                abort(400, "No CTFd user found with that username.")
            user_id = user.id
        else:
            try:
                user_id = int(data.get("user_id"))
                if user_id < 1:
                    raise ValueError("user_id must be positive")
            except (TypeError, ValueError):
                log.warning(
                    "Invalid admin queue add request challenge_id=%s admin_user_id=%s",
                    challenge_id,
                    _current_user_id(),
                )
                abort(400, "A valid username or user_id is required.")
        return _run_queue_operation(
            lambda: QueueService.admin_add(challenge.id, user_id),
            operation="admin_add",
            challenge_id=challenge.id,
        )


@queue_namespace.route("/admin/entries/<int:entry_id>/move")
class AdminQueueMove(Resource):
    @admins_only
    def post(self, entry_id):
        data = request.get_json(silent=True) or {}
        direction = str(data.get("direction", "")).lower()
        return _run_queue_operation(
            lambda: QueueService.move_entry(entry_id, direction),
            operation="admin_move_entry",
            entry_id=entry_id,
        )


@queue_namespace.route("/admin/entries/<int:entry_id>")
class AdminQueueRemove(Resource):
    @admins_only
    def delete(self, entry_id):
        return _run_queue_operation(
            lambda: QueueService.remove_entry(entry_id),
            operation="admin_remove_entry",
            entry_id=entry_id,
        )
