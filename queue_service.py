"""Queue business logic, including automatic rounds and CTFd notifications."""
import logging
import math
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from CTFd.models import Notifications, Users, db
from CTFd.utils import get_config
from CTFd.utils.user import get_current_team, get_current_user

from .models import QueueChallengeModel, QueueEntry

log = logging.getLogger("ctfd.queue_challenge")

class QueueError(Exception):
    """An expected queue operation error suitable for returning to a client."""


def _is_team_mode():
    return get_config("user_mode", "users") == "teams"


def _utcnow():
    return datetime.utcnow()


def _publish_notifications(payloads):
    """Publish stored notifications to CTFd's live event stream when available."""
    try:
        from flask import current_app

        manager = getattr(current_app, "events_manager", None)
        if manager is None:
            return
        for payload in payloads:
            manager.publish(data=payload, type="notification")
    except Exception:
        # Persistence is the source of truth; a live-event failure must not undo
        # a draw and should be visible in logs.
        log.exception("Could not publish a queue notification event")


class QueueService:
    @staticmethod
    def account_key(user, team=None):
        team = team or getattr(user, "team", None)
        # If a team relationship exists, keep the one-entry-per-team invariant
        # even on installations configured for individual scoring.
        if team is not None:
            return "team:{}".format(team.id)
        if _is_team_mode():
            raise QueueError("You must join a team before entering this queue.")
        return "user:{}".format(user.id)

    @staticmethod
    def get_challenge(challenge_id, lock=False):
        query = QueueChallengeModel.query.filter_by(id=challenge_id)
        if lock:
            query = query.with_for_update()
        challenge = query.first()
        if challenge is None:
            raise QueueError("Queue challenge not found.")
        return challenge

    @staticmethod
    def waiting_entries(challenge_id):
        return (
            QueueEntry.query.filter_by(challenge_id=challenge_id, state="waiting")
            .order_by(QueueEntry.position.asc(), QueueEntry.id.asc())
            .all()
        )

    @staticmethod
    def selected_entries(challenge_id):
        return (
            QueueEntry.query.filter_by(challenge_id=challenge_id, state="selected")
            .order_by(QueueEntry.round_number.desc(), QueueEntry.selected_at.asc())
            .all()
        )

    @staticmethod
    def _resequence(challenge_id):
        for index, entry in enumerate(QueueService.waiting_entries(challenge_id), 1):
            entry.position = index

    @staticmethod
    def _notification_for(entry, title, content):
        """Create a native CTFd notification and return its live-event payload."""
        # Notify the specific registered participant, not every teammate. In
        # team mode the queue slot is team-scoped but the selected player is not.
        notification = Notifications(
            title=title, content=content, user_id=entry.user_id
        )
        db.session.add(notification)
        db.session.flush()

        # CTFd 3.x uses Marshmallow 2 (result.data); tolerate the dict-returning
        # API used by newer Marshmallow versions as well.
        try:
            from CTFd.schemas.notifications import NotificationSchema

            dumped = NotificationSchema().dump(notification)
            payload = dumped.data if hasattr(dumped, "data") else dumped
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        payload.update(
            {
                "id": notification.id,
                "title": title,
                "content": content,
                "date": (
                    notification.date.isoformat()
                    if notification.date
                    else _utcnow().isoformat()
                ),
                "user_id": notification.user_id,
                "team_id": notification.team_id,
                "type": "alert",
                "sound": True,
            }
        )
        return payload

    @staticmethod
    def _notify_next(challenge_id, events):
        head = (
            QueueEntry.query.filter_by(challenge_id=challenge_id, state="waiting")
            .order_by(QueueEntry.position.asc(), QueueEntry.id.asc())
            .first()
        )
        if head is None or head.next_notified_at is not None:
            return
        head.next_notified_at = _utcnow()
        events.append(
            QueueService._notification_for(
                head,
                "You are next in the queue",
                "You are currently first in the queue. Please be ready for the next draw.",
            )
        )

    @staticmethod
    def _serialize_entry(entry, position=None):
        return {
            "id": entry.id,
            "user_id": entry.user_id,
            "username": getattr(entry.user, "name", None) or "User {}".format(entry.user_id),
            "team_id": entry.team_id,
            "team_name": getattr(entry.team, "name", None) if entry.team else None,
            "state": entry.state,
            "position": position if position is not None else entry.position,
            "round_number": entry.round_number,
            "joined_at": entry.joined_at.isoformat() if entry.joined_at else None,
            "selected_at": entry.selected_at.isoformat() if entry.selected_at else None,
            "completed_at": entry.completed_at.isoformat() if entry.completed_at else None,
        }

    @staticmethod
    def _estimate_wait_seconds(challenge, position, now=None):
        if position <= 0:
            return 0
        now = now or _utcnow()
        interval = max(1, int(challenge.interval_seconds or 1))
        batch_size = max(1, int(challenge.batch_size or 1))
        last_draw = challenge.last_draw_at or now
        elapsed = max(0, int((now - last_draw).total_seconds()))
        if challenge.queue_mode == "automatic":
            until_next = max(0, interval - elapsed)
        else:
            # Manual draws depend on an administrator, so treat one configured
            # interval as the estimated time to the next opportunity.
            until_next = interval
        rounds_ahead = int(math.ceil(float(position) / batch_size))
        return until_next + max(0, rounds_ahead - 1) * interval

    @staticmethod
    def user_status(challenge_id, user=None, team=None):
        user = user or get_current_user()
        if user is None:
            raise QueueError("Authentication required.")
        challenge = QueueService.get_challenge(challenge_id)
        try:
            account_key = QueueService.account_key(user, team)
        except QueueError:
            return {
                "state": "needs_team",
                "can_join": False,
                "can_leave": False,
                "can_submit": False,
                "queue_mode": challenge.queue_mode,
                "batch_size": challenge.batch_size,
                "interval_seconds": challenge.interval_seconds,
                "show_position": bool(challenge.show_position),
                "show_eta": bool(challenge.show_eta),
                "selected_message": challenge.selected_message or "",
            }
        row = QueueEntry.query.filter_by(
            challenge_id=challenge.id, account_key=account_key
        ).first()

        if row is None or row.state == "left":
            return {
                "state": "not_joined",
                "can_join": True,
                "can_leave": False,
                "can_submit": False,
                "queue_mode": challenge.queue_mode,
                "batch_size": challenge.batch_size,
                "interval_seconds": challenge.interval_seconds,
                "show_position": bool(challenge.show_position),
                "show_eta": bool(challenge.show_eta),
                "selected_message": challenge.selected_message or "",
            }

        if row.state != "completed" and row.account_key.startswith("team:") and row.user_id != user.id:
            return {
                "state": "registered_by_teammate",
                "can_join": False,
                "can_leave": False,
                "can_submit": False,
                "username": getattr(row.user, "name", None),
                "queue_mode": challenge.queue_mode,
                "show_position": bool(challenge.show_position),
                "show_eta": bool(challenge.show_eta),
                "selected_message": challenge.selected_message or "",
            }

        base = {
            "queue_mode": challenge.queue_mode,
            "batch_size": challenge.batch_size,
            "interval_seconds": challenge.interval_seconds,
            "show_position": bool(challenge.show_position),
            "show_eta": bool(challenge.show_eta),
            "selected_message": challenge.selected_message or "",
        }
        if row.state == "waiting":
            waiters = QueueService.waiting_entries(challenge.id)
            position = next(
                (i for i, item in enumerate(waiters, 1) if item.id == row.id), None
            )
            base.update(
                {
                    "state": "waiting",
                    "can_join": False,
                    "can_leave": True,
                    "can_submit": False,
                    "position": position,
                    "estimated_wait_seconds": (
                        QueueService._estimate_wait_seconds(challenge, position)
                        if position
                        else None
                    ),
                }
            )
        elif row.state == "selected":
            base.update(
                {
                    "state": "selected",
                    "can_join": False,
                    "can_leave": False,
                    "can_submit": True,
                    "position": None,
                    "round_number": row.round_number,
                }
            )
        elif row.state == "completed":
            base.update(
                {
                    "state": "completed",
                    "can_join": False,
                    "can_leave": False,
                    "can_submit": False,
                }
            )
        else:
            base.update(
                {
                    "state": "not_joined",
                    "can_join": True,
                    "can_leave": False,
                    "can_submit": False,
                }
            )
        return base

    @staticmethod
    def join(challenge_id, user=None, team=None):
        user = user or get_current_user()
        team = team if team is not None else get_current_team()
        if user is None:
            raise QueueError("Authentication required.")

        events = []
        try:
            challenge = QueueService.get_challenge(challenge_id, lock=True)
            account_key = QueueService.account_key(user, team)
            row = QueueEntry.query.filter_by(
                challenge_id=challenge.id, account_key=account_key
            ).first()
            if row and row.state in ("waiting", "selected"):
                db.session.rollback()
                return QueueService.user_status(challenge.id, user, team)
            if row and row.state == "completed":
                db.session.rollback()
                raise QueueError("You have already completed this challenge.")

            waiters = QueueService.waiting_entries(challenge.id)
            position = len(waiters) + 1
            now = _utcnow()
            if row is None:
                row = QueueEntry(
                    challenge_id=challenge.id,
                    user_id=user.id,
                    team_id=team.id if team is not None else None,
                    account_key=account_key,
                    position=position,
                    state="waiting",
                    joined_at=now,
                )
                db.session.add(row)
            else:
                row.user_id = user.id
                row.team_id = team.id if team is not None else None
                row.position = position
                row.state = "waiting"
                row.round_number = None
                row.joined_at = now
                row.selected_at = None
                row.completed_at = None
                row.left_at = None
                row.next_notified_at = None
                row.selected_notified_at = None

            db.session.flush()
            QueueService._resequence(challenge.id)
            QueueService._notify_next(challenge.id, events)
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise QueueError("You or your team are already registered for this queue.") from exc
        except Exception:
            db.session.rollback()
            raise

        _publish_notifications(events)
        return QueueService.user_status(challenge_id, user, team)

    @staticmethod
    def leave(challenge_id, user=None, team=None):
        user = user or get_current_user()
        team = team if team is not None else get_current_team()
        if user is None:
            raise QueueError("Authentication required.")
        events = []
        try:
            challenge = QueueService.get_challenge(challenge_id, lock=True)
            account_key = QueueService.account_key(user, team)
            row = QueueEntry.query.filter_by(
                challenge_id=challenge.id, account_key=account_key
            ).first()
            if row is None or row.state == "left":
                raise QueueError("You are not in this queue.")
            if row.account_key.startswith("team:") and row.user_id != user.id:
                raise QueueError("Another teammate registered this team; only that player can leave the queue.")
            if row.state == "selected":
                raise QueueError("You have already been selected and cannot leave the queue.")
            if row.state == "completed":
                raise QueueError("You have already completed this challenge.")
            row.state = "left"
            row.left_at = _utcnow()
            QueueService._resequence(challenge.id)
            QueueService._notify_next(challenge.id, events)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        _publish_notifications(events)
        return QueueService.user_status(challenge_id, user, team)

    @staticmethod
    def _advance(challenge_id, automatic=False, only_if_due=False):
        events = []
        try:
            challenge = QueueService.get_challenge(challenge_id, lock=True)
            now = _utcnow()
            interval = max(1, int(challenge.interval_seconds or 1))
            if only_if_due:
                last_draw = challenge.last_draw_at or now
                if challenge.queue_mode != "automatic" or now < last_draw + timedelta(seconds=interval):
                    db.session.rollback()
                    return {"selected": 0, "due": False}
            waiters = QueueService.waiting_entries(challenge.id)
            if not waiters:
                if only_if_due:
                    # Empty automatic rounds still advance the timer, so a new
                    # signup does not get selected immediately after a long idle.
                    challenge.last_draw_at = now
                    db.session.commit()
                else:
                    db.session.rollback()
                return {"selected": 0, "due": True}

            batch_size = max(1, int(challenge.batch_size or 1))
            picked = waiters[:batch_size]
            challenge.round_number = int(challenge.round_number or 0) + 1
            challenge.last_draw_at = now
            challenge.last_selection_at = now
            for entry in picked:
                entry.state = "selected"
                entry.round_number = challenge.round_number
                entry.selected_at = now
                entry.left_at = None
                if entry.selected_notified_at is None:
                    entry.selected_notified_at = now
                    events.append(
                        QueueService._notification_for(
                            entry,
                            "You have been selected!",
                            "You have been selected and can now submit your flag.",
                        )
                    )
            QueueService._resequence(challenge.id)
            QueueService._notify_next(challenge.id, events)
            db.session.commit()
            selected = len(picked)
        except Exception:
            db.session.rollback()
            raise

        _publish_notifications(events)
        return {"selected": selected, "due": True}

    @staticmethod
    def advance(challenge_id):
        return QueueService._advance(challenge_id, automatic=False, only_if_due=False)

    @staticmethod
    def process_due_challenges():
        """Advance due automatic queues. Safe to call repeatedly from one scheduler."""
        ids = [
            row.id
            for row in QueueChallengeModel.query.with_entities(QueueChallengeModel.id)
            .filter_by(queue_mode="automatic")
            .all()
        ]
        processed = 0
        for challenge_id in ids:
            try:
                result = QueueService._advance(
                    challenge_id, automatic=True, only_if_due=True
                )
                if result.get("selected", 0):
                    processed += 1
            except Exception:
                log.exception("Automatic queue iteration failed for challenge %s", challenge_id)
        return processed

    @staticmethod
    def admin_state(challenge_id):
        challenge = QueueService.get_challenge(challenge_id)
        selected = QueueService.selected_entries(challenge.id)
        waiting = QueueService.waiting_entries(challenge.id)
        now = _utcnow()
        last_selection = challenge.last_selection_at
        elapsed = (
            max(0, int((now - last_selection).total_seconds()))
            if last_selection
            else None
        )
        timer_reference = challenge.last_draw_at or now
        next_draw_in = max(
            0,
            int(challenge.interval_seconds or 1)
            - max(0, int((now - timer_reference).total_seconds())),
        )
        return {
            "challenge": {
                "id": challenge.id,
                "name": challenge.name,
                "queue_mode": challenge.queue_mode,
                "batch_size": challenge.batch_size,
                "interval_seconds": challenge.interval_seconds,
                "show_position": bool(challenge.show_position),
                "show_eta": bool(challenge.show_eta),
                "selected_message": challenge.selected_message or "",
                "last_draw_at": last_selection.isoformat() if last_selection else None,
                "has_drawn": last_selection is not None,
                "round_number": challenge.round_number or 0,
                "elapsed_seconds": elapsed,
                "next_draw_in_seconds": next_draw_in,
            },
            "selected": [QueueService._serialize_entry(entry) for entry in selected],
            "waiting": [
                QueueService._serialize_entry(entry, position=index)
                for index, entry in enumerate(waiting, 1)
            ],
        }

    @staticmethod
    def admin_add(challenge_id, user_id):
        events = []
        try:
            challenge = QueueService.get_challenge(challenge_id, lock=True)
            user = Users.query.filter_by(id=user_id).first()
            if user is None:
                raise QueueError("User not found.")
            team = getattr(user, "team", None)
            account_key = QueueService.account_key(user, team)
            row = QueueEntry.query.filter_by(
                challenge_id=challenge.id, account_key=account_key
            ).first()
            if row and row.state in ("waiting", "selected"):
                raise QueueError("This user or their team is already in the queue.")
            if row and row.state == "completed":
                raise QueueError("This user or team has already completed the challenge.")

            position = len(QueueService.waiting_entries(challenge.id)) + 1
            now = _utcnow()
            if row is None:
                row = QueueEntry(
                    challenge_id=challenge.id,
                    user_id=user.id,
                    team_id=team.id if team else None,
                    account_key=account_key,
                    position=position,
                    state="waiting",
                    joined_at=now,
                )
                db.session.add(row)
            else:
                row.user_id = user.id
                row.team_id = team.id if team else None
                row.position = position
                row.state = "waiting"
                row.round_number = None
                row.joined_at = now
                row.selected_at = None
                row.completed_at = None
                row.left_at = None
                row.next_notified_at = None
                row.selected_notified_at = None
            db.session.flush()
            QueueService._resequence(challenge.id)
            QueueService._notify_next(challenge.id, events)
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise QueueError("This user or their team is already in the queue.") from exc
        except Exception:
            db.session.rollback()
            raise
        _publish_notifications(events)
        return QueueService.admin_state(challenge_id)

    @staticmethod
    def move_entry(entry_id, direction):
        if direction not in ("up", "down"):
            raise QueueError("Direction must be 'up' or 'down'.")
        events = []
        try:
            row = QueueEntry.query.filter_by(id=entry_id).with_for_update().first()
            if row is None:
                raise QueueError("Queue entry not found.")
            if row.state != "waiting":
                raise QueueError("Only waiting participants can be reordered.")
            waiting = QueueService.waiting_entries(row.challenge_id)
            index = next(i for i, entry in enumerate(waiting) if entry.id == row.id)
            other_index = index - 1 if direction == "up" else index + 1
            if 0 <= other_index < len(waiting):
                other = waiting[other_index]
                row.position, other.position = other.position, row.position
            QueueService._resequence(row.challenge_id)
            QueueService._notify_next(row.challenge_id, events)
            challenge_id = row.challenge_id
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        _publish_notifications(events)
        return QueueService.admin_state(challenge_id)

    @staticmethod
    def remove_entry(entry_id):
        events = []
        try:
            row = QueueEntry.query.filter_by(id=entry_id).with_for_update().first()
            if row is None:
                raise QueueError("Queue entry not found.")
            challenge_id = row.challenge_id
            row.state = "left"
            row.left_at = _utcnow()
            QueueService._resequence(challenge_id)
            QueueService._notify_next(challenge_id, events)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        _publish_notifications(events)
        return QueueService.admin_state(challenge_id)

    @staticmethod
    def mark_completed(challenge_id, user_id, team_id=None):
        query = QueueEntry.query.filter_by(
            challenge_id=challenge_id, user_id=user_id, state="selected"
        )
        if _is_team_mode() and team_id is not None:
            query = query.filter_by(team_id=team_id)
        entry = query.first()
        if entry is None:
            return
        entry.state = "completed"
        entry.completed_at = _utcnow()
        db.session.commit()
