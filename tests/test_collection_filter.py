import pytest
from orwynn import AppCfg, BaseModel, SubOpts, SysArgs, sys, App
from pykit.check import check
from rxcat import Ok, PubOpts, ServerBus

from orwynn_mongo import Doc, body_collection_factory
from tests.conftest import MockCfg, MockCollection

class TestCollectionFilterDoc(Doc):
    pass

@pytest.mark.asyncio
async def test_has_collection(app_cfg: AppCfg):
    @sys(
        MockCfg,
        SubOpts(conditions=[body_collection_factory(TestCollectionFilterDoc)]))
    async def sys__mock_collection(
            args: SysArgs[MockCfg], body: MockCollection):
        assert body.collection == TestCollectionFilterDoc.get_collection()

    app = await App().init(app_cfg)
    bus = app.get_bus().eject()
    r = await bus.pubr(MockCollection(
        collection=TestCollectionFilterDoc.get_collection()))
    assert isinstance(r, Ok)

@pytest.mark.asyncio
async def test_no_collection(app_cfg: AppCfg):
    @sys(
        MockCfg,
        SubOpts(conditions=[body_collection_factory(TestCollectionFilterDoc)]))
    async def sys__mock_collection(
            args: SysArgs[MockCfg], body: MockCollection):
        assert body.collection == TestCollectionFilterDoc.get_collection()

    app = await App().init(app_cfg)
    bus = app.get_bus().eject()
    await check.aexpect(
        bus.pubr(
            MockCollection(collection="whocares"),
            PubOpts(pubr__timeout=0.001)),
        TimeoutError)
