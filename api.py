"""REST API endpoints for queue participants and administrators."""
from flask_restx import Namespace, Resource, abort, request

from CTFd.utils.decorators import admins_only, authed_only, during_ctf_time_only
from CTFd.utils.user import get_current_team, get_current_user, is_admin

from .models import QueueChallengeModel, QueueEntry
from .queue_service import QueueError, QueueService

queue_namespace = Namespace(
    "ctfd-queue",
    description="Queue challenge participation and administration",
)


def _challenge_for_user(challenge_id):
    try:
        challenge = QueueService.get_challenge(challenge_id)
    except QueueError:
        abort(404, "Queue challenge not found.")
    if not is_admin() and challenge.state != "visible":
        abort(404, "Queue challenge not found.")
    return challenge


def _run_queue_operation(callback):
    try:
        return {"success": True, "data": callback()}
    except QueueError as exc:
        abort(400, str(exc))
    except ValueError as exc:
        abort(400, str(exc))


@queue_namespace.route("/challenges/<int:challenge_id>/status")
class UserQueueStatus(Resource):
    @authed_only
    def get(self, challenge_id):
        challenge = _challenge_for_user(challenge_id)
        return _run_queue_operation(
            lambda: QueueService.user_status(
                challenge.id, get_current_user(), get_current_team()
            )
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
            )
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
            )
        )


def _admin_challenge(challenge_id):
    try:
        return QueueService.get_challenge(challenge_id)
    except QueueError:
        abort(404, "Queue challenge not found.")


@queue_namespace.route("/admin/challenges/<int:challenge_id>/state")
class AdminQueueState(Resource):
    @admins_only
    def get(self, challenge_id):
        challenge = _admin_challenge(challenge_id)
        return {"success": True, "data": QueueService.admin_state(challenge.id)}


@queue_namespace.route("/admin/challenges/<int:challenge_id>/advance")
class AdminQueueAdvance(Resource):
    @admins_only
    def post(self, challenge_id):
        challenge = _admin_challenge(challenge_id)
        result = _run_queue_operation(
            lambda: {
                **QueueService.advance(challenge.id),
                "state": QueueService.admin_state(challenge.id),
            }
        )
        return result


@queue_namespace.route("/admin/challenges/<int:challenge_id>/entries")
class AdminQueueAdd(Resource):
    @admins_only
    def post(self, challenge_id):
        challenge = _admin_challenge(challenge_id)
        data = request.get_json(silent=True) or {}
        try:
            user_id = int(data.get("user_id"))
        except (TypeError, ValueError):
            abort(400, "A valid user_id is required.")
        return _run_queue_operation(
            lambda: QueueService.admin_add(challenge.id, user_id)
        )


@queue_namespace.route("/admin/entries/<int:entry_id>/move")
class AdminQueueMove(Resource):
    @admins_only
    def post(self, entry_id):
        data = request.get_json(silent=True) or {}
        direction = str(data.get("direction", "")).lower()
        return _run_queue_operation(
            lambda: QueueService.move_entry(entry_id, direction)
        )


@queue_namespace.route("/admin/entries/<int:entry_id>")
class AdminQueueRemove(Resource):
    @admins_only
    def delete(self, entry_id):
        return _run_queue_operation(lambda: QueueService.remove_entry(entry_id))
