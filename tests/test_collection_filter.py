import pytest
from pykit.check import check
from rxcat import Ok, PubOpts, ServerBus

from orwynn_mongo import Doc, body_collection_factory
from orwynn import BaseModel, SubOpts, SysArgs, sys

from tests.conftest import MockCfg


class _Req1(BaseModel):
    collection: str

    @staticmethod
    def code():
        return "orwynn_mongo::test::req1"

class TestCollectionFilterDoc(Doc):
    pass


@sys(
    MockCfg,
    SubOpts(conditions=[body_collection_factory(TestCollectionFilterDoc)]))
async def sys__req1(args: SysArgs[MockCfg], body: _Req1):
    return

@pytest.mark.asyncio
async def test_has_collection(app):
    r = await ServerBus.ie().pubr(_Req1(
        collection=TestCollectionFilterDoc.get_collection()))
    assert isinstance(r, Ok)

@pytest.mark.asyncio
async def test_no_collection(app):
    bus = ServerBus.ie()
    await check.aexpect(
        bus.pubr(_Req1(collection="whocares"), PubOpts(pubr__timeout=0.001)),
        TimeoutError)
