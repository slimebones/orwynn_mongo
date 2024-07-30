from enum import Enum

from pykit.check import check

from orwynn_mongo import convert_compatible


class StringEnum(Enum):
    Red = "red"
    Green = "green"


class StringObj:
    @property
    def mongovalue(self) -> str:
        return "hello"


class DictObj:
    @property
    def mongovalue(self) -> dict:
        return {
            1: "hello",
            2: "world",
        }


class SetObj:
    @property
    def mongovalue(self) -> set:
        return {1, 2, 3}


def test_already_compatible():
    """
    Should work correctly for already compatible types.
    """
    assert convert_compatible("hello") == "hello"
    assert convert_compatible(2) == 2
    assert convert_compatible(2.3) == 2.3
    assert convert_compatible(True) is True
    assert convert_compatible([1, 2, 3]) == [1, 2, 3]
    assert convert_compatible({
        1: "hello",
        2: "world",
    }) == {
        1: "hello",
        2: "world",
    }
    assert convert_compatible(None) is None


def test_containers():
    """
    Should work correctly for containers, like list or dict, which also may
    contain sub-containers.
    """
    mock_dict: dict = {
        1: {
            1: "hello",
            2: "world",
        },
        2: [
            {
                1: "hello",
                2: "world",
            },
            {
                1: "here",
                2: "we",
                3: "go",
                4: ["again", "!!"],
            },
        ],
    }

    assert convert_compatible(mock_dict) == mock_dict


def test_enum_string():
    """
    Should convert enum correctly.
    """
    assert convert_compatible(StringEnum.Red) == "red"


def test_mongovalue_string():
    """
    Should convert objects with attribute `mongovalue`.
    """
    assert convert_compatible(StringObj()) == "hello"


def test_mongovalue_dict():
    """
    Should convert objects with attribute `mongovalue` which returns
    dict.
    """
    assert convert_compatible(DictObj()) == {
        1: "hello",
        2: "world",
    }


def test_mongovalue_set():
    """
    Should raise an error for set returning mongovalue attribute.
    """
    check.expect(
        convert_compatible,
        ValueError,
        SetObj(),
    )
