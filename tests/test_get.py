from pykit.query import AggQuery, SearchQuery

from tests.conftest import SimpleDocument

def test_main(document_1: SimpleDocument, document_2: SimpleDocument):
    assert {item.sid for item in SimpleDocument.get_many()} == {
        document_1.sid,
        document_2.sid
    }

def test_id_operators(
    document_1: SimpleDocument,
    document_2: SimpleDocument
):
    """
    Should work normally for id MongoDb operators.
    """
    f = list(SimpleDocument.get_many(SearchQuery({
        "sid": {
            "$in": [document_1.sid]
        }
    })))

    assert len(f) == 1
    assert f[0].sid == document_1.sid

def test_aggregation(
        document_1: SimpleDocument,
        document_2: SimpleDocument,
        document_3: SimpleDocument):
    f = list(SimpleDocument.agg(AggQuery.create(
        {"$sort": {"price": -1}},
        {"$limit": 2}
    )))

    assert len(f) == 2
    assert f[0].name == "sushi"
