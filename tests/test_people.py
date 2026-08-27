"""More than one person uses the portal.

Wes, 2026-07-28: "would it be feasible to add additional users that can have
their own projects? ... All current projects should be assigned to me, and they
should be able to be reassigned if desired. They should be able to belong to
multiple users who can each prompt separately while using the same context of
work history and whatnot, but the model should be able to recognize us as
different people."

What is worth pinning here, in the order it can go wrong:

- The owner exists, always, and their name is the site config's - because
  `SITE.owner` already names that same human in the agent contract, the todo
  headings and every notification, and two sources of truth for one person's
  name is a prompt that names two people.
- The backfill hands the existing board to the owner ONCE, so taking yourself
  off a project survives a restart.
- `resolve()`'s precedence, which is the whole identity design.
- The prompt is byte-for-byte unchanged while there is only one person.
"""
from __future__ import annotations



import pytest

from app import config, db, notes, people, site


@pytest.fixture
def project():
    return db.create_project(title="A project", description="d")


# --------------------------------------------------------------------------
# The owner
# --------------------------------------------------------------------------

def test_there_is_always_an_owner():
    person = people.owner()
    assert person["name"] == config.SITE.owner
    assert int(person["is_owner"]) == 1


def test_the_owner_is_created_only_once():
    first = int(people.owner()["id"])
    for _ in range(5):
        people.ensure_owner()
    assert int(people.owner()["id"]) == first
    assert len(people.everyone()) == 1


def test_deleting_every_person_does_not_leave_the_portal_without_one():
    # Not a hypothetical: the owner is who an unattributed note is credited to,
    # so `owner()` returning None would be a crash in the prompt builder.
    conn = db.get_conn()
    conn.execute("DELETE FROM people")
    conn.commit()
    assert people.owner()["name"] == config.SITE.owner


def test_the_owners_name_follows_the_site_config(monkeypatch):
    # portal.toml is the one source of truth for this person's name, because
    # SITE.owner already names them in the agent contract and the todo
    # headings. A row that could disagree would put two names for one human in
    # the same prompt.
    people.owner()  # created under the real config first
    other = site.Site(**{**site.defaults(), "owner": "Ada Lovelace", "gender": "female"})
    monkeypatch.setattr(config, "SITE", other)
    person = people.owner()
    assert person["name"] == "Ada Lovelace"
    assert people.pronouns_of(person) == ("she", "her", "her", "hers")
    # ...and it did not make a SECOND owner on the way.
    assert len([p for p in people.everyone() if int(p["is_owner"])]) == 1


def test_the_owner_cannot_be_renamed_out_from_under_the_config():
    owner_id = int(people.owner()["id"])
    people.update(owner_id, name="Somebody Else", background="learned a thing")
    person = people.owner()
    assert person["name"] == config.SITE.owner
    # The half that IS this table's to own still took the edit.
    assert person["background"] == "learned a thing"


def test_the_owner_cannot_be_archived():
    assert people.archive(int(people.owner()["id"])) is False
    assert people.owner() is not None


# --------------------------------------------------------------------------
# Adding people
# --------------------------------------------------------------------------

def test_a_second_person_is_entirely_this_tables_to_own():
    pid = people.add(name="Erin", gender="female", background="newer to this")
    people.update(pid, name="Erin Y", background="learning fast")
    person = people.get(pid)
    assert person["name"] == "Erin Y"
    assert person["background"] == "learning fast"
    assert person["gender"] == "female"


def test_a_rename_reissues_the_slug():
    # The slug is the cookie value. Leaving it on the old name would log that
    # person out of their own identity on the next request.
    pid = people.add(name="Erin")
    assert people.get(pid)["slug"] == "erin"
    people.update(pid, name="Erin Yarber")
    assert people.get(pid)["slug"] == "erin-yarber"
    assert people.by_slug("erin-yarber") is not None


def test_two_people_with_the_same_name_get_different_slugs():
    a = people.add(name="Sam")
    b = people.add(name="Sam")
    assert people.get(a)["slug"] != people.get(b)["slug"]


def test_a_nameless_person_still_gets_a_usable_slug():
    # The slug is a cookie value and a URL segment; "" would resolve to
    # everybody and nobody.
    pid = people.add(name="   ")
    assert people.get(pid)["slug"]
    assert people.by_slug(people.get(pid)["slug"]) is not None


def test_gender_is_normalized_the_same_way_the_site_config_normalizes_it():
    for written, expected in [
        ("female", "female"), ("Male", "male"), ("F", "female"), ("man", "male"),
        # The retired pronoun spellings, so an install that answered once has.
        ("she/her", "female"), ("He/Him", "male"), ("they", ""),
        ("", ""), ("nonsense", ""),
    ]:
        pid = people.add(name=f"P {written}", gender=written)
        assert people.get(pid)["gender"] == expected, written


def test_the_words_a_person_gets_are_derived_from_that_one_answer():
    """Nobody is ever asked for a pronoun set - the prose follows the answer."""
    assert people.pronouns_of(people.get(people.add(name="M", gender="male"))) == (
        "he", "him", "his", "his"
    )
    assert people.pronouns_of(people.get(people.add(name="F", gender="female"))) == (
        "she", "her", "her", "hers"
    )
    # Nobody has asked this one, so the prose stays neutral rather than guessing.
    assert people.pronouns_of(people.get(people.add(name="Unasked"))) == (
        "they", "them", "their", "theirs"
    )


def test_archiving_keeps_the_person_and_takes_them_out_of_the_pickers():
    pid = people.add(name="Erin")
    assert people.archive(pid) is True
    assert pid not in {int(p["id"]) for p in people.everyone()}
    assert pid in {int(p["id"]) for p in people.everyone(include_archived=True)}
    # And they still resolve from a live cookie: archiving retires somebody
    # from the pickers, it is not a lockout, and silently becoming a different
    # person mid-session is worse than seeing a grayed-out name.
    assert int(people.resolve(cookie_slug="erin")["id"]) == pid


# --------------------------------------------------------------------------
# Whose project is it
# --------------------------------------------------------------------------

def test_a_new_project_belongs_to_the_owner(project):
    assert [p["name"] for p in people.members(project["id"])] == [config.SITE.owner]


def test_a_project_can_be_made_somebody_elses(project):
    erin = people.add(name="Erin")
    people.set_members(project["id"], [erin])
    assert [int(p["id"]) for p in people.members(project["id"])] == [erin]
    assert people.project_ids_for(erin) == {int(project["id"])}
    assert people.project_ids_for(int(people.owner()["id"])) == set()


def test_a_project_can_belong_to_both_of_them(project):
    erin = people.add(name="Erin")
    owner_id = int(people.owner()["id"])
    people.set_members(project["id"], [owner_id, erin])
    assert people.member_ids(project["id"]) == {owner_id, erin}
    # "the same context of work history and whatnot" - nothing is partitioned,
    # so both of them see the one journal.
    db.add_journal(project["id"], "user", "note", "hello", person_id=erin)
    db.add_journal(project["id"], "user", "note", "hi", person_id=owner_id)
    assert len(db.list_journal(project["id"])) == 2


def test_a_project_cannot_be_left_with_nobody_on_it(project):
    # Reachable with two clicks by unticking the last box, and it is not a
    # useful state: the project would appear on no dashboard and the prompt
    # would have no one to address.
    people.set_members(project["id"], [])
    assert [p["name"] for p in people.members(project["id"])] == [config.SITE.owner]


def test_membership_does_not_silently_invent_people(project):
    people.set_members(project["id"], [9999])
    assert [p["name"] for p in people.members(project["id"])] == [config.SITE.owner]


def test_deleting_a_project_takes_its_membership_with_it(project):
    erin = people.add(name="Erin")
    people.set_members(project["id"], [erin])
    db.delete_project(project["id"])
    assert people.project_ids_for(erin) == set()


def test_members_by_project_agrees_with_members(project):
    erin = people.add(name="Erin")
    second = db.create_project(title="B")
    people.set_members(project["id"], [erin])
    grouped = people.members_by_project()
    for row in (project, second):
        assert [int(p["id"]) for p in grouped.get(int(row["id"]), [])] == [
            int(p["id"]) for p in people.members(row["id"])
        ]


# --------------------------------------------------------------------------
# The backfill: the board Wes already has is his
# --------------------------------------------------------------------------

def test_the_backfill_hands_every_existing_project_to_the_owner():
    # Simulate a portal.db from before people existed: projects and notes with
    # no membership and no person on them.
    a = db.create_project(title="Old A")
    b = db.create_project(title="Old B")
    note_id = db.add_journal(a["id"], "user", "note", "an old note")
    conn = db.get_conn()
    conn.execute("DELETE FROM project_people")
    conn.execute("UPDATE journal SET person_id = NULL")
    conn.commit()
    db.set_setting(people.BACKFILL_KEY, "0")

    db._backfill_people()

    owner_id = int(people.owner()["id"])
    assert people.member_ids(a["id"]) == {owner_id}
    assert people.member_ids(b["id"]) == {owner_id}
    assert int(db.get_journal(note_id)["person_id"]) == owner_id


def test_the_backfill_does_not_undo_a_reassignment():
    # "they should be able to be reassigned if desired" - so an unconditional
    # INSERT ... SELECT id FROM projects on every boot would be the portal
    # overruling a decision a person made.
    a = db.create_project(title="Hers")
    erin = people.add(name="Erin")
    people.set_members(a["id"], [erin])

    db._backfill_people()  # i.e. a restart

    assert people.member_ids(a["id"]) == {erin}


def test_the_backfill_leaves_entries_that_had_no_person_behind_them():
    # An agent's progress report and a system status line have no author. The
    # byline would be meaningless everywhere it appeared.
    a = db.create_project(title="P")
    agent_entry = db.add_journal(a["id"], "agent", "progress", "I did a thing")
    system_entry = db.add_journal(a["id"], "system", "status", "restarted")
    db.get_conn().execute("UPDATE journal SET person_id = NULL")
    db.get_conn().commit()
    db.set_setting(people.BACKFILL_KEY, "0")

    db._backfill_people()

    assert db.get_journal(agent_entry)["person_id"] is None
    assert db.get_journal(system_entry)["person_id"] is None


# --------------------------------------------------------------------------
# Which of them is holding the phone
# --------------------------------------------------------------------------

def test_with_one_person_and_no_cookie_nothing_changes():
    # Every request this portal has ever served. It must keep behaving exactly
    # as it did.
    assert int(people.resolve()["id"]) == int(people.owner()["id"])


def test_the_cookie_wins():
    erin = people.add(name="Erin")
    assert int(people.resolve(cookie_slug="erin")["id"]) == erin


def test_the_tailnet_login_identifies_somebody_with_no_cookie():
    erin = people.add(name="Erin", tailnet_login="Erin@example.com")
    assert int(people.resolve(tailnet_login="erin@example.com")["id"]) == erin


def test_the_cookie_beats_the_tailnet_login():
    # She is signed in under his tailnet user today, so whois would confidently
    # call her Wes. A person who has said who they are outranks anything the
    # network infers about the device they are holding.
    erin = people.add(name="Erin")
    people.update(int(people.owner()["id"]), tailnet_login="wes@example.com")
    assert int(people.resolve(cookie_slug="erin", tailnet_login="wes@example.com")["id"]) == erin


def test_an_unknown_cookie_falls_through_rather_than_failing():
    # A cookie left over from a person who was deleted, or from another install.
    people.add(name="Erin", tailnet_login="erin@example.com")
    assert people.resolve(cookie_slug="nobody-by-that-name")["name"] == config.SITE.owner


def test_an_empty_tailnet_login_matches_nobody():
    # `tailnet_login` defaults to '' for everybody who has not been given one,
    # so a lookup of '' matching would hand the portal an arbitrary person.
    people.add(name="Erin")
    people.add(name="Sam")
    assert people.resolve(tailnet_login="")["name"] == config.SITE.owner
    assert people.by_tailnet_login("") is None


def test_an_archived_person_is_not_identified_by_their_tailnet_login():
    erin = people.add(name="Erin", tailnet_login="erin@example.com")
    people.archive(erin)
    assert people.resolve(tailnet_login="erin@example.com")["name"] == config.SITE.owner


def test_the_whois_lookup_never_raises(monkeypatch):
    # One hint feeding resolve(); the fallback is what the portal did before
    # people existed. It must never be able to fail a request.
    from app import netinfo

    assert people.tailnet_login_for("") == ""
    assert people.tailnet_login_for("127.0.0.1") == ""
    monkeypatch.setattr(netinfo, "_tailscale", lambda *a: None)
    assert people.tailnet_login_for("100.1.2.3") == ""
    monkeypatch.setattr(netinfo, "_tailscale", lambda *a: {"UserProfile": None})
    assert people.tailnet_login_for("100.1.2.3") == ""
    monkeypatch.setattr(netinfo, "_tailscale", lambda *a: {"UserProfile": {"LoginName": "A@B.com"}})
    assert people.tailnet_login_for("100.1.2.3") == "a@b.com"

    def boom(*a):
        raise RuntimeError("daemon down")

    monkeypatch.setattr(netinfo, "_tailscale", boom)
    assert people.tailnet_login_for("100.1.2.3") == ""


# --------------------------------------------------------------------------
# What the agent is told
# --------------------------------------------------------------------------

def test_a_single_person_install_gets_no_people_section(project):
    # The prompt must be byte-for-byte what it was, so that nothing shifts
    # under a feature nobody is using.
    assert people.prompt_section(project["id"]) == ""


def test_the_section_appears_the_moment_there_are_two_of_them(project):
    people.add(name="Erin", gender="female", background="newer to self-hosting")
    text = people.prompt_section(project["id"])
    assert "## People" in text
    assert config.SITE.owner in text and "Erin" in text
    assert "(she/her)" in text
    assert "newer to self-hosting" in text


def test_the_section_says_who_is_actually_on_this_project(project):
    erin = people.add(name="Erin", gender="female")
    people.set_members(project["id"], [erin])
    text = people.prompt_section(project["id"])
    assert "**Erin** (she/her) - on this project" in text
    assert f"**{config.SITE.owner}**" in text
    owner_line = next(
        line for line in text.splitlines() if f"**{config.SITE.owner}**" in line
    )
    assert owner_line.endswith("- not on this project")


def test_the_section_tells_the_agent_to_pitch_at_the_reader(project):
    people.add(name="Erin", background="newer to this")
    text = people.prompt_section(project["id"])
    assert "Pitch each answer at the person you are answering" in text


# --------------------------------------------------------------------------
# The note block is signed
# --------------------------------------------------------------------------

def test_one_persons_notes_read_as_they_always_did(project):
    db.add_journal(project["id"], "user", "note", "do the thing")
    block = notes.render(notes.pending(project["id"]))
    assert block.startswith(f"## A note from {config.SITE.owner} since your last run")


def test_a_note_with_no_person_on_it_is_the_owners(project):
    # Everything written before people existed. The fallback is not a guess -
    # it is what the backfill asserts.
    entry = db.add_journal(project["id"], "user", "note", "an old note")
    db.get_conn().execute("UPDATE journal SET person_id = NULL WHERE id = ?", (entry,))
    db.get_conn().commit()
    block = notes.render(notes.pending(project["id"]))
    assert config.SITE.owner in block


def test_the_wording_over_a_note_follows_that_persons_gender(project):
    erin = people.add(name="Erin", gender="female")
    db.add_journal(project["id"], "user", "note", "how do I start", person_id=erin)
    block = notes.render(notes.pending(project["id"]))
    assert block.startswith("## A note from Erin since your last run")
    assert "She wrote this" in block
    # The string this replaced was a hard-coded "He", which is the one line in
    # the whole prompt the de-personalisation work missed.
    assert "He wrote this" not in block


def test_notes_from_two_people_are_each_signed(project):
    erin = people.add(name="Erin", gender="female")
    db.add_journal(project["id"], "user", "note", "his note")
    db.add_journal(project["id"], "user", "note", "her note", person_id=erin)
    block = notes.render(notes.pending(project["id"]))
    # No single name in the heading - it would be a lie about half of them.
    assert "## 2 notes since your last run, from" in block
    assert "Erin" in block and config.SITE.owner in block
    assert "answer each person in their own terms" in block
    # ...and each entry carries its own byline, so the agent can tell which
    # sentence came from whom.
    his = block.index("his note")
    hers = block.index("her note")
    assert block.rindex("Erin", 0, hers) > his


# --------------------------------------------------------------------------
# The retired `pronouns` column
# --------------------------------------------------------------------------
# Wes, 2026-07-28: "Let's get rid of the pronoun stuff and just ask someone if
# they are male or female to know how to refer to them."
#
# The risk in that rename is not the new field, it is the old rows: a bare
# `gender TEXT NOT NULL DEFAULT ''` on an ALTER silently resets everybody who
# has already answered to they/them, and nothing on any screen says so.

def _people_table_as_it_was(path):
    """A database at the pre-2026-07-28 shape, with a `pronouns` column."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            pronouns TEXT NOT NULL DEFAULT 'they',
            background TEXT NOT NULL DEFAULT '',
            tailnet_login TEXT NOT NULL DEFAULT '',
            is_owner INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            archived_at TEXT
        );
        INSERT INTO people (slug, name, pronouns, is_owner, created_at)
             VALUES ('wes', 'Wes', 'he', 1, '2026-07-01T00:00:00+00:00');
        INSERT INTO people (slug, name, pronouns, is_owner, created_at)
             VALUES ('erin', 'Erin', 'she', 0, '2026-07-28T00:00:00+00:00');
        INSERT INTO people (slug, name, pronouns, is_owner, created_at)
             VALUES ('sam', 'Sam', 'they', 0, '2026-07-28T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()


def test_an_answered_pronoun_becomes_an_answered_gender(tmp_path, monkeypatch):
    """Nobody who has already said which they are gets asked again."""
    path = tmp_path / "old.db"
    _people_table_as_it_was(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "_CONN", None)
    db.init_db()

    by_name = {r["name"]: r for r in db.get_conn().execute("SELECT * FROM people")}
    assert by_name["Erin"]["gender"] == "female"
    assert by_name["Sam"]["gender"] == "", "they/them is an unanswered row, not a third sex"


def test_the_backfill_leaves_the_owner_to_the_config(tmp_path, monkeypatch):
    """The owner's answer is portal.toml's, and a migration may not overrule it.

    Caught by running the published tree standalone, where portal.toml does not
    exist: the backfill wrote `male` from the old pronoun on the first boot and
    `ensure_owner` wrote the config's value back on the second, so the settings
    page changed its mind overnight. The config route already carries an
    upgrading install (site.load reads a legacy `pronouns` line), so this row
    has no second source to need.
    """
    path = tmp_path / "old.db"
    _people_table_as_it_was(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "_CONN", None)

    for boot in (1, 2):
        db.init_db()
        owner = people.owner()
        assert owner["gender"] == site.gender_key(config.SITE.gender), (
            f"boot {boot}: the owner's gender is not the config's"
        )


def test_clearing_somebodys_answer_is_not_undone_at_the_next_boot(tmp_path, monkeypatch):
    """The guard is a settings key, not "is gender empty".

    '' is a legitimate answer - it is what "nobody has asked" looks like - so a
    backfill that re-ran on every boot would put the stale pronoun back every
    time somebody deliberately cleared it, and the settings page would appear
    to simply not save.
    """
    path = tmp_path / "old.db"
    _people_table_as_it_was(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "_CONN", None)
    db.init_db()

    erin = people.by_slug("erin")
    people.update(int(erin["id"]), gender="")
    assert people.by_slug("erin")["gender"] == ""

    db.init_db()  # a restart
    assert people.by_slug("erin")["gender"] == "", "the backfill overruled a real edit"


# --------------------------------------------------------------------------
# Dropping the retired `pronouns` column
# --------------------------------------------------------------------------
# The column outlived the rename on purpose: `_backfill_gender` needed it as a
# source, and an ALTER ... DROP on a live table has no undo. It comes off on
# 2026-08-16, and the danger in that is not the drop - it is the backfill going
# with it. An install upgrading from before the rename straight to today has
# every answer in `pronouns` and nothing in `gender`, so a drop that did not
# move them first would reset those people to they/them on the way past, which
# is the exact failure `_backfill_gender` was written to prevent, arriving by
# way of its deletion.


def _people_table_part_way_through(path):
    """The shape a live install is in TODAY: both columns, backfill already run.

    Wes's own database, in other words - `gender` answered, a stale `pronouns`
    sitting beside it, and `people_gender_backfilled` set. The move must NOT
    run again from here, because '' is a legitimate answer and re-running it
    would put a stale pronoun back over a clearing somebody meant.
    """
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            pronouns TEXT NOT NULL DEFAULT 'they',
            gender TEXT NOT NULL DEFAULT '',
            background TEXT NOT NULL DEFAULT '',
            tailnet_login TEXT NOT NULL DEFAULT '',
            is_owner INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            archived_at TEXT
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
        INSERT INTO settings (key, value) VALUES ('people_gender_backfilled', '1');
        INSERT INTO people (slug, name, pronouns, gender, is_owner, created_at)
             VALUES ('wes', 'Wes', 'he', 'male', 1, '2026-07-01T00:00:00+00:00');
        INSERT INTO people (slug, name, pronouns, gender, is_owner, created_at)
             VALUES ('erin', 'Erin', 'she', '', 0, '2026-07-28T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()


def _columns_of_people():
    return {row["name"] for row in db.get_conn().execute("PRAGMA table_info(people)")}


def test_the_pronouns_column_comes_off_an_upgrading_database(tmp_path, monkeypatch):
    """The point of the change: the dead column is actually gone."""
    path = tmp_path / "old.db"
    _people_table_as_it_was(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "_CONN", None)
    db.init_db()

    assert "pronouns" not in _columns_of_people()
    assert "gender" in _columns_of_people(), "the replacement has to still be there"


def test_a_fresh_database_has_no_pronouns_column(tmp_path, monkeypatch):
    """Nothing re-creates it - it is not in SCHEMA and not in _ADDED_COLUMNS."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "new.db")
    monkeypatch.setattr(db, "_CONN", None)
    db.init_db()

    assert "pronouns" not in _columns_of_people()


def test_the_answers_move_across_on_the_boot_that_drops_the_column(tmp_path, monkeypatch):
    """The backfill has ONE boot left to run in, and it is this one.

    This is the test that stops `_drop_pronouns` being simplified back down to
    a bare DROP: an install that never saw the 2026-07-28 release has its only
    copy of these answers in the column being dropped, so the move has to
    happen on the same boot, before the column goes.
    """
    path = tmp_path / "old.db"
    _people_table_as_it_was(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "_CONN", None)
    db.init_db()

    assert "pronouns" not in _columns_of_people(), "the column should be gone"
    assert people.by_slug("erin")["gender"] == "female", (
        "an answered person was reset to they/them by the drop"
    )
    assert people.by_slug("sam")["gender"] == "", "they/them was never an answer"


def test_the_drop_does_not_reopen_an_answer_somebody_cleared(tmp_path, monkeypatch):
    """On a database where the move already ran, it must not run a second time.

    This is the live shape, so it is the branch that actually executes on Wes's
    install: the flag is set, `gender` is deliberately '', and the stale
    pronoun says "she". Re-running the move on the way to the drop would undo a
    real edit at the moment the evidence for it disappears forever.
    """
    path = tmp_path / "mid.db"
    _people_table_part_way_through(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "_CONN", None)
    db.init_db()

    assert "pronouns" not in _columns_of_people()
    assert people.by_slug("erin")["gender"] == "", (
        "the drop put a stale pronoun back over a cleared answer"
    )


def test_the_owner_is_skipped_even_when_the_pronoun_disagrees(tmp_path, monkeypatch):
    """`AND is_owner = 0` is load-bearing, and THIS is the install where it bites.

    Two sweeps were needed to write this test, and the first version of it was
    wrong in an instructive way. `ensure_owner` runs before `_drop_pronouns`
    and writes the owner's gender from the config, so on an install whose
    portal.toml answers the question the owner's row is no longer '' by the
    time the migration's `COALESCE(gender, '') = ''` looks at it - the clause
    excludes them anyway and dropping `AND is_owner = 0` changes nothing that
    any assertion can see. Both the original owner test and a first attempt at
    this one sailed through the mutation for that reason.

    The clause earns its keep on the install that has NOT said: a published
    tree with no portal.toml, where `config.SITE.gender` is '' and so is the
    owner's row. There the retired column is the only thing left holding an
    answer, and taking it would mean a migration deciding something the person
    never told this install - which is the whole reason the owner is the
    config's to answer for. So the config is pinned to '' here rather than
    inherited, and the test states its own conditions.

    It also reads the stored ROW rather than `people.owner()`, and that took a
    third sweep to find. `owner()` calls `ensure_owner()` on every call, which
    rewrites the row's gender from the config before handing it back - so it
    repairs the damage on the way past and any assertion made through it holds
    no matter what the migration wrote. `everyone()` does no such repair, which
    is where a polluted owner row would actually surface.
    """
    import dataclasses
    import sqlite3

    monkeypatch.setattr(config, "SITE", dataclasses.replace(config.SITE, gender=""))
    assert site.gender_key(config.SITE.gender) == "", "the config must say nothing here"
    disagreeing = "he"

    path = tmp_path / "owner.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        f"""
        CREATE TABLE people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            pronouns TEXT NOT NULL DEFAULT 'they',
            background TEXT NOT NULL DEFAULT '',
            tailnet_login TEXT NOT NULL DEFAULT '',
            is_owner INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            archived_at TEXT
        );
        INSERT INTO people (slug, name, pronouns, is_owner, created_at)
             VALUES ('wes', 'Wes', '{disagreeing}', 1, '2026-07-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "_CONN", None)
    db.init_db()

    stored = db.get_conn().execute(
        "SELECT gender FROM people WHERE is_owner = 1"
    ).fetchone()["gender"]
    assert stored == "", (
        "the migration answered for the owner out of the retired column"
    )
    assert "pronouns" not in _columns_of_people()


def test_a_typed_answer_beats_the_stale_pronoun_beside_it(tmp_path, monkeypatch):
    """An answer given in the UI outranks the column being dropped.

    The install that meets this: upgraded from before the rename, so the flag
    is unset and the move still has to run - but somebody opened the settings
    page in between and said which they are. Their typed answer and the old
    pronoun disagree, and the move must not overwrite the one a person gave
    with the one a migration found. `COALESCE(gender, '') = ''` is the line
    that decides it, and this is the only test that would notice it going.
    """
    import sqlite3

    path = tmp_path / "old.db"
    _people_table_as_it_was(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE people ADD COLUMN gender TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE people SET gender = 'female' WHERE slug = 'sam'")
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "_CONN", None)
    db.init_db()

    assert people.by_slug("sam")["gender"] == "female", (
        "the migration overwrote an answer somebody typed"
    )
    assert people.by_slug("erin")["gender"] == "female", "an unanswered row still moves"
    assert "pronouns" not in _columns_of_people()


def test_the_backfill_flag_goes_with_the_column(tmp_path, monkeypatch):
    """A settings row no code can explain is what this cleanup is about.

    It has to be the part-way-through fixture, and that is the whole test. On a
    database that never ran the old backfill the flag was never written, so
    nothing sets it and `is None` is true whether or not the DELETE runs - the
    first version of this test used that fixture and a sweep walked the
    mutation straight through it. Only an install that HAS the row can show it
    being taken away.
    """
    path = tmp_path / "mid.db"
    _people_table_part_way_through(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "_CONN", None)

    import sqlite3

    before = sqlite3.connect(path).execute(
        "SELECT value FROM settings WHERE key = 'people_gender_backfilled'"
    ).fetchone()
    assert before == ("1",), "the fixture must actually carry the row being dropped"

    db.init_db()

    assert db.get_setting(db.GENDER_BACKFILL_KEY) is None


def test_dropping_the_column_is_idempotent(tmp_path, monkeypatch):
    """The column's own absence is the guard, so a restart is a no-op."""
    path = tmp_path / "old.db"
    _people_table_as_it_was(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "_CONN", None)

    for _ in range(3):
        db.init_db()

    assert "pronouns" not in _columns_of_people()
    assert people.by_slug("erin")["gender"] == "female"
