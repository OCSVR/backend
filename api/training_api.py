"""
Training API for OCS VR.

The VR app (or any client) signs in with /api/authenticate, then:

- POST /api/sessions/start            a mission begins; returns its session_id
- POST /api/sessions/complete         a mission ended; saves score, attempts and time
- GET  /api/learners/<uid>/progress   one learner's progress, for a dashboard

The learner is always the signed-in account, never a field of the request, so nobody can post a
score for somebody else. Manual requests are in api/training.http; scripts/verify_training.py
checks every rule here.
"""
from flask import Blueprint, g
from flask_restful import Api, Resource

from api.authorize import token_required
from api.request_body import json_object
from model import account_types
from model import training
from model.training import TrainingSession
from model.user import User

training_api = Blueprint('training_api', __name__, url_prefix='/api')
api = Api(training_api)


def _whole_number(value):
    """The value as an int when it is a JSON whole number (85 or 85.0), otherwise None.
    true and false are not numbers here, although Python counts them as 1 and 0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _read_module(body):
    """(module, None), or (None, why the body's module is not usable)."""
    module = body.get('module')
    if not isinstance(module, str) or not module:
        return None, 'module is required, for example build_computer.identify_components'
    if len(module) > training.MODULE_MAX or not training.MODULE_PATTERN.match(module):
        return None, (f'module must be lower case words joined by dots, such as '
                      f'build_computer.identify_components, at most {training.MODULE_MAX} characters')
    return module, None


def _read_device(body):
    """(device or None, None), or (None, why the body's device is not usable)."""
    device = body.get('device')
    if device is None or device == '':
        return None, None
    if not isinstance(device, str) or len(device) > training.DEVICE_MAX:
        return None, f'device must be text of at most {training.DEVICE_MAX} characters'
    return device, None


def _read_result(body):
    """({attempts, duration_s, score}, None), or (None, why the body's result is not usable)."""
    limits = (('attempts', 1, training.ATTEMPTS_MAX),
              ('duration_s', 0, training.DURATION_MAX_S),
              ('score', 0, training.SCORE_MAX))
    result = {}
    for field, low, high in limits:
        number = _whole_number(body.get(field))
        if number is None or not low <= number <= high:
            return None, f'{field} is required and must be a whole number from {low} to {high}'
        result[field] = number
    return result, None


def _wrong_learner(body, user):
    """True when the body names a learner other than the signed-in account."""
    learner_id = body.get('learner_id')
    return learner_id is not None and learner_id != user.uid


_NOT_YOUR_SCORE = {'message': 'learner_id does not match the signed-in account. '
                              'A session always belongs to the account that is signed in.'}


class TrainingAPI:

    class _Start(Resource):
        @token_required()
        def post(self):
            """Start a session for the signed-in learner. Body: {module, device?}."""
            user = g.current_user
            body = json_object()
            if _wrong_learner(body, user):
                return _NOT_YOUR_SCORE, 403
            module, error = _read_module(body)
            if error:
                return {'message': error}, 400
            device, error = _read_device(body)
            if error:
                return {'message': error}, 400

            session = TrainingSession(user_id=user.id, module=module, device=device).create()
            return session.read(), 201

    class _Complete(Resource):
        @token_required()
        def post(self):
            """Save a mission's result. Body: {session_id?, module, attempts, duration_s, score, device?}.

            With a session_id from /sessions/start, that session is completed (200). Sending the
            same completion again returns the saved result and changes nothing, so the VR app can
            retry after a dropped connection. Without a session_id, a completed session is
            created in one request (201); its module is then required.
            """
            user = g.current_user
            body = json_object()
            if _wrong_learner(body, user):
                return _NOT_YOUR_SCORE, 403
            if body.get('status', training.COMPLETED) != training.COMPLETED:
                return {'message': f"status, when sent, must be '{training.COMPLETED}'"}, 400

            session = None
            session_id = body.get('session_id')
            if session_id is not None:
                session = TrainingSession.find(session_id) if isinstance(session_id, str) else None
                # Somebody else's session is reported the same as one that does not exist.
                if session is None or session.user_id != user.id:
                    return {'message': 'No training session with that session_id for this account'}, 404
                if body.get('module') not in (None, session.module):
                    return {'message': f'module does not match the session, which is for {session.module}'}, 400
                if session.status == training.COMPLETED:
                    return session.read(), 200

            if session is None:
                module, error = _read_module(body)
                if error:
                    return {'message': error}, 400
            device, error = _read_device(body)
            if error:
                return {'message': error}, 400
            result, error = _read_result(body)
            if error:
                return {'message': error}, 400

            if session is None:
                session = TrainingSession.completed_now(user.id, module, device=device, **result)
                return session.read(), 201
            session.complete(device=device, **result)
            return session.read(), 200

    class _Progress(Resource):
        @token_required()
        def get(self, uid):
            """A learner's progress. Learners see their own; Teachers and Admins see anyone's."""
            user = g.current_user
            if uid == user.uid:
                learner = user
            elif user.role not in account_types.staff_roles():
                return {'message': 'You can only see your own progress'}, 403
            else:
                learner = User.query.filter_by(_uid=uid).first()
                if learner is None:
                    return {'message': f'No learner with the id {uid}'}, 404
            return training.progress(learner), 200

    api.add_resource(_Start, '/sessions/start')
    api.add_resource(_Complete, '/sessions/complete')
    api.add_resource(_Progress, '/learners/<string:uid>/progress')
