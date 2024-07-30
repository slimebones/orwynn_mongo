import typing

from orwynn import Flag, Ok
from pykit.check import check
from pykit.err import LockErr
from pykit.query import SearchQuery, UpdQuery
from rxcat import ServerBus

from orwynn_mongo import (
    CheckLockDoc,
    Doc,
    LockDoc,
    UnlockDoc,
    DocField,
    reg_doc_types
)


def test_link_lock(app):
    class Test1Doc(Doc):
        IS_ARCHIVABLE = False
        name: str

    class Test2Doc(Doc):
        IS_LINKING_IGNORING_LOCK = True
        FIELDS = [
            DocField(name="sids", linked_doc="test1_doc")
        ]
        name: str
        sids: list[str]

    class Test3Doc(Doc):
        IS_ARCHIVABLE = False
        name: str

    class Test4Doc(Doc):
        IS_LINKING_IGNORING_LOCK = False
        FIELDS = [
            DocField(name="sids", linked_doc="test3_doc")
        ]
        name: str
        sids: list[str]

    reg_doc_types(Test1Doc, Test2Doc, Test3Doc, Test4Doc)

    d1 = Test1Doc(name="child").create()
    Test2Doc(name="parent", sids=[d1.sid]).create().lock()
    # should delete normally, since IS_LINKING_IGNORING_LOCK = True
    d1.delete()

    d3 = Test3Doc(name="child").create()
    Test4Doc(name="parent", sids=[d3.sid]).create().lock()
    # should raise LockErr, since IS_LINKING_IGNORING_LOCK = False
    check.expect(d3.delete, LockErr)

def test_lock_write(app):
    class TestLockDoc(Doc):
        IS_ARCHIVABLE = True
        name: str

    d = TestLockDoc(name="hello").create()
    d = d.lock()

    check.expect(d.upd, LockErr, UpdQuery.create(set={"name": "wow"}))
    check.expect(d.delete, LockErr)
    check.expect(d.get_and_del, LockErr, SearchQuery.create_sid(d.sid))
    check.expect(
        d.get_and_upd,
        LockErr,
        SearchQuery({"sid": d.sid}),
        UpdQuery.create(set={"name": "wow"}))

    assert not d.try_upd(UpdQuery.create(set={"name": "wow"}))
    assert not d.try_del()
    assert not d.try_get_and_del(SearchQuery.create_sid(d.sid))

    d = d.unlock()

    assert d.upd(UpdQuery.create(set={"name": "world"})).name == "world"
    assert d.try_upd(UpdQuery.create(set={"name": "wow"}))
    d.delete()
    assert not d.try_get(SearchQuery.create_sid(d.sid))

async def test_sys(app):
    class TestLockSysDoc(Doc):
        COLLECTION_NAMING = "snake_case"
        name: str

    doc = TestLockSysDoc(name="hello").create()
    bus = ServerBus.ie()

    evt = await bus.pubr(
        LockDoc(doc_collection="test_lock_sys_doc", doc_sid=doc.sid))
    assert isinstance(evt, Ok)
    assert "locked" in doc.refresh().internal_marks

    evt = await bus.pubr(CheckLockDoc(
        doc_collection="test_lock_sys_doc", doc_sid=doc.sid))
    assert isinstance(evt, Flag)
    assert typing.cast(Flag, evt).val is True

    evt = await bus.pubr(
        UnlockDoc(doc_collection="test_lock_sys_doc", doc_sid=doc.sid))
    assert isinstance(evt, Ok)
    assert "locked" not in doc.refresh().internal_marks

    evt = await bus.pubr(CheckLockDoc(
        doc_collection="test_lock_sys_doc", doc_sid=doc.sid))
    assert isinstance(evt, Flag)
    assert typing.cast(Flag, evt).val is False

