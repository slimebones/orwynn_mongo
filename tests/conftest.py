from orwynn import Cfg
import pytest

from orwynn_mongo import Doc


class SimpleDocument(Doc):
    name: str
    price: float
    priority: int = 5

class NestedDocument(Doc):
    nested: dict

class MockCfg(Cfg):
    pass

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
