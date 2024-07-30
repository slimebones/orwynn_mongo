import typing
import pytest
from orwynn import Cfg

from orwynn_mongo import Doc, MongoCfg

import os
from asyncio import Queue
from typing import Self

import pytest
import pytest_asyncio
from rxcat import Conn, ConnArgs, ServerBusCfg, Transport
from pydantic import BaseModel

from orwynn import App, AppCfg, Cfg, CfgPack
from orwynn_mongo import plugin as mongo_plugin


@pytest_asyncio.fixture(autouse=True)
async def autorun():
    os.environ["ORWYNN_MODE"] = "test"
    os.environ["ORWYNN_DEBUG"] = "1"
    os.environ["ORWYNN_ALLOW_CLEAN"] = "1"

    yield
    await App.ie().destroy(is_hard=True)

@pytest.fixture
def app_cfg() -> AppCfg:
    return AppCfg(
        plugins=[mongo_plugin],
        server_bus_cfg=ServerBusCfg(
            transports=[
                Transport(
                    is_server=True,
                    conn_type=MockConn
                )
            ],
            reg_types=[Mock_1, MockCollection]),
        extend_cfg_pack=typing.cast(CfgPack, {
            "test": [
                MockCfg(num=1),
                MongoCfg(
                    url="mongodb://localhost:9006",
                    database_name="orwynn_mongo_test",
                    must_clean_db_on_destroy=True,
                    default__collection_naming="camel_case",
                    default__is_archivable=False),
            ]
        }))

class MockConn(Conn[None]):
    def __init__(self, args: ConnArgs[None] = ConnArgs(core=None)) -> None:
        super().__init__(args)
        self.inp_queue: Queue[dict] = Queue()
        self.out_queue: Queue[dict] = Queue()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> dict:
        return await self.recv()

    async def recv(self) -> dict:
        return await self.inp_queue.get()

    async def send(self, data: dict):
        await self.out_queue.put(data)

    async def close(self):
        self._is_closed = True

    async def client__send(self, data: dict):
        await self.inp_queue.put(data)

    async def client__recv(self) -> dict:
        return await self.out_queue.get()

@pytest_asyncio.fixture
async def app(app_cfg: AppCfg) -> App:
    app = await App().init(app_cfg)
    return app

class Mock_1(Cfg):
    key: str

    @staticmethod
    def code():
        return "orwynn_mongo::test::mock_1"

class SimpleDocument(Doc):
    name: str
    price: float
    priority: int = 5

class NestedDocument(Doc):
    nested: dict

class MockCfg(Cfg):
    num: int

@pytest.fixture
def document_1(app) -> SimpleDocument:
    return SimpleDocument(
        name="pizza",
        price=1.2,
        priority=2
    ).create()


@pytest.fixture
def document_2(app) -> SimpleDocument:
    return SimpleDocument(
        name="donut",
        price=1
    ).create()


@pytest.fixture
def document_3(app) -> SimpleDocument:
    return SimpleDocument(
        name="sushi",
        price=1.5,
        priority=3
    ).create()


@pytest.fixture
def nested_document_1(app) -> NestedDocument:
    return NestedDocument(
        nested={
            "key1": {
                "key2": 1
            }
        }
    ).create()

class MockCollection(BaseModel):
    collection: str

    @staticmethod
    def code():
        return "orwynn_mongo::test::mock_collection"
