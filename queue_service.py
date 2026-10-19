"""Queue business logic, including automatic rounds and CTFd notifications."""
import logging
import math
from datetime import datetime, timedelta
from html import escape as escape_html

from sqlalchemy.exc import IntegrityError

from CTFd.models import Notifications, Solves, Users, db
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
            if payloads:
                log.warning(
                    "Queue notifications were saved but live delivery is unavailable "
                    "(event manager missing); notification_count=%s",
                    len(payloads),
                )
            return
        for payload in payloads:
            # CTFd's built-in "notification" SSE event is broadcast to every
            # connected client. Queue notifications are private to one user, so
            # send a distinct event name and filter it client-side by user_id.
            manager.publish(data=payload, type="queue_notification")
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
    def has_solved_challenge(challenge, user):
        """Whether this CTFd scoring account has already solved the challenge."""
        account_id = getattr(user, "account_id", None)
        if account_id is None:
            account_id = getattr(user, "id", None)
        if account_id is None:
            return False
        return (
            Solves.query.filter_by(
                challenge_id=challenge.id,
                account_id=account_id,
            ).first()
            is not None
        )

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
    def _notification_for(entry, title, content, recipient_user_id=None):
        """Create a native CTFd notification and return its live-event payload."""
        challenge = getattr(entry, "challenge", None)
        challenge_name = (
            getattr(challenge, "name", None) or "Challenge {}".format(entry.challenge_id)
        )
        recipient_user_id = (
            entry.user_id if recipient_user_id is None else int(recipient_user_id)
        )
        # Place the challenge name inside the sentence and bold only that name
        # in the live popup. Both the name and message are safely HTML-escaped.
        plain_content = str(content).replace("{challenge_name}", challenge_name, 1)
        escaped_content = escape_html(plain_content)
        escaped_name = escape_html(challenge_name)
        html_content = escaped_content.replace(
            escaped_name, "<strong>{}</strong>".format(escaped_name), 1
        )
        notification = Notifications(
            title=title, content=plain_content, user_id=recipient_user_id
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
            log.exception(
                "Could not serialize queue notification for user_id=%s; "
                "using fallback payload",
                entry.user_id,
            )
            payload = {}

        payload.update(
            {
                "id": notification.id,
                "title": title,
                "content": plain_content,
                # CTFd's live popup renderer reads html rather than content.
                "html": html_content,
                "date": (
                    notification.date.isoformat()
                    if notification.date
                    else _utcnow().isoformat()
                ),
                "user_id": recipient_user_id,
                "team_id": notification.team_id,
                "type": "alert",
                "sound": True,
            }
        )
        return payload

    @staticmethod
    def _notify_next(challenge_id, events):
        waiters = QueueService.waiting_entries(challenge_id)
        head = waiters[0] if waiters else None

        # next_notified_at represents the current "next" turn. Clear it on
        # anyone who is no longer first, so a later reorder can notify them
        # again if they become the head of the queue a second time.
        for entry in waiters[1:]:
            if entry.next_notified_at is not None:
                entry.next_notified_at = None

        if head is None or head.next_notified_at is not None:
            return
        head.next_notified_at = _utcnow()
        events.append(
            QueueService._notification_for(
                head,
                "You are next in the queue",
                "You are currently first in the queue for {challenge_name}. Please be ready for the next draw.",
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
        common_status = {
            "can_submit": True,
            "queue_mode": challenge.queue_mode,
            "batch_size": challenge.batch_size,
            "interval_seconds": challenge.interval_seconds,
            "show_position": bool(challenge.show_position),
            "show_eta": bool(challenge.show_eta),
            "selected_message": challenge.selected_message or "",
        }
        if QueueService.has_solved_challenge(challenge, user):
            return {
                **common_status,
                "state": "completed",
                "can_join": False,
                "can_leave": False,
            }
        try:
            account_key = QueueService.account_key(user, team)
        except QueueError:
            return {
                "state": "needs_team",
                "can_join": False,
                "can_leave": False,
                "can_submit": True,
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

        if row is None or row.state in ("left", "completed"):
            return {
                "state": "not_joined",
                "can_join": True,
                "can_leave": False,
                "can_submit": True,
                "queue_mode": challenge.queue_mode,
                "batch_size": challenge.batch_size,
                "interval_seconds": challenge.interval_seconds,
                "show_position": bool(challenge.show_position),
                "show_eta": bool(challenge.show_eta),
                "selected_message": challenge.selected_message or "",
            }

        # Eviction only ends the current round; an unsolved scoring account may rejoin.
        if row.state == "evicted":
            return {
                **common_status,
                "state": "not_joined",
                "can_join": True,
                "can_leave": False,
            }

        if row.state != "completed" and row.account_key.startswith("team:") and row.user_id != user.id:
            return {
                "state": "registered_by_teammate",
                "can_join": False,
                "can_leave": False,
                "can_submit": True,
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
            "round_number": challenge.round_number or 0,
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
                    "can_submit": True,
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
                    "can_leave": True,
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
                    "can_submit": True,
                }
            )
        else:
            base.update(
                {
                    "state": "not_joined",
                    "can_join": True,
                    "can_leave": False,
                    "can_submit": True,
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
            if QueueService.has_solved_challenge(challenge, user):
                raise QueueError(
                    "You or your team have already solved this challenge and cannot rejoin its queue."
                )
            account_key = QueueService.account_key(user, team)
            row = QueueEntry.query.filter_by(
                challenge_id=challenge.id, account_key=account_key
            ).first()
            if row and row.state in ("waiting", "selected"):
                db.session.rollback()
                return QueueService.user_status(challenge.id, user, team)
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
            log_context = (
                challenge.id,
                user.id,
                team.id if team is not None else None,
                row.position,
            )
            db.session.commit()
            log.info(
                "Participant joined queue challenge_id=%s user_id=%s team_id=%s position=%s",
                *log_context,
            )
        except IntegrityError as exc:
            db.session.rollback()
            log.info(
                "Queue join lost a concurrent registration race challenge_id=%s user_id=%s",
                challenge_id,
                user.id,
            )
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
            if QueueService.has_solved_challenge(challenge, user):
                raise QueueError("You or your team have already solved this challenge.")
            account_key = QueueService.account_key(user, team)
            row = QueueEntry.query.filter_by(
                challenge_id=challenge.id, account_key=account_key
            ).first()
            if row is None or row.state in ("left", "completed"):
                raise QueueError("You are not in this queue.")
            if row.account_key.startswith("team:") and row.user_id != user.id:
                raise QueueError("Another teammate registered this team; only that player can leave the queue.")
            row.state = "left"
            row.left_at = _utcnow()
            QueueService._resequence(challenge.id)
            QueueService._notify_next(challenge.id, events)
            log_context = (
                challenge.id,
                user.id,
                team.id if team is not None else None,
            )
            db.session.commit()
            log.info(
                "Participant left queue challenge_id=%s user_id=%s team_id=%s",
                *log_context,
            )
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
            challenge_id = challenge.id
            now = _utcnow()
            interval = max(1, int(challenge.interval_seconds or 1))
            if only_if_due:
                last_draw = challenge.last_draw_at or now
                if (
                    challenge.queue_mode != "automatic"
                    or now < last_draw + timedelta(seconds=interval)
                ):
                    db.session.rollback()
                    return {"selected": 0, "evicted": 0, "due": False}

            # A selection lasts until the next queue iteration. Remove the
            # previous round's still-selected entries before drawing the next
            # batch, even when no one is waiting for the next round.
            previously_selected = QueueService.selected_entries(challenge.id)
            evicted_contexts = [
                (entry.id, entry.user_id, entry.team_id, entry.round_number)
                for entry in previously_selected
            ]
            evicted_count = len(evicted_contexts)
            for entry in previously_selected:
                entry.state = "evicted"
                entry.left_at = now

            waiters = QueueService.waiting_entries(challenge.id)
            source = "automatic" if automatic else "manual"
            if not waiters:
                # A draw is still an event when the queue is empty. Record its
                # timestamp so the admin elapsed counter always has a reference
                # and automatic mode starts a fresh interval from this draw.
                challenge.last_draw_at = now
                challenge.last_selection_at = now
                db.session.commit()
                for entry_id, user_id, team_id, round_number in evicted_contexts:
                    log.info(
                        "Selected participant evicted from queue at next iteration "
                        "challenge_id=%s entry_id=%s user_id=%s team_id=%s round_number=%s",
                        challenge_id,
                        entry_id,
                        user_id,
                        team_id,
                        round_number,
                    )
                if evicted_count:
                    log.info(
                        "Queue iteration completed with no new selections "
                        "challenge_id=%s source=%s evicted_count=%s",
                        challenge_id,
                        source,
                        evicted_count,
                    )
                else:
                    log.info(
                        "Queue draw completed with no participants "
                        "challenge_id=%s source=%s round_number=%s",
                        challenge_id,
                        source,
                        challenge.round_number or 0,
                    )
                return {"selected": 0, "evicted": evicted_count, "due": True}

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
                            "You have been selected for {challenge_name}!",
                        )
                    )
            QueueService._resequence(challenge.id)
            QueueService._notify_next(challenge.id, events)
            selected = len(picked)
            log_context = (
                challenge.id,
                source,
                challenge.round_number,
                selected,
                max(0, len(waiters) - selected),
                evicted_count,
            )
            db.session.commit()
            for entry_id, user_id, team_id, round_number in evicted_contexts:
                log.info(
                    "Selected participant evicted from queue at next iteration "
                    "challenge_id=%s entry_id=%s user_id=%s team_id=%s round_number=%s",
                    challenge_id,
                    entry_id,
                    user_id,
                    team_id,
                    round_number,
                )
            if selected or evicted_count:
                log.info(
                    "Queue draw completed challenge_id=%s source=%s round_number=%s "
                    "selected_count=%s remaining_waiting=%s evicted_previous_count=%s",
                    *log_context,
                )
        except Exception:
            db.session.rollback()
            log.exception("Failed to advance queue challenge_id=%s", challenge_id)
            raise

        _publish_notifications(events)
        return {"selected": selected, "evicted": evicted_count, "due": True}

    @staticmethod
    def advance(challenge_id):
        return QueueService._advance(challenge_id, automatic=False, only_if_due=False)

    @staticmethod
    def process_due_challenges():
        """Advance due automatic queues. Safe to call repeatedly from one scheduler."""
        ids = [
            row.id
            for row in QueueChallengeModel.query.with_entities(QueueChallengeModel.id)
            .filter_by(queue_mode="automatic", state="visible")
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
        last_draw = challenge.last_draw_at
        elapsed = (
            max(0, int((now - last_draw).total_seconds()))
            if last_draw
            else 0
        )
        timer_reference = last_draw or now
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
                "last_draw_at": last_draw.isoformat() if last_draw else None,
                "has_drawn": last_draw is not None,
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
            if QueueService.has_solved_challenge(challenge, user):
                raise QueueError(
                    "This user or their team has already solved the challenge and cannot rejoin its queue."
                )
            team = getattr(user, "team", None)
            account_key = QueueService.account_key(user, team)
            row = QueueEntry.query.filter_by(
                challenge_id=challenge.id, account_key=account_key
            ).first()
            if row and row.state in ("waiting", "selected"):
                raise QueueError("This user or their team is already in the queue.")
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
            admin_user = get_current_user()
            log_context = (
                challenge.id,
                user.id,
                getattr(admin_user, "id", None),
            )
            db.session.commit()
            log.info(
                "Administrator added participant to queue challenge_id=%s user_id=%s admin_user_id=%s",
                *log_context,
            )
        except IntegrityError as exc:
            db.session.rollback()
            log.warning(
                "Concurrent admin queue registration conflict challenge_id=%s target_user_id=%s",
                challenge_id,
                user_id,
            )
            raise QueueError("This user or their team is already in the queue.") from exc
        except Exception:
            db.session.rollback()
            raise
        _publish_notifications(events)
        return QueueService.admin_state(challenge_id)


    @staticmethod
    def notify_flag_to_selected_team(entry_id, flag_content):
        """Send a challenge flag only to the selected participant's team."""
        events = []
        try:
            entry = QueueEntry.query.filter_by(id=entry_id).with_for_update().first()
            if entry is None:
                raise QueueError("Queue entry not found.")
            if entry.state != "selected":
                raise QueueError("The participant must be currently selected to notify the flag.")

            challenge = QueueService.get_challenge(entry.challenge_id, lock=True)
            if QueueService.has_solved_challenge(challenge, entry.user):
                raise QueueError("The selected player's team has already solved this challenge.")

            if entry.team_id is not None:
                recipients = [
                    user_id
                    for (user_id,) in Users.query.with_entities(Users.id)
                    .filter_by(team_id=entry.team_id)
                    .order_by(Users.id.asc())
                    .all()
                ]
            else:
                recipients = [entry.user_id]

            recipients = sorted(set(recipients))
            if not recipients:
                raise QueueError("No eligible team members were found for this notification.")

            for recipient_id in recipients:
                events.append(
                    QueueService._notification_for(
                        entry,
                        "Challenge flag",
                        "The flag for {challenge_name} is: " + str(flag_content),
                        recipient_user_id=recipient_id,
                    )
                )

            challenge_name = challenge.name
            team_id = entry.team_id
            db.session.commit()
            log.info(
                "Administrator notified selected participant's team of challenge flag "
                "challenge_id=%s entry_id=%s team_id=%s recipient_count=%s",
                challenge.id,
                entry_id,
                team_id,
                len(recipients),
            )
        except Exception:
            db.session.rollback()
            raise

        _publish_notifications(events)
        return {
            "recipient_count": len(recipients),
            "team_id": team_id,
            "challenge_name": challenge_name,
        }

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
            admin_user = get_current_user()
            log_context = (
                challenge_id,
                entry_id,
                direction,
                getattr(admin_user, "id", None),
            )
            db.session.commit()
            log.info(
                "Administrator reordered queue entry challenge_id=%s entry_id=%s direction=%s admin_user_id=%s",
                *log_context,
            )
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
            admin_user = get_current_user()
            log_context = (
                challenge_id,
                entry_id,
                getattr(admin_user, "id", None),
            )
            db.session.commit()
            log.info(
                "Administrator removed queue entry challenge_id=%s entry_id=%s admin_user_id=%s",
                *log_context,
            )
        except Exception:
            db.session.rollback()
            raise
        _publish_notifications(events)
        return QueueService.admin_state(challenge_id)
