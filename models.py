"""Database models for queue-based CTFd challenges."""
from datetime import datetime

from sqlalchemy import UniqueConstraint

from CTFd.models import Challenges, db


class QueueChallengeModel(Challenges):
    """Joined-table subtype preserving all native CTFd challenge fields."""

    __tablename__ = "queue_challenges"
    __mapper_args__ = {"polymorphic_identity": "queue"}

    id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        primary_key=True,
    )

    queue_mode = db.Column(db.String(16), nullable=False, default="manual")
    batch_size = db.Column(db.Integer, nullable=False, default=1)
    interval_seconds = db.Column(db.Integer, nullable=False, default=900)
    show_position = db.Column(db.Boolean, nullable=False, default=True)
    show_eta = db.Column(db.Boolean, nullable=False, default=True)
    selected_message = db.Column(
        db.Text,
        nullable=False,
        default="You have been selected! You may now attempt this challenge.",
    )
    # last_draw_at anchors the automatic cadence; last_selection_at is the last
    # round that actually selected participants (None until the first draw).
    last_draw_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_selection_at = db.Column(db.DateTime, nullable=True)
    round_number = db.Column(db.Integer, nullable=False, default=0)


class QueueEntry(db.Model):
    """One signup per challenge and account; left entries rejoin, evicted entries are terminal."""

    __tablename__ = "queue_entries"
    __table_args__ = (
        UniqueConstraint(
            "challenge_id", "account_key", name="uq_queue_challenge_account"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(
        db.Integer,
        db.ForeignKey("queue_challenges.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    team_id = db.Column(
        db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    # "team:<id>" in team mode, otherwise "user:<id>". This portable unique key
    # prevents two team members from registering the same team twice.
    account_key = db.Column(db.String(80), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=1)
    state = db.Column(db.String(16), nullable=False, default="waiting", index=True)
    round_number = db.Column(db.Integer, nullable=True)
    joined_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    selected_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    left_at = db.Column(db.DateTime, nullable=True)
    next_notified_at = db.Column(db.DateTime, nullable=True)
    selected_notified_at = db.Column(db.DateTime, nullable=True)

    challenge = db.relationship(
        "QueueChallengeModel", foreign_keys=[challenge_id], lazy="joined"
    )
    user = db.relationship("Users", foreign_keys=[user_id], lazy="joined")
    team = db.relationship("Teams", foreign_keys=[team_id], lazy="joined")

    def __repr__(self):
        return (
            "<QueueEntry id={} challenge={} account={} state={}>".format(
                self.id, self.challenge_id, self.account_key, self.state
            )
        )



class QueueNotification(db.Model):
    """Private queue notification delivered only to its recipient."""

    __tablename__ = "queue_notifications"

    id = db.Column(db.Integer, primary_key=True)
    recipient_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    queue_entry_id = db.Column(
        db.Integer,
        db.ForeignKey("queue_entries.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title = db.Column(db.String(160), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    acknowledged_at = db.Column(db.DateTime, nullable=True, index=True)
