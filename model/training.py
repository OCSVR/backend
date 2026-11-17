"""
Training sessions for OCS VR.

One row is one run of one mission by one learner: the VR app starts a session when a mission
begins and completes it with the score, the number of attempts and the time taken.

    TrainingSession
    ---------------
    id            int, primary key
    session_id    uuid text, unique: the id the VR app holds between start and complete
    user_id       -> users.id: the learner, always the signed-in account
    module        "module.mission", e.g. build_computer.identify_components
    status        started | completed
    attempts      tries the learner needed (set on complete)
    duration_s    seconds the mission took (set on complete)
    score         0 to 100 (set on complete)
    device        what it ran on, e.g. quest3 or desktop
    started_at    UTC, set by the server
    completed_at  UTC, set by the server
"""
import re
import uuid
from datetime import datetime, timedelta, timezone

from __init__ import app, db
from model.user import User

STARTED = 'started'
COMPLETED = 'completed'

# Limits the API checks before a row is saved.
MODULE_MAX = 128
DEVICE_MAX = 32
SCORE_MAX = 100
ATTEMPTS_MAX = 1000
DURATION_MAX_S = 24 * 60 * 60
# "module.mission" in lower snake case. The names themselves are data in the VR app's module
# registry, so a new mission needs no change here.
MODULE_PATTERN = re.compile(r'^[a-z0-9_]+(\.[a-z0-9_]+)*$')

# How many sessions a progress report lists; its totals always cover every session.
RECENT_SESSIONS = 100


def utcnow():
    """The current UTC time without tzinfo, which is how every DateTime column here is stored."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(moment):
    return moment.isoformat(timespec='seconds') + 'Z' if moment else None


class TrainingSession(db.Model):
    __tablename__ = 'training_sessions'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(36), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    module = db.Column(db.String(MODULE_MAX), nullable=False)
    status = db.Column(db.String(16), nullable=False, default=STARTED)
    attempts = db.Column(db.Integer, nullable=True)
    duration_s = db.Column(db.Integer, nullable=True)
    score = db.Column(db.Integer, nullable=True)
    device = db.Column(db.String(DEVICE_MAX), nullable=True)
    started_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship('User', backref=db.backref('training_sessions', cascade='all, delete-orphan'))

    def __init__(self, user_id, module, device=None, session_id=None):
        self.session_id = session_id or str(uuid.uuid4())
        self.user_id = user_id
        self.module = module
        self.device = device
        self.status = STARTED
        self.started_at = utcnow()

    def create(self):
        try:
            db.session.add(self)
            db.session.commit()
            return self
        except Exception:
            db.session.rollback()
            raise

    def read(self):
        return {
            'session_id': self.session_id,
            'learner_id': self.user.uid if self.user else None,
            'module': self.module,
            'status': self.status,
            'attempts': self.attempts,
            'duration_s': self.duration_s,
            'score': self.score,
            'device': self.device,
            'started_at': _iso(self.started_at),
            'completed_at': _iso(self.completed_at),
        }

    def set_result(self, attempts, duration_s, score, device=None):
        """Mark this session completed with its result. The caller saves it (create or commit)."""
        self.status = COMPLETED
        self.attempts = attempts
        self.duration_s = duration_s
        self.score = score
        if device:
            self.device = device
        self.completed_at = utcnow()
        return self

    def complete(self, attempts, duration_s, score, device=None):
        """Save the result of a session that was started earlier."""
        try:
            self.set_result(attempts, duration_s, score, device)
            db.session.commit()
            return self
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def find(session_id):
        return TrainingSession.query.filter_by(session_id=session_id).first()

    @staticmethod
    def completed_now(user_id, module, attempts, duration_s, score, device=None, session_id=None):
        """A session reported only when it ended: its start is worked out from the time it took."""
        session = TrainingSession(user_id=user_id, module=module, device=device, session_id=session_id)
        session.set_result(attempts, duration_s, score)
        session.started_at = session.completed_at - timedelta(seconds=duration_s)
        return session.create()


def progress(learner):
    """One learner's progress: totals, one line per module, and the most recent sessions."""
    # Newest first, by when each session last changed: its completion, or its start if unfinished.
    sessions = sorted(TrainingSession.query.filter_by(user_id=learner.id).all(),
                      key=lambda s: (s.completed_at or s.started_at, s.id), reverse=True)
    done = [s for s in sessions if s.status == COMPLETED]

    modules = {}
    for session in sessions:
        modules.setdefault(session.module, {
            'module': session.module, 'completed': False, 'sessions_started': 0,
            'sessions_completed': 0, 'best_score': None, 'last_score': None, 'attempts': 0,
            'best_duration_s': None, 'last_completed_at': None,
        })['sessions_started'] += 1
    for session in reversed(done):  # oldest first, so "last" ends on the newest
        line = modules[session.module]
        line['completed'] = True
        line['sessions_completed'] += 1
        line['best_score'] = session.score if line['best_score'] is None else max(line['best_score'], session.score)
        line['last_score'] = session.score
        line['attempts'] += session.attempts
        line['best_duration_s'] = (session.duration_s if line['best_duration_s'] is None
                                   else min(line['best_duration_s'], session.duration_s))
        line['last_completed_at'] = _iso(session.completed_at)

    return {
        'learner_id': learner.uid,
        'name': learner.name,
        'summary': {
            'modules_completed': sum(1 for line in modules.values() if line['completed']),
            'sessions_started': len(sessions),
            'sessions_completed': len(done),
            'attempts': sum(s.attempts for s in done),
            'duration_s': sum(s.duration_s for s in done),
        },
        'modules': sorted(modules.values(), key=lambda line: line['module']),
        'recent_sessions': [s.read() for s in sessions[:RECENT_SESSIONS]],
    }


# Fixed ids, so seeding the same database twice (or importing a backup over the seed) never
# doubles the sample rows.
_SAMPLES = (
    ('0c5b1f6e-0000-4000-8000-000000000001', 'build_computer.identify_components', 1, 95, 100, 'desktop'),
    ('0c5b1f6e-0000-4000-8000-000000000002', 'build_computer.assemble_system', 2, 184, 85, 'quest3'),
)


def initTrainingSessions():
    """Sample completed sessions for the default test user, so a new database has progress to show."""
    with app.app_context():
        db.create_all()
        learner = User.query.filter_by(_uid=app.config['USER_UID']).first()
        if learner is None:
            return
        for session_id, module, attempts, duration_s, score, device in _SAMPLES:
            if not TrainingSession.find(session_id):
                TrainingSession.completed_now(learner.id, module, attempts, duration_s, score, device, session_id)


# ── Migration (api/data_export_import_api.py and the scripts/db_* scripts) ────────────────────

def export_sessions(sessions=None):
    """Sessions as the migration moves them: read(), whose learner_id and session_id identify a
    row on any database, whatever numeric ids it has there."""
    if sessions is None:
        sessions = TrainingSession.query.order_by(TrainingSession.id).all()
    return [session.read() for session in sessions]


def _parse(moment):
    try:
        return datetime.fromisoformat(str(moment).replace('Z', '+00:00')).replace(tzinfo=None) if moment else None
    except ValueError:
        return None


def import_sessions(rows):
    """Load exported sessions. A session that is already here (same session_id) is skipped, so
    importing the same data twice, or over the seeded samples, adds nothing."""
    stats = {'imported': 0, 'skipped': 0, 'failed': 0, 'errors': []}
    for row in rows or []:
        try:
            session_id, uid = row.get('session_id'), row.get('learner_id')
            if session_id and TrainingSession.find(session_id):
                stats['skipped'] += 1
                continue
            learner = User.query.filter_by(_uid=uid).first() if uid else None
            started_at = _parse(row.get('started_at'))
            if not session_id or learner is None or not row.get('module') or started_at is None:
                # user_id is NOT NULL, so a session without its learner cannot be stored.
                stats['failed'] += 1
                stats['errors'].append(f"TrainingSession {session_id!r}: no session_id, module, start time or user for uid {uid!r}")
                continue
            session = TrainingSession(user_id=learner.id, module=row['module'], device=row.get('device'), session_id=session_id)
            session.status = COMPLETED if row.get('status') == COMPLETED else STARTED
            session.attempts, session.duration_s, session.score = row.get('attempts'), row.get('duration_s'), row.get('score')
            session.started_at, session.completed_at = started_at, _parse(row.get('completed_at'))
            db.session.add(session)
            db.session.commit()
            stats['imported'] += 1
        except Exception as e:
            db.session.rollback()
            stats['failed'] += 1
            stats['errors'].append(f"TrainingSession: {e}")
    return stats
