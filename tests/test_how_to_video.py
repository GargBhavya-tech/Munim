"""The how-to video for a merchant who keeps asking the same thing: an approved video, a fixed message, the executor, consent."""
import json

import pytest

from jobs.how_to_video import (ALREADY, BLOCKED, NO_CHANNEL, SENT, HowToVideoJob, build_message, load_catalog, pick_video)
from jobs.recurrence import _SCHEMA
from tests.checkpoint_script import SCRIPT
from tests.world import World

MID = "M-0009"                                           # an English-speaking Bengaluru merchant with no contact window


@pytest.fixture
def world(db_path, clock):
    w = World(db_path, clock, SCRIPT)
    w.conn.executescript(_SCHEMA)
    return w


def task(w, tid="RC-T1", mid=MID, cause="MERCHANT_NEEDS_HELP", issues=("volume",), status="OPEN"):
    w.conn.execute("INSERT INTO root_cause_tasks(task_id, merchant_id, issue_types_json, cause, action, owner_tier, history_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (tid, mid, json.dumps(list(issues)), cause, "a short video", "SENIOR", "[]", status, w.clock.now_iso()))


def consent(w, mid=MID, channel="whatsapp"):
    w.conn.execute("INSERT OR REPLACE INTO consent(subject_id, channel, status, source, ts) VALUES (?,?,'GRANTED','test',?)", (mid, channel, w.clock.now_iso()))


# ---------------------------------------------------------------------------------------------------- the catalog and the message
def test_the_catalog_has_every_video_in_all_four_languages():
    videos, templates = load_catalog()
    assert videos and set(templates) == {"en-IN", "hi-IN", "hinglish", "kn-IN"}
    for v in videos:
        assert set(v.titles) == set(templates) and v.url.startswith("https://") and v.seconds > 0


def test_every_link_is_on_a_reserved_host_so_nothing_real_can_open():
    assert all(".invalid/" in v.url for v in load_catalog()[0])


def test_the_video_is_picked_by_the_issue_and_the_basics_video_is_the_fallback():
    videos, _ = load_catalog()
    assert pick_video(videos, ["volume"]).video_id == "soundbox_volume"
    assert pick_video(videos, ["WIFI"]).video_id == "soundbox_network"                         # case does not matter
    assert pick_video(videos, ["DEVICE"]).video_id == "soundbox_volume"                        # the first video that lists the topic
    assert pick_video(videos, ["something unheard of"]).video_id == "soundbox_basics" and pick_video(videos, []).video_id == "soundbox_basics"


@pytest.mark.parametrize("lang", ["en-IN", "hi-IN", "hinglish", "kn-IN"])
def test_the_message_is_the_template_with_the_approved_title_and_link_and_nothing_else(lang):
    videos, templates = load_catalog()
    v = videos[0]
    body = build_message(v, lang, templates)
    assert body == templates[lang].format(title=v.titles[lang], url=v.url) and v.url in body and v.titles[lang] in body


def test_a_language_with_no_template_falls_back_to_english():
    videos, templates = load_catalog()
    assert build_message(videos[0], "ta-IN", templates) == build_message(videos[0], "en-IN", templates)


# ---------------------------------------------------------------------------------------------------- the job
def test_only_how_to_tasks_that_are_still_open_are_taken(world):
    task(world, "RC-A", cause="MERCHANT_NEEDS_HELP")
    task(world, "RC-B", cause="FAULTY_DEVICE")
    task(world, "RC-C", cause="MERCHANT_NEEDS_HELP", status="ACTIONED")
    assert [t["task_id"] for t in HowToVideoJob(world.conn, world.clock, world.executor).tasks()] == ["RC-A"]


def test_a_merchant_who_consented_gets_the_video_and_the_task_is_closed_off(world):
    task(world)
    consent(world)
    (r,) = HowToVideoJob(world.conn, world.clock, world.executor).run()
    assert r.status == SENT and r.video_id == "soundbox_volume" and r.channel == "whatsapp"
    sent = world.conn.execute("SELECT * FROM comms_outbox WHERE merchant_id = ?", (MID,)).fetchall()
    assert len(sent) == 1 and sent[0]["body"] == r.body and "https://videos.example.invalid/munim/soundbox_volume" in sent[0]["body"]
    assert world.conn.execute("SELECT status FROM root_cause_tasks WHERE task_id = 'RC-T1'").fetchone()[0] == "ACTIONED"
    assert world.conn.execute("SELECT video_id FROM video_sends WHERE task_id = 'RC-T1'").fetchone()[0] == "soundbox_volume"


def test_the_same_task_never_sends_twice(world):
    task(world)
    consent(world)
    job = HowToVideoJob(world.conn, world.clock, world.executor)
    job.run()
    assert job.run() == []                                                                         # the task is no longer open
    row = world.conn.execute("SELECT * FROM root_cause_tasks WHERE task_id = 'RC-T1'").fetchone()
    assert job.send_for(row).status == ALREADY
    assert world.conn.execute("SELECT COUNT(*) FROM comms_outbox WHERE merchant_id = ?", (MID,)).fetchone()[0] == 1


def test_a_merchant_with_no_consent_is_not_messaged_and_the_task_stays_open(world):
    task(world)
    (r,) = HowToVideoJob(world.conn, world.clock, world.executor).run()
    assert r.status == NO_CHANNEL and r.body is None
    assert world.conn.execute("SELECT COUNT(*) FROM comms_outbox WHERE merchant_id = ?", (MID,)).fetchone()[0] == 0
    assert world.conn.execute("SELECT status FROM root_cause_tasks WHERE task_id = 'RC-T1'").fetchone()[0] == "OPEN"


def test_a_number_on_do_not_disturb_is_not_messaged_either(world):
    task(world)
    consent(world)
    phone = world.conn.execute("SELECT phone FROM merchants WHERE merchant_id = ?", (MID,)).fetchone()[0]
    world.conn.execute("INSERT OR IGNORE INTO dnd_registry(phone) VALUES (?)", (phone,))
    (r,) = HowToVideoJob(world.conn, world.clock, world.executor).run()
    assert r.status in (BLOCKED, NO_CHANNEL) and world.conn.execute("SELECT COUNT(*) FROM comms_outbox WHERE merchant_id = ?", (MID,)).fetchone()[0] == 0


def test_the_channel_order_is_whatsapp_then_app_then_sms(world):
    task(world)
    consent(world, channel="sms")
    consent(world, channel="app")
    (r,) = HowToVideoJob(world.conn, world.clock, world.executor).run()
    assert r.channel == "app"


def test_the_merchants_own_language_picks_the_template(world):
    world.conn.execute("UPDATE merchants SET language = 'kn-IN' WHERE merchant_id = ?", (MID,))
    task(world)
    consent(world)
    (r,) = HowToVideoJob(world.conn, world.clock, world.executor).run()
    videos, templates = load_catalog()
    assert r.body == build_message(videos[0], "kn-IN", templates)
