#!/usr/bin/env python3
"""Verify the OCS VR training API (api/training_api.py, model/training.py): starting and
completing a session, the checks on a result, who a session belongs to, who may read a learner's
progress, and that seeding twice does not double the sample rows.

Runs the real app against a throwaway SQLite database with the network blocked, and checksums the
real dev DB before and after to prove it was never touched.

Usage:  python scripts/verify_training.py        (from the flask repo root)
Exit:   0 all checks passed, 1 otherwise.
"""
import hashlib, logging, os, socket, sys, tempfile
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault("SECRET_KEY", "verify-training")

REAL_DB = os.path.join(REPO, "instance", "volumes", "user_management.db")
PASSWORD = "Verify1!pass"
MODULE = "build_computer.identify_components"
GOOD = {"attempts": 2, "duration_s": 184, "score": 85}
_failures = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        _failures.append(label)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    real_before = sha256(REAL_DB) if os.path.exists(REAL_DB) else None

    def no_network(*_args, **_kwargs):
        raise OSError("network blocked by verify_training.py")
    socket.socket.connect = no_network

    from sqlalchemy import create_engine
    from main import app, db  # registers every model and blueprint
    from model.user import User
    from model.training import TrainingSession, initTrainingSessions, utcnow
    with app.app_context():
        db.engines[None] = create_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='training-'), 'new.db')}")
        db.create_all()
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False
    logging.disable(logging.CRITICAL)

    def client_for(uid, role="User"):
        with app.app_context():
            user = User.query.filter_by(_uid=uid).first()
            if user is None:
                user = User(name=f"{uid.title()} Person", uid=uid, password=PASSWORD, role=role).create()
            user.active, user.last_verified = True, utcnow()
            db.session.commit()
        client = app.test_client()
        r = client.post("/api/authenticate", json={"uid": uid, "password": PASSWORD})
        assert r.status_code == 200, f"could not sign in as {uid}: {r.status_code}"
        return client

    def rows(uid=None):
        with app.app_context():
            query = TrainingSession.query
            if uid:
                query = query.filter_by(user_id=User.query.filter_by(_uid=uid).first().id)
            return query.count()

    def said(r, text):
        return text in ((r.get_json() or {}).get("message") or "")

    ada, bob, teacher = client_for("ada"), client_for("bob"), client_for("teach", role="Teacher")

    # nobody signed in
    anonymous = app.test_client()
    for method, url in (("POST", "/api/sessions/start"), ("POST", "/api/sessions/complete"),
                        ("GET", "/api/learners/ada/progress")):
        r = anonymous.open(url, method=method, json={"module": MODULE, **GOOD})
        check(f"{method} {url} without signing in is a 401", r.status_code == 401, r.status_code)
    check("nothing was saved for requests without a sign-in", rows() == 0, rows())

    # start
    r = ada.post("/api/sessions/start", json={"module": MODULE, "device": "quest3"})
    started = r.get_json() or {}
    check("starting a session is a 201 with a session_id", r.status_code == 201 and len(started.get("session_id", "")) == 36, r.get_data(as_text=True)[:120])
    check("the session belongs to the signed-in learner and is 'started'",
          started.get("learner_id") == "ada" and started.get("status") == "started" and started.get("score") is None, started)
    for label, body in (("no module", {}), ("an upper case module", {"module": "Build_Computer"}),
                        ("a module with spaces", {"module": "build computer"}), ("a module that is a number", {"module": 7}),
                        ("a module over the length limit", {"module": "a" * 129}),
                        ("a device over the length limit", {"module": MODULE, "device": "q" * 33}),
                        ("a device that is a number", {"module": MODULE, "device": 3})):
        r = ada.post("/api/sessions/start", json=body)
        check(f"starting with {label} is a 400", r.status_code == 400, f"{r.status_code} {r.get_data(as_text=True)[:100]}")
    check("the refused starts saved nothing", rows() == 1, rows())

    # complete a started session
    sid = started.get("session_id")
    r = bob.post("/api/sessions/complete", json={"session_id": sid, **GOOD})
    check("completing somebody else's session is a 404, the same as one that does not exist", r.status_code == 404, r.status_code)
    r = ada.post("/api/sessions/complete", json={"session_id": "no-such-session", **GOOD})
    check("completing an unknown session_id is a 404", r.status_code == 404, r.status_code)
    r = ada.post("/api/sessions/complete", json={"session_id": 12, **GOOD})
    check("a session_id that is not text is a 404", r.status_code == 404, r.status_code)
    r = ada.post("/api/sessions/complete", json={"session_id": sid, "module": "build_network.add_router", **GOOD})
    check("a module that differs from the session's is a 400", r.status_code == 400 and said(r, "does not match"), r.get_data(as_text=True)[:100])
    r = ada.post("/api/sessions/complete", json={"session_id": sid, "status": "failed", **GOOD})
    check("a status other than 'completed' is a 400", r.status_code == 400 and said(r, "status"), r.get_data(as_text=True)[:100])

    for field, low, high in (("attempts", 1, 1000), ("duration_s", 0, 86400), ("score", 0, 100)):
        bad = {"missing": None, "below the range": low - 1, "above the range": high + 1, "text": "5",
               "true": True, "a fraction": low + 0.5, "a list": [low]}
        for label, value in bad.items():
            body = {"session_id": sid, **GOOD, field: value}
            if value is None:
                del body[field]
            r = ada.post("/api/sessions/complete", json=body)
            check(f"{field} {label} is a 400 naming the field", r.status_code == 400 and said(r, field), f"{r.status_code} {r.get_data(as_text=True)[:100]}")
    with app.app_context():
        check("the refused completions left the session 'started'", TrainingSession.find(sid).status == "started")

    # the payload from the plan, with the fields the server works out itself (learner, timestamp)
    r = ada.post("/api/sessions/complete", json={"session_id": sid, "learner_id": "ada", "module": MODULE, "status": "completed",
                                                 "attempts": 2, "duration_s": 184.0, "score": 85, "device": "quest3",
                                                 "timestamp": "2026-04-14T10:32:00Z"})
    done = r.get_json() or {}
    check("completing the session is a 200 with the result saved (a whole number sent as 184.0 is accepted)",
          r.status_code == 200 and (done.get("status"), done.get("attempts"), done.get("duration_s"), done.get("score")) == ("completed", 2, 184, 85),
          r.get_data(as_text=True)[:160])
    check("the server sets completed_at itself, not the client's timestamp",
          (done.get("completed_at") or "").startswith(utcnow().strftime("%Y-")) and done.get("completed_at", "").endswith("Z"), done.get("completed_at"))
    r = ada.post("/api/sessions/complete", json={"session_id": sid, "attempts": 1, "duration_s": 1, "score": 100})
    check("completing it again is a 200 that returns the saved result unchanged", r.status_code == 200 and r.get_json() == done, r.get_data(as_text=True)[:160])

    # complete in one request
    r = ada.post("/api/sessions/complete", json={"module": "build_network.connect_two_computers", "attempts": 1, "duration_s": 60, "score": 70})
    one = r.get_json() or {}
    check("completing without a session_id creates a completed session (201)", r.status_code == 201 and one.get("status") == "completed" and one.get("session_id") != sid, r.get_data(as_text=True)[:160])
    if one.get("started_at") and one.get("completed_at"):
        took = (datetime.fromisoformat(one["completed_at"][:-1]) - datetime.fromisoformat(one["started_at"][:-1])).total_seconds()
        check("its start is the completion time less duration_s", took == 60, took)
    r = ada.post("/api/sessions/complete", json=GOOD)
    check("completing without a session_id or a module is a 400", r.status_code == 400 and said(r, "module"), r.get_data(as_text=True)[:100])

    # nobody posts for somebody else
    before = rows()
    for url in ("/api/sessions/start", "/api/sessions/complete"):
        r = bob.post(url, json={"learner_id": "ada", "module": MODULE, **GOOD})
        check(f"POST {url} naming another learner is a 403", r.status_code == 403 and said(r, "learner_id"), f"{r.status_code} {r.get_data(as_text=True)[:100]}")
    check("and it saved nothing, for either learner", rows() == before and rows("bob") == 0, (rows(), rows("bob")))

    # progress
    ada.post("/api/sessions/start", json={"module": "build_network.add_router"})   # started, never finished
    ada.post("/api/sessions/complete", json={"module": MODULE, "attempts": 1, "duration_s": 90, "score": 100})
    r = ada.get("/api/learners/ada/progress")
    report = r.get_json() or {}
    check("a learner reads their own progress (200)", r.status_code == 200 and report.get("learner_id") == "ada", r.get_data(as_text=True)[:120])
    check("the summary counts started and completed sessions, attempts and time",
          report.get("summary") == {"modules_completed": 2, "sessions_started": 4, "sessions_completed": 3, "attempts": 4, "duration_s": 334}, report.get("summary"))
    by_module = {line["module"]: line for line in report.get("modules", [])}
    first = by_module.get(MODULE, {})
    check("a module's line has its best and last score, attempts and best time",
          (first.get("completed"), first.get("sessions_completed"), first.get("best_score"), first.get("last_score"), first.get("attempts"), first.get("best_duration_s")) == (True, 2, 100, 100, 3, 90), first)
    unfinished = by_module.get("build_network.add_router", {})
    check("a module that was started but never finished is listed as not completed",
          (unfinished.get("completed"), unfinished.get("sessions_started"), unfinished.get("best_score")) == (False, 1, None), unfinished)
    check("recent_sessions lists every session, newest first",
          len(report.get("recent_sessions", [])) == 4 and report["recent_sessions"][0]["module"] == MODULE, [s.get("module") for s in report.get("recent_sessions", [])])
    r = bob.get("/api/learners/ada/progress")
    check("another learner's progress is a 403", r.status_code == 403, r.status_code)
    r = bob.get("/api/learners/nobody-here/progress")
    check("so is a learner that does not exist, for an account that is not staff", r.status_code == 403, r.status_code)
    r = bob.get("/api/learners/bob/progress")
    check("a learner with no sessions gets an empty report (200)", r.status_code == 200 and r.get_json()["summary"]["sessions_started"] == 0, r.get_data(as_text=True)[:120])
    r = teacher.get("/api/learners/ada/progress")
    check("a Teacher reads any learner's progress (200)", r.status_code == 200 and r.get_json()["summary"]["sessions_completed"] == 3, r.get_data(as_text=True)[:120])
    r = teacher.get("/api/learners/nobody-here/progress")
    check("a Teacher asking for a learner that does not exist gets a 404", r.status_code == 404, r.status_code)

    # migration: the table is counted, exported and imported like every other (README, "Adding a new model?")
    admin = client_for("boss", role="Admin")
    r = admin.get("/api/export/counts")
    check("/api/export/counts includes training_sessions", (r.get_json() or {}).get("counts", {}).get("training_sessions") == 4, r.get_data(as_text=True)[:160])
    r = ada.get("/api/export/training_sessions")
    check("exporting training sessions needs an Admin (403 for a learner)", r.status_code == 403, r.status_code)
    r = admin.get("/api/export/training_sessions")
    exported = (r.get_json() or {}).get("training_sessions", [])
    check("an Admin exports every session with its session_id and learner", r.status_code == 200 and len(exported) == 4 and all(s["learner_id"] == "ada" and s["session_id"] for s in exported), r.get_data(as_text=True)[:160])
    check("/api/export/all carries them too", len((admin.get("/api/export/all").get_json() or {}).get("training_sessions", [])) == 4)
    r = admin.post("/api/export/import/training_sessions", json={"training_sessions": exported})
    stats = (r.get_json() or {}).get("training_sessions", {})
    check("importing sessions that are already here skips them all", (stats.get("imported"), stats.get("skipped"), stats.get("failed")) == (0, 4, 0) and rows() == 4, stats)
    with app.app_context():
        TrainingSession.query.delete()
        db.session.commit()
    r = admin.post("/api/export/import/training_sessions", json={"training_sessions": exported + [{**exported[0], "session_id": "orphan", "learner_id": "gone"}]})
    stats = (r.get_json() or {}).get("training_sessions", {})
    check("importing into an empty table restores every session; one whose learner is gone is reported as failed",
          (stats.get("imported"), stats.get("skipped"), stats.get("failed")) == (4, 0, 1) and rows("ada") == 4, stats)
    r = admin.get("/api/export/training_sessions")
    check("the restored sessions equal the exported ones, times included",
          sorted((r.get_json() or {}).get("training_sessions", []), key=lambda s: s["session_id"]) == sorted(exported, key=lambda s: s["session_id"]))
    r = admin.post("/api/export/import", json={"training_sessions": exported})
    check("/api/export/import takes them as well (all skipped here)", ((r.get_json() or {}).get("results", {}).get("training_sessions", {}).get("skipped")) == 4, r.get_data(as_text=True)[:200])
    r = ada.post("/api/export/import/training_sessions", json={"training_sessions": exported})
    check("importing needs an Admin (403 for a learner)", r.status_code == 403, r.status_code)

    # the rows go with the account
    with app.app_context():
        User.query.filter_by(_uid="ada").first().delete()
        check("deleting an account deletes its training sessions", TrainingSession.query.count() == 0, TrainingSession.query.count())

    # seeding
    with app.app_context():
        initTrainingSessions()
        check("seeding without the default test user adds nothing", TrainingSession.query.count() == 0, TrainingSession.query.count())
    client_for(app.config["USER_UID"])
    initTrainingSessions()
    initTrainingSessions()
    seeded = rows(app.config["USER_UID"])
    check("seeding twice leaves one copy of each sample session", seeded == 2 and rows() == 2, (seeded, rows()))

    real_after = sha256(REAL_DB) if os.path.exists(REAL_DB) else None
    check("the real dev database was not touched", real_before == real_after)

    print(f"\n{'ALL PASSED' if not _failures else f'{len(_failures)} FAILED'}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
